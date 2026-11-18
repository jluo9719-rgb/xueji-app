"""隔离的公开资料评测：真实 Chroma/嵌入/重排，调参和留出严格分开。

python -B tests/eval/prepare_public.py
python -B tests/eval/public_benchmark.py
--answers-per-class 6 额外在每组每类前 6 题跑真实问答（每题无历史）。
输出为诊断指标，答案字串命中不是语义正确率；绝不自动修改线上门槛。
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT.parent / 'noim' / 'public-eval'


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def answer_covered(answers, evidence, doc_id=None):
    """按原文顺序拼回同一文档的命中块，允许标准答案跨块；不跨文档拼答案。"""
    groups = {}
    for entry in evidence:
        key = entry['doc_id']
        if doc_id is None or key == doc_id:
            groups.setdefault(key, []).append(entry)
    normalize = lambda text: re.sub(r'\s+', '', text)
    for entries in groups.values():
        text = normalize(''.join(e['text'] for e in sorted(entries, key=lambda e: e['chunk'])))
        if any(normalize(answer) in text for answer in answers if normalize(answer)):
            return True
    return False


def rates(rows, threshold):
    positive = [r for r in rows if r['expected'] == 'answer']
    negative = [r for r in rows if r['expected'] == 'refuse']
    hit = sum(r['span_hit_at_5'] for r in positive)
    passed = sum(r['top_score'] >= threshold for r in positive)
    false_pass = sum(r['top_score'] >= threshold for r in negative)
    return {'threshold': threshold, 'in_total': len(positive), 'in_pass': passed,
            'in_pass_rate': passed / len(positive) if positive else None,
            'out_total': len(negative), 'out_false_pass': false_pass,
            'out_block_rate': 1 - false_pass / len(negative) if negative else None,
            'span_hit_at_5': hit, 'span_hit_rate': hit / len(positive) if positive else None}


def calibrate(rows):
    if not rows or any(r['split'] != 'calibration' for r in rows):
        raise ValueError('门槛只能使用调参集选择')
    thresholds = sorted({0., .12, 1., *(r['top_score'] for r in rows),
                         *(math.nextafter(r['top_score'], math.inf) for r in rows)})
    sweep = [rates(rows, t) for t in thresholds]
    allowed = [r for r in sweep if r['out_false_pass'] / r['out_total'] <= .1]
    # 先最大化资料内通过；同等通过数时更少误放；再选择较低门槛。
    best = min(allowed, key=lambda r: (-r['in_pass'], r['out_false_pass'], r['threshold']))
    return best, sweep


async def run(args):
    target = DATA / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6])
    target.mkdir(parents=True)
    os.environ['XUEJI_DATA_DIR'] = str(target / 'runtime')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    sys.path.insert(0, str(ROOT))
    from core.config import config
    from core.engine import AgentEngine
    from core.session import SessionStore
    from core.document_chat import NeedsGeneralAnswer, ANSWER_PROMPT_VERSION
    from core.credentials import get_llm_model
    from rag.kb_manager import get_kb_manager, session_kb_id
    from rag.evidence import retrieve_evidence
    from rag.chunker import CHUNK_VERSION
    manifest = json.loads((DATA / 'manifest.json').read_text(encoding='utf-8'))
    questions = [json.loads(line) for line in (DATA / 'questions.jsonl').read_text(encoding='utf-8').splitlines()]
    docs = {d['key']: d for d in manifest['documents']}
    scopes = {split: {q['document'] for q in questions if q['split'] == split} for split in ('calibration', 'holdout')}
    if scopes['calibration'] & scopes['holdout']:
        raise ValueError('原文在调参和留出集重叠')
    config._data['retrieval']['evidence_min_score'] = 0.
    kb = get_kb_manager()
    kb._ensure_worker = lambda: None
    rows, uploaded = [], []
    started = time.perf_counter()
    report = {'archive_sha256': manifest['archive_sha256'],
              'questions_sha256': hashlib.sha256((DATA / 'questions.jsonl').read_bytes()).hexdigest(),
              'embedding': config.get('embedding.model'), 'reranker': config.get('rerank.model'),
              'llm': get_llm_model(), 'chunk_version': CHUNK_VERSION, 'answer_prompt_version': ANSWER_PROMPT_VERSION,
              'retrieval_settings': dict(config._data['retrieval']),
              'limitations': manifest['limitations'] + ['段落隔离检索不是多文档召回成绩；共享资料召回另列',
                  '答案命中为标准答案字串检查，不能证明整段回答忠实', '留出集查看后不能继续作为未见集反复调参'],
              'rows': rows, 'answers': [], 'mixed_retrieval': [], 'status': 'running'}
    save(target / 'result.json', report)
    print('OUTPUT', target, flush=True)
    try:
        for index, q in enumerate(questions, 1):
            doc = docs[q['document']]
            content = (DATA / doc['path']).read_bytes()
            if hashlib.sha256(content).hexdigest() != doc['sha256']:
                raise ValueError('评测资料被修改')
            sid = 'public-' + q['document']
            record = kb.add_document(q['document'] + '.md', content, session_kb_id(sid))
            uploaded.append((record['doc_id'], session_kb_id(sid)))
            kb._index_document(record['doc_id'])
            ts = time.perf_counter()
            evidence = retrieve_evidence(sid, q['question'])
            rows.append(dict(q, top_score=evidence[0]['score'] if evidence else 0.,
                             span_hit_at_5=answer_covered(q['answers'], evidence),
                             seconds=round(time.perf_counter() - ts, 3),
                             top_chunks=[{'doc_id': e['doc_id'], 'chunk_id': e['chunk_id'],
                                          'chunk': e['chunk'], 'score': e['score'], 'text': e['text']} for e in evidence]))
            save(target / 'result.json', report)
            print(f"retrieve {index}/{len(questions)} {q['split']} {q['expected']} {rows[-1]['top_score']:.4f}", flush=True)
        calibration = [r for r in rows if r['split'] == 'calibration']
        holdout = [r for r in rows if r['split'] == 'holdout']
        best, sweep = calibrate(calibration)
        report.update(selected_threshold=best['threshold'], calibration=best,
                      holdout=rates(holdout, best['threshold']), threshold_sweep=sweep,
                      existing_threshold={'calibration': rates(calibration, .12), 'holdout': rates(holdout, .12)})
        save(target / 'result.json', report)
        # 多文档正例召回：同一 split 的 30 份有答案原文同时入库；不将这里的其他原文
        # 加入无答案测试，以免破坏上游仅相对于给定篇章的无答案标签。
        for split in ('calibration', 'holdout'):
            sid = 'mixed-' + split
            mapped = {}
            for q in (q for q in questions if q['split'] == split and q['expected'] == 'answer'):
                doc = docs[q['document']]
                rec = kb.add_document(q['document'] + '.md', (DATA / doc['path']).read_bytes(), session_kb_id(sid))
                uploaded.append((rec['doc_id'], session_kb_id(sid)))
                kb._index_document(rec['doc_id'])
                mapped[q['document']] = rec['doc_id']
            for q in (q for q in questions if q['split'] == split and q['expected'] == 'answer'):
                evidence = retrieve_evidence(sid, q['question'])
                report['mixed_retrieval'].append({'id': q['id'], 'split': split,
                    'document_hit_at_5': any(e['doc_id'] == mapped[q['document']] for e in evidence),
                    'span_hit_at_5': answer_covered(q['answers'], evidence, mapped[q['document']]),
                    'top_score': evidence[0]['score'] if evidence else 0.})
                print('mixed', split, q['id'], report['mixed_retrieval'][-1]['span_hit_at_5'], flush=True)
                save(target / 'result.json', report)
        # 最终问答用现行门槛 .12：候选门槛仅是诊断，不把不可用的门槛写入产品。
        config._data['retrieval']['evidence_min_score'] = .12
        for split in ('calibration', 'holdout'):
            for expected in ('answer', 'refuse'):
                selected = [q for q in questions if q['split'] == split and q['expected'] == expected][:args.answers_per_class]
                for q in selected:
                    store = SessionStore()
                    engine = AgentEngine(store)
                    engine.session_id = 'public-' + q['document']
                    ts = time.perf_counter()
                    result = dict(id=q['id'], question=q['question'], split=split, expected=expected)
                    try:
                        text = await engine.chat(q['question'])
                        body = text.split('参考资料：')[0]
                        result.update(answer=text, mode=engine.messages[-1].get('meta', {}).get('mode'),
                                      gold_substring_hit=any(a in body for a in q['answers']))
                    except NeedsGeneralAnswer as error:
                        result.update(answer=str(error), mode='no_evidence', gold_substring_hit=False)
                    except Exception as error:
                        result.update(error=str(error), mode='error', gold_substring_hit=False)
                    finally:
                        store._db.close()
                        engine.mistakes.close()
                    result['seconds'] = round(time.perf_counter() - ts, 2)
                    report['answers'].append(result)
                    save(target / 'result.json', report)
                    print('answer', split, expected, result['mode'], result['seconds'], flush=True)
        report['status'] = 'complete'
    except BaseException as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        failures = []
        # 本脚本禁用了后台 worker，异常退出时没有在途任务；把同步索引中断记录
        # 转为可清理状态，避免 delete_document 因 processing 状态留下原件。
        try:
            kb._recover_stale()
        except Exception as error:
            failures.append({'stage': 'recover_interrupted_index', 'error': str(error)})
        for doc_id, scope in uploaded:
            try:
                kb.delete_document(doc_id, scope)
            except Exception as error:
                failures.append({'doc_id': doc_id, 'error': str(error)})
        report['cleanup'] = {'remaining_vectors': kb.collection.count(), 'failures': failures}
        report['seconds'] = round(time.perf_counter() - started, 2)
        save(target / 'result.json', report)
    print('RESULT', target / 'result.json', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--answers-per-class', type=int, default=6)
    asyncio.run(run(parser.parse_args()))

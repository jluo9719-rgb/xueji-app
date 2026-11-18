"""在三份公开资料上实跑换表达、部分覆盖、资料外问答及出题。
诊断集来自调参资料，独立于正式留出统计；所有答案保留供人工核对。
"""
import asyncio
import argparse
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT.parent / 'noim' / 'public-eval'


async def main(args):
    target = DATA / ('diagnostic-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6])
    target.mkdir(parents=True)
    os.environ['XUEJI_DATA_DIR'] = str(target / 'runtime')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    sys.path.insert(0, str(ROOT))
    from core.engine import AgentEngine
    from core.config import config
    from core.session import SessionStore
    from core.document_chat import NeedsGeneralAnswer, ANSWER_PROMPT_VERSION
    from core.exam_builder import generate_questions, review_questions, apply_reviews
    from rag.evidence import retrieve_evidence
    from rag.kb_manager import get_kb_manager, session_kb_id
    if args.answerability_check is not None:
        config._data['retrieval']['answerability_check'] = args.answerability_check == 'enabled'
    cases = json.loads(Path(__file__).with_name('diagnostic_cases.json').read_text(encoding='utf-8'))
    kb = get_kb_manager()
    kb._ensure_worker = lambda: None
    sid = 'public-diagnostics'
    scope = session_kb_id(sid)
    uploaded = []
    report = {'status': 'running', 'answers': [], 'exams': [], 'regressions': [], 'description': cases['description'],
              'answer_prompt_version': ANSWER_PROMPT_VERSION}
    report['answerability_check'] = config.get('retrieval.answerability_check', False)
    def save():
        (target / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('OUTPUT', target, flush=True)
    try:
        corpus = ''
        for source in cases['documents']:
            content = (DATA / 'documents' / (source['key'] + '.md')).read_bytes()
            corpus += content.decode('utf-8')
            doc = kb.add_document(source['name'], content, scope)
            uploaded.append((doc['doc_id'], scope))
            kb._index_document(doc['doc_id'])
        for case in cases['cases']:
            if any(term in corpus for term in case.get('absent_terms', [])):
                raise ValueError('预期资料外实体实际上存在，请复核诊断题')
            store = SessionStore()
            engine = AgentEngine(store)
            engine.session_id = sid
            engine.messages = []  # 每题独立；不能让前题答案污染诊断结果。
            result = dict(case)
            start = time.perf_counter()
            try:
                answer = await engine.chat(case['question'])
                result.update(answer=answer, meta=engine.messages[-1].get('meta', {}))
            except NeedsGeneralAnswer as error:
                result.update(answer=str(error), meta={'mode': 'no_evidence'})
            except Exception as error:
                result.update(error=str(error), meta={'mode': 'error'})
            finally:
                store._db.close()
                engine.mistakes.close()
            result['seconds'] = round(time.perf_counter() - start, 2)
            report['answers'].append(result)
            save()
            print('answer', len(report['answers']), case['category'], result['meta']['mode'], flush=True)
        if args.baseline:
            baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
            if baseline['status'] != 'complete':
                raise ValueError('对照运行未完成')
            questions = {q['id']: q for q in baseline['rows']}
            report['baseline'] = str(args.baseline.resolve())
            for previous in baseline['answers']:
                q = questions[previous['id']]
                regression_sid = 'regression-' + q['document']
                regression_scope = session_kb_id(regression_sid)
                content = (DATA / 'documents' / (q['document'] + '.md')).read_bytes()
                doc = kb.add_document(q['document'] + '.md', content, regression_scope)
                uploaded.append((doc['doc_id'], regression_scope))
                kb._index_document(doc['doc_id'])
                store = SessionStore()
                engine = AgentEngine(store)
                engine.session_id = regression_sid
                start = time.perf_counter()
                result = dict(id=q['id'], question=q['question'], split=q['split'], expected=q['expected'],
                              before_mode=previous['mode'], before_answer=previous.get('answer', ''))
                try:
                    answer = await engine.chat(q['question'])
                    result.update(answer=answer, mode=engine.messages[-1].get('meta', {}).get('mode'))
                except NeedsGeneralAnswer as error:
                    result.update(answer=str(error), mode='no_evidence')
                except Exception as error:
                    result.update(error=str(error), mode='error')
                finally:
                    store._db.close()
                    engine.mistakes.close()
                result['seconds'] = round(time.perf_counter() - start, 2)
                report['regressions'].append(result)
                save()
                print('regression', len(report['regressions']), result['before_mode'], result['mode'], flush=True)
        for source in cases['documents']:
            start = time.perf_counter()
            result = {'topic': source['topic'], 'requested': 3}
            try:
                chunks = retrieve_evidence(sid, source['topic'])
                generated = await generate_questions(chunks, count=3, context=source['topic'])
                reviews = await review_questions(generated, chunks)
                kept, rejected = apply_reviews(generated, reviews)
                result.update(count=len(kept), questions=kept, rejected=rejected)
            except Exception as error:
                result['error'] = str(error)
            result['seconds'] = round(time.perf_counter() - start, 2)
            report['exams'].append(result)
            save()
            print('exam', source['topic'], result.get('count'), result['seconds'], flush=True)
        report['status'] = 'complete'
    except BaseException as error:
        report.update(status='failed', error=str(error))
        raise
    finally:
        failures = []
        try:
            kb._recover_stale()
        except Exception as error:
            failures.append('recover_interrupted_index: ' + str(error))
        for doc_id, doc_scope in uploaded:
            try:
                kb.delete_document(doc_id, doc_scope)
            except Exception as error:
                failures.append(str(error))
        report['cleanup'] = {'remaining_vectors': kb.collection.count(), 'failures': failures}
        save()
    print('RESULT', target / 'result.json', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, help='对指定已完成基线中的真实问答逐题复测；只算回归，不算新留出')
    parser.add_argument('--answerability-check', choices=('enabled', 'disabled'), help='覆盖实验性核对开关，仅影响本次隔离评测')
    asyncio.run(main(parser.parse_args()))

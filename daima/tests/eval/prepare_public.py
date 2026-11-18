"""下载官方 DuReader Checklist；只读压缩包内指定 JSON，不执行下载脚本。

固定按原文哈希抽样，官方 train 用作调参、dev 用作留出。
原件、转出的 Markdown、问题与来源记录全部写入 noim/public-eval。
"""
import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT.parent / 'noim' / 'public-eval'
URL = 'https://dataset-bj.cdn.bcebos.com/lic2021/dureader_checklist.dataset.tar.gz'
SHA256 = 'd51553cfe1e025f8706e40417da065cbd097240ab2bf99077744489ada0e0ede'
SOURCE = 'https://github.com/baidu/DuReader/tree/master/DuReader-Checklist'


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def prepare():
    TARGET.mkdir(parents=True, exist_ok=True)
    archive = TARGET / 'dureader_checklist.dataset.tar.gz'
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=60) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError('官方数据包校验不一致；请人工核对来源版本，禁止静默替换评测集')
    datasets = {}
    with tarfile.open(archive) as bundle:
        for name in ('train', 'dev'):
            raw = bundle.extractfile(f'dataset/{name}.json').read()
            (TARGET / f'checklist-{name}.json').write_bytes(raw)
            datasets[name] = json.loads(raw)
    documents, questions = [], []
    seen_contexts, seen_questions = set(), set()
    for upstream, split in (('train', 'calibration'), ('dev', 'holdout')):
        candidates = []
        for item in datasets[upstream]['data']:
            for paragraph in item['paragraphs']:
                context = paragraph['context'].strip()
                if not 200 <= len(context) <= 5000:
                    continue
                for qa in paragraph['qas']:
                    candidates.append((digest(context), paragraph, qa))
        counts = {'answer': 0, 'refuse': 0}
        for fingerprint, paragraph, qa in sorted(candidates, key=lambda row: (row[0], row[2]['id'])):
            expected = 'refuse' if qa['is_impossible'] else 'answer'
            context = paragraph['context'].strip()
            question = qa['question'].strip()
            if counts[expected] >= 30 or fingerprint in seen_contexts or question in seen_questions:
                continue
            answers = list(dict.fromkeys(a['text'] for a in qa['answers'] if a['text']))
            if expected == 'answer' and (not answers or not all(a in context for a in answers)):
                continue
            doc_key = fingerprint[:20]
            # 标题来自网页，可能包含答案而正文无答案。为保持官方标注语义，标题只放清单，
            # 索引只包含原始 context，不能把原始问题、答案或网页标题混进检索资料。
            relpath = f'documents/{doc_key}.md'
            path = TARGET / relpath
            path.parent.mkdir(exist_ok=True)
            path.write_text(context, encoding='utf-8')
            documents.append({'key': doc_key, 'path': relpath, 'title': paragraph.get('title', ''),
                              'sha256': fingerprint, 'split': split, 'chars': len(context)})
            questions.append({'id': qa['id'], 'source': 'DuReader-Checklist', 'upstream_split': upstream,
                              'split': split, 'document': doc_key, 'category': 'in' if answers else 'out_related',
                              'question': question, 'expected': expected, 'answers': answers,
                              'expected_section': doc_key if answers else None,
                              'annotation_type': qa.get('type', 'original'), 'label_origin': 'upstream'})
            seen_contexts.add(fingerprint)
            seen_questions.add(question)
            counts[expected] += 1
        if counts != {'answer': 30, 'refuse': 30}:
            raise ValueError(f'样本不足：{split} {counts}')
    manifest = {'source_url': SOURCE, 'archive_url': URL, 'archive_sha256': SHA256,
                'selection': 'context SHA256 ascending; 200..5000 chars; 30 answer + 30 refuse per split',
                'limitations': ['段落级公开阅读理解材料，不是备考教材', '非官方排行榜评测',
                                '原文按来源去重，未保证所有相近主题不跨分组', '原始标注可能有歧义，保留原标注并单列人工复核'],
                'documents': documents}
    (TARGET / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (TARGET / 'questions.jsonl').write_text(''.join(json.dumps(q, ensure_ascii=False) + '\n' for q in questions), encoding='utf-8')
    print(f'Prepared {len(documents)} documents, {len(questions)} questions: {TARGET}', flush=True)


if __name__ == '__main__':
    prepare()

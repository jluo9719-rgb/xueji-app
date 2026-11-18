"""直接用三份公开原文验证出题和引文校验；不重复加载检索模型，不计作检索成绩。"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT.parent / 'noim' / 'public-eval'


async def main():
    target = DATA / ('exam-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6])
    target.mkdir(parents=True)
    os.environ['XUEJI_DATA_DIR'] = str(target / 'runtime')
    sys.path.insert(0, str(ROOT))
    from core import exam_builder as exam
    sources = json.loads(Path(__file__).with_name('diagnostic_cases.json').read_text(encoding='utf-8'))['documents']
    report = {'mode': 'direct_public_source_generation_not_retrieval', 'status': 'running', 'exams': [], 'calls': []}
    real_call = exam.acall_llm
    def save():
        (target / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    async def recorded_call(*args, **kwargs):
        response = await real_call(*args, **kwargs)
        report['calls'].append({'system': kwargs.get('system'), 'output': exam.extract_text(response),
                                'stop_reason': getattr(response, 'stop_reason', None)})
        save()
        return response
    exam.acall_llm = recorded_call
    print('OUTPUT', target, flush=True)
    try:
        for source in sources:
            text = (DATA / 'documents' / (source['key'] + '.md')).read_text(encoding='utf-8')
            chunks = [{'ref': 1, 'text': text, 'name': source['name'], 'chunk': 1,
                       'doc_id': source['key'], 'chunk_id': source['key'] + '_direct', 'metadata': {}}]
            result = {'topic': source['topic'], 'requested': 3}
            start = time.perf_counter()
            try:
                questions = await exam.generate_questions(chunks, count=3, context=source['topic'])
                kept, rejected = exam.apply_reviews(questions, await exam.review_questions(questions, chunks))
                result.update(accepted=len(kept), questions=kept, rejected=rejected)
            except Exception as error:
                result['error'] = str(error)
            result['seconds'] = round(time.perf_counter() - start, 2)
            report['exams'].append(result)
            save()
            print(source['topic'], result.get('accepted'), result['seconds'], flush=True)
        report['status'] = 'complete'
    finally:
        exam.acall_llm = real_call
        save()
    print('RESULT', target / 'result.json', flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    asyncio.run(main())

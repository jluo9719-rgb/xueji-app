"""真实 Chroma + 嵌入 + 重排评测。只写 noim，原文只读。
用法：python -B tests/eval/run.py --document 文件路径 --questions questions.jsonl
输入每行：category, question, expected_section(可空), expected(answer/refuse/partial)。
这是一轮诊断，不自动把同一评测集选出的阈值称为留出集效果。
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
TARGET=ROOT.parent/'noim'/('evaluation-'+str(int(time.time())))
TARGET.mkdir(parents=True,exist_ok=True)
os.environ['XUEJI_DATA_DIR']=str(TARGET)
os.environ['HF_HUB_OFFLINE']='1'
sys.path.insert(0,str(ROOT))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document',required=True)
    parser.add_argument('--questions',default=str(Path(__file__).with_name('questions.jsonl')))
    args=parser.parse_args()
    from core.config import config
    from rag.kb_manager import get_kb_manager, session_kb_id
    from rag.evidence import retrieve_evidence
    kb=get_kb_manager();kb._ensure_worker=lambda:None
    source=Path(args.document)
    doc=kb.add_document(source.name,source.read_bytes(),session_kb_id('evaluation'))
    kb._index_document(doc['doc_id'])
    config._data['retrieval']['evidence_min_score']=0.0
    rows=[]
    try:
        for line in Path(args.questions).read_text(encoding='utf-8').splitlines():
            if not line.strip():continue
            question=json.loads(line)
            evidence=retrieve_evidence('evaluation',question['question'])
            section=question.get('expected_section')
            rows.append(dict(question,top_score=evidence[0]['score'] if evidence else 0,
                             section_hit=any(section in e['text'] for e in evidence) if section else None,
                             top_chunks=[{'id':e['chunk_id'],'score':e['score'],'text':e['text']} for e in evidence]))
        sweep=[]
        positives=[r for r in rows if r['expected']=='answer']
        negatives=[r for r in rows if r['expected']=='refuse']
        for threshold in (.02,.05,.1,.12,.2,.3,.5,.7):
            sweep.append({'threshold':threshold,'in_pass':sum(r['top_score']>=threshold for r in positives),
                          'in_total':len(positives),'out_false_pass':sum(r['top_score']>=threshold for r in negatives),
                          'out_total':len(negatives)})
        report={'warning':'单资料诊断；未做调参集/留出集拆分，不是泛化准确率。未标注章节不统计命中率。',
                'document':source.name,'rows':rows,'threshold_sweep':sweep}
        (TARGET/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('Result:',TARGET/'result.json')
    finally:
        kb.delete_document(doc['doc_id'],session_kb_id('evaluation'))

if __name__=='__main__':main()

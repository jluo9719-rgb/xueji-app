import os
assert os.environ.get('XUEJI_DATA_DIR'), '请使用 python -B tests/run.py，避免写入正式数据'
import asyncio, tempfile, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.config import config
root=Path(os.environ['XUEJI_DATA_DIR'])
config._data['storage'].update(data_dir=str(root/'data'),cache_dir=str(root/'cache'),chroma_path=str(root/'chroma'),sessions_db=str(root/'sessions.db'),sqlite_path=str(root/'memory.db'),mistakes_db=str(root/'mistakes.db'))
config._data['redis']['enabled']=False
# 旧契约用例断言"无依据时完全不调用模型"，与追问补全的额外改写调用冲突；
# 这里统一关闭补全，文件末尾有专项用例单独验证补全行为。
config._data.setdefault('retrieval',{})['context_rewrite']=False
# 本文件验证检索与隔离顺序；新增可回答性检查由 test_reliability 专项覆盖。
config._data['retrieval']['answerability_check']=False
from core.session import SessionStore
from rag.kb_manager import KBManager,session_kb_id
from core.document_chat import answer_from_documents, NeedsGeneralAnswer
from core.engine import AgentEngine
from api.app import app
import httpx

async def main():
    store=SessionStore();store.create_session('math','数学');store.rename_session('math','高等数学');store.archive_session('math',True)
    other=SessionStore();assert other.list_sessions()[0]['archived'];assert other.list_sessions()[0]['title']=='高等数学'
    print('PASS session rename/archive persistence')
    kb=KBManager();kb._ensure_worker=lambda:None
    a=kb.add_document('a.txt',b'alpha',session_kb_id('math'))
    b=kb.add_document('a.txt',b'alpha',session_kb_id('english'))
    assert a['doc_id']!=b['doc_id']
    for d in (a,b):
        kb._update(d['doc_id'],status='ready',chunk_count=1)
        kb.collection.add(ids=[d['doc_id']+'_0'],documents=['test content'],embeddings=[[1.,0.,0.]],metadatas=[{'kb_id':d['kb_id'],'doc_id':d['doc_id']}])
    assert len(kb.list_documents(session_kb_id('math')))==1
    from core.document_chat import retrieve_evidence
    with patch('rag.evidence.get_kb_manager',return_value=kb),patch('rag.kb_manager.get_embedder',return_value=NS(encode=lambda queries:[[1.,0.,0.]])),patch('rag.retriever.Reranker.instance',return_value=NS(score=lambda query,docs:[1.0]*len(docs))):
        evidence_rows = retrieve_evidence('math','query')
        assert len(evidence_rows)==1 and evidence_rows[0]['text']=='test content'
        assert retrieve_evidence('unused','query')==[]
    print('PASS actual vector query applies session/document filters')
    kb.get_snapshot(a['kb_id']);assert list((root/'cache').glob('*.pkl'))
    assert kb.delete_document(a['doc_id'],b['kb_id']) is False
    assert kb.delete_document(a['doc_id'],a['kb_id'])
    assert not kb.collection.get(where={'doc_id':a['doc_id']},include=[])['ids']
    assert not (root/'data/uploads'/(a['doc_id']+'.txt')).exists()
    assert not kb._snapshots and not list((root/'cache').glob('*.pkl'))
    assert kb.collection.get(where={'doc_id':b['doc_id']},include=[])['ids']
    assert not kb.list_documents(a['kb_id'])
    print('PASS scoped upload and real Chroma deletion, originals/cache removed, other scope intact')
    # Failed delete must not report success or remove original/registry.
    with patch.object(kb.collection,'delete',side_effect=RuntimeError('test failure')):
        try:kb.delete_document(b['doc_id'],b['kb_id'])
        except RuntimeError:pass
        else:raise AssertionError('delete failure swallowed')
    assert kb.list_documents(b['kb_id']) and (root/'data/uploads'/(b['doc_id']+'.txt')).exists()
    print('PASS delete failure propagated')
    engine=NS(session_id='empty',messages=[],session=store)
    llm=AsyncMock(return_value=NS(content=[NS(type='text',text='答案')],stop_reason='end_turn'))
    with patch('core.document_chat.get_kb_manager',return_value=kb),patch('core.document_chat.acall_llm',llm):
        try:await answer_from_documents(engine,'问题')
        except NeedsGeneralAnswer:pass
        else:raise AssertionError('empty documents should ask')
        llm.assert_not_called()
        answer=await answer_from_documents(engine,'问题',True)
        assert '通用知识' in answer and llm.await_count==1
    engine.session_id='english'
    with patch('core.document_chat.get_kb_manager',return_value=kb),patch('core.document_chat.retrieve_evidence',return_value=[]),patch('core.document_chat.acall_llm',AsyncMock()) as model:
        try:await answer_from_documents(engine,'无关问题')
        except NeedsGeneralAnswer:pass
        else:raise AssertionError('no evidence should ask')
        model.assert_not_called()
    order=[]
    def evidence(*args):
        order.append('retrieve')
        return [{'text':'只支持平方运算','name':'数学.txt','chunk':1,'doc_id':'d','chunk_id':'d_r_0'}]
    async def grounded(*args, **kwargs):
        order.append('llm')
        assert '数学.txt' in args[0][0]['content']
        return NS(content=[NS(type='text',text='资料说明平方运算[1]')],stop_reason='end_turn')
    with patch('core.document_chat.get_kb_manager',return_value=kb),patch('core.document_chat.retrieve_evidence',side_effect=evidence),patch('core.document_chat.acall_llm',side_effect=grounded):
        result=await answer_from_documents(engine,'资料支持什么')
        assert order==['retrieve','llm'] and '参考资料' in result
    with patch('core.document_chat.get_kb_manager',return_value=kb),patch('core.document_chat.retrieve_evidence',side_effect=evidence),patch('core.document_chat.acall_llm',AsyncMock(return_value=NS(content=[NS(type='text',text='NO_DOCUMENT_EVIDENCE')],stop_reason='end_turn'))):
        try:await answer_from_documents(engine,'资料之外')
        except NeedsGeneralAnswer:pass
        else:raise AssertionError('insufficient evidence should require consent')
    print('PASS document-first order, citations, empty/miss/sentinel gating; explicit general consent')
    # 追问补全：有历史对话时，代词追问先被改写为独立问题，再用于检索与作答
    config._data['retrieval']['context_rewrite']=True
    engine2=NS(session_id='english',messages=[{'role':'user','content':'子公司的管理制度是什么？'},{'role':'assistant','content':'依据资料：子公司实行分级管理。'}],session=store)
    seen={}; calls=[]
    def evidence_rewrite(session_id,q):
        seen['q']=q
        return [{'text':'授权审批需两级签字','name':'制度.txt','chunk':2,'doc_id':'d','chunk_id':'d_r_1'}]
    async def stub_llm(messages,**kwargs):
        calls.append(messages)
        if len(calls)==1:
            return NS(content=[NS(type='text',text='子公司的授权审批有什么要求？')],stop_reason='end_turn')
        return NS(content=[NS(type='text',text='资料说明：授权审批需两级签字[1]')],stop_reason='end_turn')
    with patch('core.document_chat.get_kb_manager',return_value=kb),patch('core.document_chat.retrieve_evidence',side_effect=evidence_rewrite),patch('core.document_chat.acall_llm',side_effect=stub_llm):
        result=await answer_from_documents(engine2,'它的授权审批有什么要求？')
    assert seen['q']=='子公司的授权审批有什么要求？'
    assert '问题：子公司的授权审批有什么要求？' in calls[1][0]['content']
    assert '两级签字' in result and '制度.txt' in result and '【依据当前对话资料】' in result
    config._data['retrieval']['context_rewrite']=False
    print('PASS follow-up question rewritten to standalone query before retrieval and answering')
    with patch('api.app.get_kb_manager',return_value=kb),patch('core.document_chat.get_kb_manager',return_value=kb):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
            r=await c.post('/sessions',json={'session_id':'new-test','title':'新对话'});assert r.status_code==200,r.text
            r=await c.get('/documents',params={'session_id':'new-test'});assert r.json()['docs']==[]
            r=await c.patch('/sessions/new-test',json={'title':'物理','archived':True});assert r.status_code==200
            r=await c.patch('/sessions/new-test',json={'archived':False});assert r.status_code==200
            r=await c.post('/chat',json={'session_id':'new-test','message':'资料问题'});assert 'event: needs_general' in r.text and 'event: answer' not in r.text
            r=await c.delete('/sessions/new-test');assert r.status_code==200,r.text
            r=await c.get('/sessions');assert not any(s['id']=='new-test' for s in r.json()['sessions'])
    print('PASS API create empty session, rename/archive/restore/delete and consent SSE')
    print('ALL PASS; isolated artifacts:',root)
asyncio.run(main())

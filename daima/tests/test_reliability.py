import os
assert os.environ.get('XUEJI_DATA_DIR'), '请通过 tests/run.py 运行'
import asyncio
import copy
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.config import config
from core.document_chat import NeedsGeneralAnswer, answer_from_documents, _recent_pairs, _parse_answerability, _check_answerability
from core.engine import AgentEngine
from core.exam_builder import generate_questions, review_questions, apply_reviews, validate_question, exam_response
from core.session import SessionStore
from rag.chunker import chunk_document
from rag.loader import load_document
from rag.kb_manager import KBManager, session_kb_id
from rag.evidence import retrieve_evidence
from rag.text_match import quote_in_source


def response(text, stop='end_turn'):
    return NS(content=[NS(type='text',text=text)],stop_reason=stop)


EVIDENCE = [{'ref':1,'doc_id':'d','chunk_id':'d_rev_0','text':'采购审批需要两级签字。','name':'讲义.md','chunk':1,
             'metadata':{'doc_name':'讲义.md'}},
            {'ref':2,'doc_id':'e','chunk_id':'e_rev_0','text':'付款审批需要三级签字。','name':'制度.md','chunk':2,
             'metadata':{'doc_name':'制度.md'}}]
QUESTION = {'type':'short','question':'付款审批需要几级签字？','answer':'三级',
            'explanation':'付款审批需要三级签字。','evidence_ref':2,'quote':'付款审批需要三级签字。'}


class Reliability(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original = copy.deepcopy(config._data)
        config._data['retrieval']['context_rewrite'] = False
        config._data['retrieval']['answerability_check'] = True
        self.store = SessionStore()
        self.engine = AgentEngine(self.store)
        self.engine.session_id = self.id()
        self.kb = NS(list_documents=lambda scope:[{'status':'ready','doc_id':'d'}])
        assessment = patch('core.document_chat._check_answerability', AsyncMock(return_value={'status':'supported','refs':[1]}))
        assessment.start()
        self.addCleanup(assessment.stop)

    def tearDown(self):
        self.store._db.close()
        self.engine.mistakes.close()
        config._data.clear(); config._data.update(self.original)

    def test_data_isolation(self):
        root = Path(os.environ['XUEJI_DATA_DIR'])
        for key in ('data_dir','cache_dir','chroma_path','sessions_db','sqlite_path','mistakes_db'):
            self.assertTrue(Path(config.get('storage.'+key)).is_relative_to(root))
        self.assertTrue(Path(config.get('logging.dir')).is_relative_to(root))
        self.assertFalse(config.get('redis.enabled'))

    def test_chunk_bounds_and_headings(self):
        text = '# 采购制度\n简介\n### 审批职责\n' + '审批须复核。'*1000 + '\n### 付款职责\n必须三级签字。'
        chunks=chunk_document(None,text)
        self.assertTrue(chunks)
        self.assertTrue(all(len(c)<=800 for c in chunks))
        self.assertIn('审批职责', ''.join(chunks))
        self.assertIn('必须三级签字', ''.join(chunks))
        self.assertEqual(''.join(chunks).count('审批须复核。'),1000)

    def test_gbk_and_empty(self):
        path=Path(os.environ['XUEJI_DATA_DIR'])/'test.txt'
        path.write_bytes('审计学习资料'.encode('gbk'))
        self.assertEqual(load_document(str(path)),'审计学习资料')
        path.write_bytes(b' \n')
        with self.assertRaisesRegex(ValueError,'文字'): load_document(str(path))

    def test_registry_transient_lock_retry_and_permanent_error(self):
        kb = KBManager()
        replace = os.replace
        count = 0
        def briefly_locked(source, target):
            nonlocal count
            count += 1
            if count < 3:
                raise PermissionError('file temporarily busy')
            replace(source, target)
        with patch('rag.kb_manager.os.replace', side_effect=briefly_locked), \
             patch('rag.kb_manager.time.sleep'):
            kb._save_registry()
        self.assertEqual(count, 3)
        before = Path(kb.registry_path).read_bytes()
        with patch('rag.kb_manager.os.replace', side_effect=PermissionError('busy')) as operation, \
             patch('rag.kb_manager.time.sleep'):
            with self.assertRaises(PermissionError):
                kb._save_registry()
        self.assertEqual(operation.call_count, 5)
        self.assertEqual(Path(kb.registry_path).read_bytes(), before)

    def test_scanned_pdf(self):
        path=Path(os.environ['XUEJI_DATA_DIR'])/'scan.pdf';path.write_bytes(b'placeholder')
        with patch.dict(sys.modules,{'pymupdf4llm':NS(to_markdown=lambda p:'')}):
            with self.assertRaisesRegex(ValueError,'OCR'):load_document(str(path))

    async def test_no_evidence_persisted_and_general_no_duplicate(self):
        with patch('core.document_chat.get_kb_manager',return_value=NS(list_documents=lambda scope:[])), \
             patch('core.document_chat.acall_llm',AsyncMock(return_value=response('模型一般知识'))) as model:
            with self.assertRaises(NeedsGeneralAnswer):await self.engine.chat('问题')
            model.assert_not_called()
            saved=self.store.load_messages(self.engine.session_id)
            self.assertEqual(saved[-1]['meta']['mode'],'no_evidence')
            self.assertEqual(_recent_pairs(saved),[])
            await self.engine.chat('问题',allow_general=True)
            self.assertEqual(sum(m['role']=='user' for m in self.engine.messages),1)
            self.assertEqual(self.engine.messages[-1]['meta']['mode'],'general')
            self.assertEqual(_recent_pairs(self.engine.messages)[-1][0], '问题')

    async def test_upload_size_and_no_evidence_capture(self):
        import httpx
        from api.app import app
        config._data['storage']['upload_max_bytes']=4
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            result=await client.post('/documents',files={'file':('large.txt',b'12345')})
            self.assertEqual(result.status_code,413)
            with patch('core.document_chat.get_kb_manager',return_value=NS(list_documents=lambda scope:[])):
                await client.post('/chat/sync',json={'session_id':'no-evidence-capture','message':'问题'})
            result=await client.post('/mistakes',json={'session_id':'no-evidence-capture'})
            self.assertEqual(result.status_code,400)

    async def test_citation_repair_and_only_used_refs(self):
        with patch('core.document_chat.get_kb_manager',return_value=self.kb), \
             patch('core.document_chat.retrieve_evidence',return_value=EVIDENCE), \
             patch('core.document_chat.acall_llm',AsyncMock(side_effect=[response('三级[9]'),response('三级[2]')])) as model:
            answer=await self.engine.chat('付款几级')
        self.assertEqual(model.await_count,2)
        self.assertNotIn('[9]',answer)
        self.assertIn('制度.md',answer)
        self.assertNotIn('讲义.md',answer)
        self.assertEqual(self.engine.messages[-1]['meta']['evidence'][0]['chunk_id'],'e_rev_0')

    def test_answerability_quotes_and_missing_parts(self):
        data = {'requested_fact':'付款审批级数', 'document_fact':'付款需三级', 'status':'supported',
                'quotes':[{'ref':2, 'quote':'付款审批需要三级签字。'}]}
        result = _parse_answerability(json.dumps(data), EVIDENCE)
        self.assertEqual(result['refs'], [2])
        for bad in (dict(data, quotes=[{'ref':1,'quote':'付款审批需要三级签字。'}]),
                    dict(data, quotes=[{'ref':True,'quote':'采购审批需要两级签字。'}]),
                    dict(data, status='partial'), dict(data, quotes=[])):
            with self.assertRaises(ValueError):
                _parse_answerability(json.dumps(bad), EVIDENCE)
        partial = _parse_answerability(json.dumps(dict(data,status='partial',missing='生日未知')),EVIDENCE)
        self.assertEqual(partial['status'], 'partial')

    async def test_unsupported_or_invalid_check_never_generates_answer(self):
        for checker in (AsyncMock(return_value={'status':'unsupported','refs':[]}),
                        AsyncMock(side_effect=ValueError('bad quote'))):
            with patch('core.document_chat.get_kb_manager',return_value=self.kb), \
                 patch('core.document_chat.retrieve_evidence',return_value=EVIDENCE), \
                 patch('core.document_chat._check_answerability',checker), \
                 patch('core.document_chat.acall_llm',AsyncMock()) as generator:
                with self.assertRaises(NeedsGeneralAnswer):
                    await self.engine.chat('缺少关键答案的问题')
                generator.assert_not_called()
                self.assertEqual(self.engine.messages[-1]['meta']['mode'], 'no_evidence')

    async def test_answerability_calls_real_parser_and_detects_truncation(self):
        valid = {'requested_fact':'采购审批级数','document_fact':'需要两级','status':'supported',
                 'quotes':[{'ref':1,'quote':'采购审批需要两级签字。'}]}
        with patch('core.document_chat.acall_llm',AsyncMock(return_value=response(json.dumps(valid)))):
            self.assertEqual((await _check_answerability('采购审批几级',EVIDENCE))['refs'],[1])
        with patch('core.document_chat.acall_llm',AsyncMock(return_value=response(json.dumps(valid),'max_tokens'))):
            with self.assertRaises(ValueError):await _check_answerability('采购审批几级',EVIDENCE)

    async def test_missing_citations_not_grounded(self):
        with patch('core.document_chat.get_kb_manager',return_value=self.kb), \
             patch('core.document_chat.retrieve_evidence',return_value=EVIDENCE), \
             patch('core.document_chat.acall_llm',AsyncMock(return_value=response('三级'))):
            answer=await self.engine.chat('付款几级')
        self.assertIn('未验证',answer)
        self.assertNotIn('参考资料',answer)

    async def test_partial_sentinel_not_refusal(self):
        with patch('core.document_chat.get_kb_manager',return_value=self.kb), \
             patch('core.document_chat.retrieve_evidence',return_value=EVIDENCE), \
             patch('core.document_chat.acall_llm',AsyncMock(return_value=response('NO_DOCUMENT_EVIDENCE\n审批三级[2]，其他内容资料未覆盖。'))):
            answer=await self.engine.chat('付款几级，另一个公司呢')
        self.assertNotIn('NO_DOCUMENT_EVIDENCE',answer)
        self.assertIn('未覆盖',answer)

    async def test_empty_truncation_and_cancel_rollback(self):
        for result in (response(''),response('未完',stop='max_tokens')):
            with patch('core.document_chat.acall_llm',AsyncMock(return_value=result)):
                with self.assertRaises(RuntimeError):await self.engine.chat('问题',allow_general=True)
            self.assertEqual(self.engine.messages,[])
        async def slow(*a,**kw):await asyncio.sleep(10)
        with patch('core.document_chat.acall_llm',slow):
            task=asyncio.create_task(self.engine.chat('停止',allow_general=True))
            await asyncio.sleep(.01);task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.engine.messages,[])

    def test_exam_validation_matches_own_source(self):
        q=validate_question(QUESTION,EVIDENCE)
        self.assertEqual(q['source']['doc_id'],'e')
        for changes in ({'evidence_ref':9},{'quote':'并不存在的引文'},{'type':'unknown'},
                        {'type':'choice','options':['a','b','c'],'answer':'A'}):
            with self.assertRaises(ValueError):validate_question(dict(QUESTION,**changes),EVIDENCE)

    def test_quote_typography_does_not_change_numbers_or_facts(self):
        self.assertTrue(quote_in_source('“比例是0.5”，不可增加。', '比例是0.5,不可增加.'))
        self.assertFalse(quote_in_source('比例是05', '比例是0.5'))
        self.assertFalse(quote_in_source('可以增加', '不可增加'))
        self.assertFalse(quote_in_source('需要三级签字', '需要两级签字'))
        self.assertFalse(quote_in_source('“ ”', '完整原文'))

    async def test_exam_json_retry_and_rejections(self):
        with patch('core.exam_builder.acall_llm',AsyncMock(side_effect=[response('坏 JSON'),response(json.dumps([QUESTION,dict(QUESTION,quote='错引文')],ensure_ascii=False))])) as model:
            qs=await generate_questions(EVIDENCE,count=2)
        self.assertEqual(model.await_count,2);self.assertEqual(len(qs),1);self.assertEqual(len(qs.rejected),1)
        with patch('core.exam_builder.acall_llm',AsyncMock(return_value=response('{}'))):
            with self.assertRaisesRegex(ValueError,'格式错误'):await generate_questions(EVIDENCE)

    async def test_exam_schema_retry_keeps_valid_and_rechecks_sources(self):
        good = dict(QUESTION)
        bad = dict(QUESTION, evidence_ref=9)
        fixed = dict(QUESTION, question='付款审批的签字层级数？')
        with patch('core.exam_builder.acall_llm', AsyncMock(side_effect=[
            response(json.dumps([good, bad])), response(json.dumps([bad, fixed]))])) as model:
            questions = await generate_questions(EVIDENCE, count=2)
        self.assertEqual(model.await_count, 2)
        self.assertEqual(len(questions), 2)
        self.assertEqual(questions[0]['question'], good['question'])
        self.assertEqual(questions[1]['source']['doc_id'], 'e')
        self.assertEqual(questions.rejected, [])
        with patch('core.exam_builder.acall_llm', AsyncMock(return_value=response(json.dumps([bad])))) as model:
            questions = await generate_questions(EVIDENCE, count=1)
        self.assertEqual(model.await_count, 2)
        self.assertFalse(questions)
        self.assertIn('越界', questions.rejected[0]['reason'])

    async def test_exam_repair_failure_retains_only_valid_questions(self):
        with patch('core.exam_builder.acall_llm', AsyncMock(side_effect=[
            response(json.dumps([QUESTION])), RuntimeError('offline')])):
            questions = await generate_questions(EVIDENCE, count=2)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions.rejected, [{'index': 2, 'reason': '模型未生成此题'}])

    async def test_review_failure_and_mismatch_are_unreviewed(self):
        qs=[validate_question(QUESTION,EVIDENCE)]
        for mock in (AsyncMock(side_effect=RuntimeError('offline')),AsyncMock(return_value=response('[]'))):
            with patch('core.exam_builder.acall_llm',mock):reviews=await review_questions(qs,EVIDENCE)
            kept,rejected=apply_reviews(qs,reviews)
            self.assertEqual(len(kept),1)
            self.assertEqual(kept[0]['review_status'],'unreviewed')
            result=exam_response(kept,'试卷',1,rejected)
            self.assertNotIn('付款审批需要三级签字。',result['question_markdown'])
            self.assertIn('三级',result['answer_markdown'])
            self.assertEqual(result['review_status'],'unreviewed')
        kept,rejected=apply_reviews(qs,[{'valid':False,'reason':'答案错误','review_status':'rejected'}])
        self.assertEqual(kept,[]);self.assertEqual(len(rejected),1)

    async def test_real_chroma_rebuild_and_threshold(self):
        kb=KBManager();kb._ensure_worker=lambda:None
        scope=session_kb_id(self.id())
        d=kb.add_document('lecture.txt','审批需要两级签字。'.encode(),scope)
        embed=NS(encode=lambda texts:[[1.,0.,0.] for _ in texts])
        with patch('rag.kb_manager.get_embedder',return_value=embed):kb._index_document(d['doc_id'])
        ids1=kb.collection.get(where={'doc_id':d['doc_id']},include=[])['ids']
        with patch('rag.evidence.get_kb_manager',return_value=kb), \
             patch('rag.kb_manager.get_embedder',return_value=embed), \
             patch('rag.retriever.Reranker.instance',return_value=NS(score=lambda q,ds:[.119]*len(ds))):
            self.assertEqual(retrieve_evidence(self.id(),'审批'),[])
        with patch('rag.evidence.get_kb_manager',return_value=kb), \
             patch('rag.kb_manager.get_embedder',return_value=embed), \
             patch('rag.retriever.Reranker.instance',return_value=NS(score=lambda q,ds:[.121]*len(ds))):
            rows=retrieve_evidence(self.id(),'审批')
            self.assertEqual(rows[0]['chunk_id'],ids1[0])
        kb.rebuild_document(d['doc_id'],scope)
        with patch('rag.kb_manager.get_embedder',return_value=embed):kb._index_document(d['doc_id'])
        ids2=kb.collection.get(where={'doc_id':d['doc_id']},include=[])['ids']
        self.assertTrue(set(ids1).isdisjoint(ids2))
        kb.get_snapshot(scope)
        self.assertTrue(kb.delete_document(d['doc_id'],scope))
        self.assertFalse(kb.collection.get(where={'doc_id':d['doc_id']},include=[])['ids'])
        self.assertFalse(list(Path(kb.cache_dir).glob('kb_*_bm25.pkl')))


if __name__=='__main__':unittest.main(verbosity=2)

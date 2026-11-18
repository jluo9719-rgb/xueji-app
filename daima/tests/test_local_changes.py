import os
assert os.environ.get('XUEJI_DATA_DIR'), '请使用 python -B tests/run.py，避免写入正式数据'
import asyncio, copy, sys, unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.config import config
from core.llm import _build_kwargs
from core.context import ContextCompressor
from core.legacy_engine import AgentEngine


def text(value='完成'):
    return NS(type='text',text=value)
def tool():
    return NS(type='tool_use',name='multi_retrieve',id='tool_1',input={'query':'知识点'})
def response(*blocks, stop='end_turn'):
    return NS(content=list(blocks),stop_reason=stop)

class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original=copy.deepcopy(config._data)
        self.patches=[patch('core.legacy_engine.LongTermMemory'),patch('core.legacy_engine.MistakeBook'),patch('core.legacy_engine.get_client')]
        for p in self.patches:p.start()
        self.store=MagicMock()
        self.engine=AgentEngine(self.store)
        self.engine.memory.load_memory_text.return_value=''
        self.engine.setup_tools()
        self.engine.compressor=NS(compress=lambda x:x,force_window=lambda x:x)
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        config._data.clear();config._data.update(self.original)

    # ---- 旧工具自治流程（_chat_impl，历史实现，非默认入口）----
    async def test_plain_answer_one_call(self):
        with patch('core.legacy_engine.acall_llm',new=AsyncMock(return_value=response(text()))) as llm:
            self.assertEqual(await self.engine._chat_impl('你好'),'完成')
            self.assertEqual(llm.await_count,1)
            self.store.save_messages.assert_called_once()
    async def test_mixed_tool_and_text_and_budget(self):
        self.engine.max_tool_calls=1
        self.engine._execute_tools=AsyncMock(return_value=[self.engine._tool_result('tool_1','资料')])
        with patch('core.legacy_engine.acall_llm',new=AsyncMock(side_effect=[response(tool(),text('正在查')),response(text('最终回答'))])) as llm:
            self.assertEqual(await self.engine._chat_impl('查询资料'),'最终回答')
            self.engine._execute_tools.assert_awaited_once()
            self.assertIsNone(llm.call_args_list[1].kwargs['tools'])
    async def test_truncated_output_is_error(self):
        # 直连 _chat_impl 无 chat() 的回滚包装：报错时本轮 user 消息留在列表里
        with patch('core.legacy_engine.acall_llm',new=AsyncMock(return_value=response(text('半截'),stop='max_tokens'))):
            with self.assertRaisesRegex(RuntimeError,'max_tokens'):await self.engine._chat_impl('问题')
        self.assertEqual(len(self.engine.messages),1)
    async def test_budget_ignoring_model_is_error(self):
        self.engine.max_tool_calls=0
        with patch('core.legacy_engine.acall_llm',new=AsyncMock(return_value=response(tool()))):
            with self.assertRaisesRegex(RuntimeError,'预算'):await self.engine._chat_impl('问题')
        self.assertEqual(len(self.engine.messages),1)

    # ---- chat() 包装层：默认调度资料优先入口，验证失败回滚/超时/串行 ----
    async def test_empty_response_rolls_back(self):
        self.engine.messages=[{'role':'user','content':'旧问题'},{'role':'assistant','content':'旧回答'}]
        before=copy.deepcopy(self.engine.messages)
        with patch('core.document_chat.answer_from_documents',new=AsyncMock(side_effect=RuntimeError('模型返回空内容，请检查模型状态、thinking 和 max_tokens 配置'))):
            with self.assertRaisesRegex(RuntimeError,'空内容'):await self.engine.chat('新问题')
        self.assertEqual(self.engine.messages,before)
        self.store.save_messages.assert_not_called()
    async def test_timeout_rollback(self):
        config._data['llm']['chat_timeout']=0.03
        async def slow(*args,**kwargs):await asyncio.sleep(1)
        with patch('core.document_chat.answer_from_documents',new=slow):
            with self.assertRaises(asyncio.TimeoutError):await self.engine.chat('问题')
        self.assertEqual(self.engine.messages,[])
    async def test_session_calls_serialized(self):
        active=0; peak=0
        async def fake_answer(engine,query,allow_general=False,notify=None):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            await asyncio.sleep(.01)
            active-=1
            engine.messages.extend([{'role':'user','content':query},{'role':'assistant','content':'答'}])
            return '答'
        with patch('core.document_chat.answer_from_documents',new=fake_answer):
            await asyncio.gather(self.engine.chat('一'),self.engine.chat('二'))
        self.assertEqual(peak,1)
        self.assertEqual(len(self.engine.messages),4)

    def test_system_summary_moved_to_top_level(self):
        kw=_build_kwargs([{'role':'system','content':'摘要'},{'role':'user','content':'问题'}],'规则')
        self.assertEqual(kw['system'],'规则\n\n摘要')
        self.assertEqual(len(kw['messages']),1)
        self.assertEqual(kw['thinking'],{'type':'disabled'})
    def test_compression_preserves_tool_pair(self):
        messages=[{'role':'user','content':'问题'},{'role':'assistant','content':[{'type':'tool_use','id':'x'}]},
                  {'role':'user','content':[{'type':'tool_result','tool_use_id':'x'}]}]
        self.assertEqual(ContextCompressor._safe_cut(messages,2),0)
    async def test_disabled_tool_is_not_executed(self):
        result=await self.engine._execute_tools([{'id':'x','name':'search_google_retry','input':{'query':'test'}}])
        self.assertIn('未知工具',result[0]['content'][0]['content'])
    async def test_bad_tool_arguments(self):
        result=await self.engine._execute_tools([{'id':'x','name':'multi_retrieve','input':'bad'}])
        self.assertIn('参数校验错误',result[0]['content'][0]['content'])

if __name__=='__main__':unittest.main(verbosity=2)

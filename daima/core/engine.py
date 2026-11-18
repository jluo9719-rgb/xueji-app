"""当前主入口：资料优先、按对话串行、持久化与失败回滚。
历史工具循环保存在 legacy_engine.py，不参与当前问答。
"""
import asyncio
import copy
import time
from core.config import config
from core.document_chat import NeedsGeneralAnswer
from core.context import count_messages_tokens
from core.log import get_logger
from core.mistakes import MistakeBook
from core.session import SessionStore, DEFAULT_SESSION_ID

log = get_logger(__name__)


class AgentEngine:
    def __init__(self, session_store=None):
        self._chat_lock = asyncio.Lock()
        self.session = session_store or SessionStore()
        self.mistakes = MistakeBook()
        self.session_id = DEFAULT_SESSION_ID
        self.messages = []
        self._memory = None

    @property
    def memory(self):
        # 仅旧管理接口按需读取，当前问答不使用跨会话记忆。
        if self._memory is None:
            from core.memory import LongTermMemory
            self._memory = LongTermMemory()
        return self._memory

    def setup_tools(self):
        """兼容旧调用入口；当前流程由程序直接检索，不提供模型自主选工具。"""

    def start(self) -> str:
        """加载历史会话。返回状态描述。"""
        self.messages = self.session.load_messages(self.session_id)
        if self.messages:
            n = sum(1 for m in self.messages if m.get("role") == "user")
            return f"已恢复历史会话（{n} 轮对话，{count_messages_tokens(self.messages)} tokens）"
        return "新会话"

    def session_title(self, session_id: str | None = None) -> str:
        """查会话标题（错题卡片的来源字段用）。"""
        sid = session_id or self.session_id
        try:
            for s in self.session.list_sessions():
                if s["id"] == sid:
                    return s.get("title") or sid
        except Exception:
            pass
        return sid

    async def chat(self, query: str, on_status=None, allow_general: bool = False) -> str:
        """同一会话串行处理；失败/取消回滚本轮消息，避免残留半截工具调用。"""
        if not query.strip():
            raise ValueError("请输入问题")
        async with self._chat_lock:
            before = copy.deepcopy(self.messages)
            started = time.perf_counter()
            try:
                from core.document_chat import answer_from_documents
                return await asyncio.wait_for(
                    answer_from_documents(self, query, allow_general, on_status),
                    timeout=float(config.get("llm.chat_timeout", 300)))
            except NeedsGeneralAnswer as error:
                self.messages = before + [{'role':'user','content':query},
                    {'role':'assistant','content':str(error),'meta':{'mode':'no_evidence','question':query}}]
                self.session.save_messages(self.session_id,self.messages)
                raise
            except asyncio.TimeoutError as error:
                self.messages = before
                raise TimeoutError("本轮问答超时，未完成的消息已回滚；可简化问题或调整 llm.chat_timeout") from error
            except BaseException:
                self.messages = before
                raise
            finally:
                log.info("chat session=%s elapsed=%.2fs", self.session_id,
                         time.perf_counter() - started)


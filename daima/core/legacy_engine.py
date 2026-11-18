# 历史工具自治流程：保留用于阅读和旧测试，网页与 CLI 不调用。
"""
AgentEngine：智能体主引擎（对应原 多agent协作.py 的 main_loop 及其配套逻辑）。

架构：
  用户提问 → 可选记忆提取/已有记忆注入 → 工具注册 → LLM 循环：
      ├─ 输出文本 → 持久化(BUG-03修复) → 结束
      └─ 请求工具 → Pydantic 校验 → 并行执行（搜索 asyncio / RAG 线程池）
            └─ 可选自我反思审核（上限由配置决定）→ 新线索继续检索

修复点：
  - BUG-03：最终回答先 append 再 save_session，重启不再丢最后一轮
  - 工具 schema 直接注册，不再维护额外工具向量库
  - thinking 块剥离后不进上下文（沿用原 quchu 逻辑）
"""
import asyncio
import copy
import time

from pydantic import BaseModel, Field, ValidationError

from core.config import config
from core.context import ContextCompressor, count_messages_tokens
from core.document_chat import NeedsGeneralAnswer
from core.llm import acall_llm, extract_text, get_client
from core.log import get_logger
from core.memory import LongTermMemory
from core.mistakes import MistakeBook, judge_exchange
from core.session import SessionStore, DEFAULT_SESSION_ID

log = get_logger(__name__)


# ---------------- 工具参数模型（Pydantic 校验） ----------------
class SearchInput(BaseModel):
    query: str = Field(description="搜索关键词，应包含完整的问题信息以提高搜索准确性")


class RagInput(BaseModel):
    query: str = Field(description="用户问题")


TOOL_MODELS = {"search_google_retry": SearchInput, "multi_retrieve": RagInput}


class AgentEngine:
    def __init__(self, session_store: SessionStore | None = None):
        self.client = get_client()
        self._chat_lock = asyncio.Lock()
        # session_store 可注入共享实例（API 多引擎场景下内存降级模式才互通）
        self.session = session_store or SessionStore()
        self.memory = LongTermMemory()
        self.mistakes = MistakeBook()
        self.compressor = ContextCompressor()
        self.session_id = DEFAULT_SESSION_ID   # 由 API 层按会话设置

        self.messages: list[dict] = []
        self.max_tool_calls = config.get("tools.max_tool_calls", 10)
        self.max_reflections = config.get("reflection.max_rounds", 2)

        # RAG 组件延迟初始化（首次用到知识检索才加载大模型）
        self._retriever = None

        # 工具注册表：name -> {schema, description}
        self.tools_registry: dict[str, dict] = {}
        self.description_to_schema: dict[str, dict] = {}
        self.embedder = None

    # ================= 初始化 =================
    def setup_tools(self) -> None:
        """注册内置工具；关闭联网时不向模型提供搜索工具。"""
        self.tools_registry = {
            "search_google_retry": {
                "description": "一个强大的通用网络搜索引擎。当你需要获取最新的实时信息"
                               "（如天气、新闻、股票）、百科知识，或者当用户的提问在"
                               "知识库/文档中找不到答案时，请使用此工具。",
                "input_schema": SearchInput.model_json_schema(),
            },
            "multi_retrieve": {
                "description": "检索用户上传到个人知识库的文档（讲义、教材、真题、资料等）。"
                               "当用户的问题可能与其上传的学习资料/文档内容相关时使用此工具。",
                "input_schema": RagInput.model_json_schema(),
            },
        }

        # 只有两个工具，直接注入 schema，不再为选择工具加载向量模型。
        if not config.get("tools.web_search_enabled", False):
            self.tools_registry.pop("search_google_retry", None)

        # 保留旧映射接口，schema 直接提供给模型。
        self.description_to_schema = {
            t["description"]: {"name": name, **t}
            for name, t in self.tools_registry.items()
        }

    def _ensure_rag(self):
        """延迟初始化检索器。索引已在文档上传时由 KBManager 后台建好，
        这里只加载嵌入模型与快照（有缓存时秒级）。"""
        if self._retriever is None:
            from rag.kb_manager import get_embedder, session_kb_id
            from rag.retriever import HybridRetriever
            log.info("首次使用知识检索，初始化检索器 ...")
            self.embedder = self.embedder or get_embedder()
            # 旧工具流程同样按会话取库：写死 default 会检索到空库
            self._retriever = HybridRetriever(kb_id=session_kb_id(self.session_id),
                                              embedder=self.embedder)
        return self._retriever

    # ================= 会话生命周期 =================
    def start(self) -> str:
        """加载历史会话。返回状态描述。"""
        self.messages = self.session.load_messages(self.session_id)
        if self.messages:
            n = sum(1 for m in self.messages if m.get("role") == "user")
            return f"已恢复历史会话（{n} 轮对话，{count_messages_tokens(self.messages)} tokens）"
        return "新会话"

    # ================= 工具选择 =================
    def _select_tools(self, query: str) -> list[dict]:
        """工具数量很少，直接提供所有已启用工具。"""
        return list(self.description_to_schema.values())

    # ================= 工具执行 =================
    async def _execute_tools(self, tool_uses: list[dict]) -> list[dict]:
        """
        并行执行一轮全部工具请求。
        返回 tool_result 消息列表（每项含 tool_use_id）。
        """
        import asyncio
        from tools.web_search import search_google_retry

        search_calls: list[tuple[str, str]] = []   # (tool_use_id, query)
        rag_calls: list[tuple[str, str]] = []
        results: list[dict] = []

        # 先做 Pydantic 参数校验
        for tu in tool_uses:
            model_cls = TOOL_MODELS.get(tu["name"])
            if model_cls is None or tu["name"] not in self.tools_registry:
                results.append(self._tool_result(tu["id"], f"未知工具：{tu['name']}"))
                continue
            try:
                if not isinstance(tu["input"], dict):
                    raise ValueError("工具参数必须是 JSON 对象")
                validated = model_cls(**tu["input"])
            except (ValidationError, ValueError, TypeError) as e:
                results.append(self._tool_result(tu["id"], f"参数校验错误：{e}"))
                continue
            if tu["name"] == "search_google_retry":
                search_calls.append((tu["id"], validated.query))
            else:
                rag_calls.append((tu["id"], validated.query))

        async def search_one(tid, query):
            try:
                output = await search_google_retry(query)
            except Exception as error:
                output = f"联网搜索失败：{error}"
            return self._tool_result(tid, output)

        async def rag_batch():
            # 多个 RAG 请求依次在工作线程运行，避免同时挤占本地推理资源。
            batch = []
            for tid, query in rag_calls:
                try:
                    def retrieve():
                        from tools.knowledge_search import knowledge_search
                        return knowledge_search(self._ensure_rag(), query)
                    output = await asyncio.to_thread(retrieve)
                except Exception as error:
                    log.exception("RAG 工具执行失败")
                    output = f"检索失败：{error}"
                batch.append(self._tool_result(tid, output))
            return batch

        jobs = [search_one(tid, query) for tid, query in search_calls]
        if rag_calls:
            jobs.append(rag_batch())
        for output in await asyncio.gather(*jobs):
            results.extend(output if isinstance(output, list) else [output])

        return results

    @staticmethod
    def _tool_result(tool_id: str, content: str) -> dict:
        return {"role": "user",
                "content": [{"type": "tool_result",
                             "tool_use_id": tool_id,
                             "content": content}]}

    @staticmethod
    def _strip_thinking(content_blocks) -> list[dict]:
        """assistant 回复存入 messages 前剥离 thinking 块。"""
        result = []
        for b in content_blocks:
            if getattr(b, "type", "") == "thinking":
                continue
            if getattr(b, "type", "") == "tool_use":
                result.append({"type": "tool_use", "name": b.name,
                               "input": b.input, "id": b.id})
            else:
                result.append({"type": "text", "text": getattr(b, "text", "")})
        return result

    # ================= 错题本自动收录（第六期） =================
    async def _capture_mistake(self, query: str, answer: str, notify) -> None:
        """回答后由 LLM 判断是否暴露知识困惑，是则自动收进错题本。失败静默跳过。"""
        if not config.get("mistakes.auto_capture", True):
            return
        if not answer or len(answer) < 30 or answer.startswith("搜索次数超限"):
            return                                    # 寒暄/超限提示不值得收录
        try:
            await notify("mistake", "分析是否收录错题本…")
            card = await judge_exchange(query, answer)
            if card:
                self.mistakes.add(question=query, answer=answer,
                                  confusion=card["confusion"], topic=card["topic"],
                                  session_id=self.session_id,
                                  session_title=self.session_title())
                await notify("mistake",
                             f"已收录错题本：{card['topic'] or card['confusion'][:20]}")
        except Exception as e:
            log.warning("错题自动收录失败（忽略）：%s", str(e)[:150])

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

    # ================= 自我反思 =================
    async def _reflect(self) -> str | None:
        """
        审核"当前检索结果能否回答用户问题"。
        返回 None（够了）或新查询词。
        """
        fanwen = """你当前的检索结果能回答用户的问题吗？
如果能，请只回答：YES
如果不能，请按照格式回答：NO，新查询：你的新关键词"""
        reflection_messages = self.messages + [{"role": "user", "content": fanwen}]
        try:
            resp = await acall_llm(reflection_messages,
                                   system="你是专门检测检索结果能否回答用户问题的",
                                   max_tokens=config.get("llm.max_tokens_util", 400))
            text = extract_text(resp).strip()
        except Exception as e:
            log.warning("反思审核失败，默认资料足够：%s", str(e)[:120])
            return None
        log.info("反思审核：%s", text[:80])
        if "YES" in text.upper():
            return None
        if "新查询：" not in text:
            return None
        return text.split("新查询：", 1)[1].strip()[:500] or None

    # ================= 主入口 =================
    SYSTEM_PROMPT = """你是用户的专业学习/知识助手。你的任务是根据检索工具返回的文档内容回答用户问题。
原则：
1. 优先使用检索文档中的信息作答；文档内容与问题相关时，可进行合理的归纳、对比和解释。
2. 不要脱离检索文档虚构事实；文档未覆盖的部分可用你自己的知识补充，但要说明这不是来自文档。
3. 实时类信息（新闻、天气、行情等）可用联网搜索工具查询。
4. 如果需要同时查文档和联网，请在同一轮次中调用所有相关工具，不要只调用一个。
5. 默认使用简体中文回答。请直接输出结果，不要输出思考过程。
{memory_line}
注意语气要平易近人，对学习者友好。"""

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

    async def _chat_impl(self, query: str, on_status=None) -> str:
        """
        处理一次用户提问，返回最终回答文本。

        on_status: 可选回调 on_status(stage: str, detail: str)，
        用于向 SSE 前端推送过程状态（拆解/检索/反思/回答中）。
        """
        async def notify(stage: str, detail: str = "") -> None:
            log.info("[状态] %s %s", stage, detail)
            if on_status:
                try:
                    r = on_status(stage, detail)
                    if hasattr(r, "__await__"):
                        await r
                except Exception:
                    pass

        # 长时记忆：提取 + 注入
        await notify("memory", "正在提取长时记忆…" if config.get("memory.auto_extract", False)
                     else "读取已有记忆…")
        if config.get("memory.auto_extract", False):
            await self.memory.aextract_from(query)
        memory_text = self.memory.load_memory_text()
        memory_line = f"6. {memory_text}" if memory_text else ""
        system_prompt = self.SYSTEM_PROMPT.format(memory_line=memory_line)
        if not config.get("tools.web_search_enabled", False):
            system_prompt += "\n当前未启用联网搜索，不能核实实时信息，请如实说明信息时效限制。"

        self.messages.append({"role": "user", "content": query})
        await notify("tools", "选择工具中…")
        tools = self._select_tools(query) if self.max_tool_calls > 0 else []

        tool_call_count = 0
        reflect_count = 0

        while True:
            # 软压缩 + 硬窗口
            self.messages = await asyncio.to_thread(self.compressor.compress, self.messages)

            await notify("thinking", "模型思考中…")
            response = await acall_llm(
                messages=self.messages,
                system=system_prompt,
                max_tokens=config.get("llm.max_tokens_answer", 2000),
                tools=tools or None,
            )

            blocks = response.content or []
            if not blocks:
                raise RuntimeError("模型返回空内容，请检查模型状态、thinking 和 max_tokens 配置")
            if getattr(response, "stop_reason", None) == "max_tokens":
                raise RuntimeError("模型输出达到 max_tokens 上限，未得到完整回答；请提高额度或关闭思考")
            tool_blocks = [b for b in blocks if getattr(b, "type", "") == "tool_use"]

            # ---- 最终文本回答 ----
            if not tool_blocks:
                answer = extract_text(response).strip()
                if not answer:
                    raise RuntimeError("模型没有返回正文（可能只有思考块），请检查模型配置")
                # BUG-03 修复：先入列再持久化
                self.messages.append({"role": "assistant", "content": answer})
                self.messages = self.compressor.force_window(self.messages)
                await self._capture_mistake(query, answer, notify)
                self.session.save_messages(self.session_id, self.messages)
                await notify("done", "")
                return answer

            # ---- 工具调用轮 ----
            tool_call_count += 1
            if not tools or tool_call_count > self.max_tool_calls:
                raise RuntimeError("模型在工具预算耗尽后仍请求工具，本轮已终止，请简化问题")

            self.messages.append({"role": "assistant",
                                  "content": self._strip_thinking(blocks)})
            tool_uses = [{"id": b.id, "name": b.name, "input": b.input}
                         for b in blocks if getattr(b, "type", "") == "tool_use"]
            log.info("第 %d 轮工具调用：%s", tool_call_count,
                     [t["name"] for t in tool_uses])
            await notify("tool_call",
                         f"第 {tool_call_count} 轮：{', '.join(t['name'] for t in tool_uses)}")

            tool_results = await self._execute_tools(tool_uses)
            self.messages.extend(tool_results)
            await notify("tool_result", f"第 {tool_call_count} 轮工具返回完毕")

            # 工具轮数到上限后移除 tools，下一轮只允许根据现有资料作答。
            if tool_call_count >= self.max_tool_calls:
                tools = []
                system_prompt += "\n工具预算已用完，请根据已有结果直接回答；资料不足请明确说明。"
                continue

            # ---- 自我反思（可选） ----
            if not config.get("reflection.enabled", False):
                continue
            if reflect_count >= self.max_reflections:
                log.info("已达反思上限，强制进入回答")
                tools = []
                continue
            await notify("reflect", "审核检索结果是否足够…")
            reflect_count += 1
            new_query = await self._reflect()
            if new_query is None:
                tools = []
                continue   # 资料足够，下一轮直接回答

            if reflect_count >= self.max_reflections:
                tools = []
                system_prompt += "\n审核预算已用完，请用已有证据作答并说明资料不足的部分。"
                continue

            await notify("reflect_retry",
                         f"资料不足（第 {reflect_count} 次），用新线索再查：{new_query[:40]}")
            # 清理旧线索提示（沿用原逻辑：避免多条"新线索"堆积）
            if reflect_count > 1:
                self.messages = [
                    m for m in self.messages
                    if not (isinstance(m.get("content"), str)
                            and "请结合这个新线索继续检索：" in m["content"])
                ]
            # 若新查询需要别的工具，重新做工具选择
            tools = self._select_tools(new_query)
            self.messages.append({
                "role": "user",
                "content": f"请结合这个新线索继续检索：{new_query}",
            })

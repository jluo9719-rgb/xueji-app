"""
FastAPI 服务（第三期后端）

路由：
  GET  /health                    健康检查
  POST /chat                      SSE 流式对话（status/answer/done 三类事件）
  POST /chat/sync                 非流式对话（一次性 JSON 返回，便于测试）
  GET  /sessions                  会话列表
  POST /sessions                  新建会话
  DELETE /sessions/{sid}          删除会话
  GET  /sessions/{sid}/messages   某会话消息历史
  GET  /memory                    长时记忆列表
  DELETE /memory/{key}            删除某条记忆
  POST /documents                 上传知识库文档（后台异步索引）
  GET  /documents                 文档列表与索引进度
  DELETE /documents/{doc_id}      删除文档（含索引清理）
  GET  /mistakes                  错题本列表（?status= 过滤）
  POST /mistakes                  手动收录某会话最后一轮问答
  PUT  /mistakes/{id}/status      切换掌握状态
  DELETE /mistakes/{id}           删除错题卡片
  GET  /mistakes/export           导出 Markdown 错题本
  POST /exam/from-mistakes        从错题本生成试卷
  POST /exam/from-topic           从知识库主题生成试卷

启动：
  cd daima && uvicorn api.app:app --host 127.0.0.1 --port 8000
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# 保证以 `uvicorn api.app:app` 启动时也能找到项目根模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, Response
from pydantic import BaseModel, Field

from core.log import setup_logging
setup_logging()

from core.config import config
from core.engine import AgentEngine
from core.document_chat import NeedsGeneralAnswer
from core.llm import get_async_client
from core.session import DEFAULT_SESSION_ID, SessionStore
from core.log import get_logger
from core.mistakes import judge_exchange
from rag.kb_manager import get_kb_manager, session_kb_id
from core.exam_builder import (
    retrieve_for_mistakes, retrieve_for_topic,
    generate_questions, review_questions, build_exam_markdown, apply_reviews, exam_response
)

log = get_logger(__name__)

app = FastAPI(title="学记个人学习备考助手", version="2.1")

# Web 前端（第四期）：单文件聊天页
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@app.get("/", include_in_schema=False)
async def index():
    """聊天界面入口：浏览器访问 http://127.0.0.1:8000/"""
    return FileResponse(_WEB_DIR / "index.html")

# 引擎按会话缓存：{session_id: AgentEngine}
_engines: dict[str, AgentEngine] = {}
_engine_lock = asyncio.Lock()
# 全局共享一个 SessionStore：Redis 不可用降级为内存时，各引擎的会话数据才能互通
# （第六期修复：此前每个引擎各自 new 一个 SessionStore，内存模式下会话互相不可见；
#   同时 engine.chat 固定存 default 会话导致多会话内容串库——现由 session_id 精确绑定）
_SESSION_STORE = SessionStore()


async def _get_engine(session_id: str) -> AgentEngine:
    """取（或创建）某会话的引擎实例。引擎内部持有 messages 与 RAG 组件。"""
    async with _engine_lock:
        engine = _engines.get(session_id)
        if engine is None:
            engine = AgentEngine(session_store=_SESSION_STORE)
            engine.session_id = session_id
            engine.setup_tools()
            engine.messages = engine.session.load_messages(session_id)
            _engines[session_id] = engine
        return engine


# ================= 请求模型 =================
class ChatRequest(BaseModel):
    message: str
    session_id: str = DEFAULT_SESSION_ID
    allow_general: bool = False


class CreateSessionRequest(BaseModel):
    session_id: str
    title: str | None = None


# ================= 基础 =================
@app.get("/health")
async def health():
    from core.credentials import mask, get_llm_model
    try:
        key, url = await asyncio.to_thread(
            lambda: (__import__("core.credentials", fromlist=["get_anthropic_config"])
                     .get_anthropic_config()))
        llm_ok = bool(key)
        key_masked = mask(key)
    except Exception:
        llm_ok, key_masked = False, "<未配置>"
    return {"status": "ok",
            "llm_configured": llm_ok,
            "llm": get_llm_model(),
            "provider": config.get("llm.provider", "anthropic"),
            "llm_reachable": "not_checked",
            "base_url": url if llm_ok else None,
            "key": key_masked,
            "redis_persistent": _SESSION_STORE.redis is not None,
            "session_storage": "sqlite", "data_dir": str(config.get("storage.data_dir"))}


# ================= 对话 =================
@app.post("/chat")
async def chat_stream(req: ChatRequest):
    """SSE 流式对话。事件序列示例：

        event: status     data: {"stage":"memory","detail":"..."}
        event: status     data: {"stage":"tool_call","detail":"第 1 轮：multi_retrieve"}
        event: answer     data: {"text":"……最终回答……"}
        event: done       data: {}
    """
    engine = await _get_engine(req.session_id)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        def on_status(stage: str, detail: str):
            queue.put_nowait(("status", {"stage": stage, "detail": detail}))

        async def run_chat():
            try:
                answer = await engine.chat(req.message, on_status=on_status, allow_general=req.allow_general)
                queue.put_nowait(("answer", {"text": answer, "meta": engine.messages[-1].get("meta", {})}))
            except NeedsGeneralAnswer as e:
                queue.put_nowait(("needs_general", {"message":str(e), "question":req.message}))
            except Exception as e:
                log.exception("chat 处理失败")
                queue.put_nowait(("error", {"message": str(e)[:300]}))
            finally:
                queue.put_nowait(("_end", {}))

        task = asyncio.create_task(run_chat())

        try:
            while True:
                kind, data = await queue.get()
                if kind == "_end":
                    yield f"event: done\ndata: {{}}\n\n"
                    break
                payload = json.dumps(data, ensure_ascii=False)
                yield f"event: {kind}\ndata: {payload}\n\n"
        finally:
            task.cancel()
        # 更新会话标题：首轮提问自动作为标题
        sessions = engine.session.list_sessions()
        for s in sessions:
            if s["id"] == req.session_id and s["user_turns"] <= 1 and not s.get("title_manual"):
                engine.session.rename_session(req.session_id, req.message[:20])
                break

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/chat/sync")
async def chat_sync(req: ChatRequest):
    """非流式对话：等待最终回答一次性返回。"""
    engine = await _get_engine(req.session_id)
    t0 = asyncio.get_event_loop().time()
    try:
        answer = await engine.chat(req.message, allow_general=req.allow_general)
    except NeedsGeneralAnswer as e:
        return {"needs_general": True, "message": str(e), "question": req.message}
    except Exception as e:
        log.exception("chat/sync 处理失败")
        raise HTTPException(status_code=502, detail=str(e)[:300]) from e
    cost = asyncio.get_event_loop().time() - t0
    # 首轮提问自动作为会话标题
    for s in engine.session.list_sessions():
        if s["id"] == req.session_id and s["user_turns"] <= 1 and not s.get("title_manual"):
            engine.session.rename_session(req.session_id, req.message[:20])
            break
    return {"session_id": req.session_id, "answer": answer, "meta": engine.messages[-1].get("meta", {}),
            "cost_seconds": round(cost, 1),
            "message_count": len(engine.messages)}


# ================= 会话管理 =================
@app.get("/sessions")
async def list_sessions():
    engine = await _get_engine(DEFAULT_SESSION_ID)
    return {"sessions": engine.session.list_sessions()}


@app.post("/sessions")
async def create_session(req: CreateSessionRequest):
    engine = await _get_engine(req.session_id)
    if any(s['id'] == req.session_id for s in engine.session.list_sessions()):
        raise HTTPException(status_code=409, detail="会话已存在")
    meta = engine.session.create_session(req.session_id, req.title)
    engine.messages = []
    return meta


class SessionUpdate(BaseModel):
    title: str | None = None
    archived: bool | None = None


@app.patch("/sessions/{sid}")
async def update_session(sid: str, req: SessionUpdate):
    store = _SESSION_STORE
    if not any(s["id"] == sid for s in store.list_sessions()):
        raise HTTPException(status_code=404, detail="会话不存在")
    if req.title is not None:
        if not req.title.strip() or len(req.title.strip()) > 100:
            raise HTTPException(status_code=400, detail="标题请输入 1～100 个字符")
        store.rename_session(sid, req.title.strip())
    if req.archived is not None:
        store.archive_session(sid, req.archived)
    return {"updated": sid}


@app.delete("/sessions/{sid}")
async def delete_session(sid: str):
    engine = await _get_engine(sid)
    async with engine._chat_lock:
        if not any(s["id"] == sid for s in engine.session.list_sessions()):
            raise HTTPException(status_code=404, detail="会话不存在")
        try:
            await asyncio.to_thread(get_kb_manager().delete_scope, session_kb_id(sid))
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        for card in engine.mistakes.list_all():
            if card['session_id'] == sid:
                engine.mistakes.delete(card['id'])
        engine.session.delete_session(sid)
        engine.messages = []
        _engines.pop(sid, None)
    return {"deleted": sid}


@app.get("/sessions/{sid}/messages")
async def get_messages(sid: str):
    engine = await _get_engine(DEFAULT_SESSION_ID)
    msgs = engine.session.load_messages(sid)
    if not msgs and sid != DEFAULT_SESSION_ID and \
            not any(s["id"] == sid for s in engine.session.list_sessions()):
        raise HTTPException(status_code=404, detail=f"会话不存在：{sid}")
    return {"session_id": sid, "messages": msgs}


# ================= 记忆管理 =================
@app.get("/memory")
async def list_memory():
    engine = await _get_engine(DEFAULT_SESSION_ID)
    items = [{"key": k, "value": v} for k, v in engine.memory.list_all()]
    return {"items": items}


@app.delete("/memory/{key}")
async def delete_memory(key: str):
    engine = await _get_engine(DEFAULT_SESSION_ID)
    before = dict(engine.memory.list_all())
    if key not in before:
        raise HTTPException(status_code=404, detail=f"记忆不存在：{key}")
    engine.memory.delete(key)
    return {"deleted": key}


# ================= 知识库文档管理（第五期） =================
@app.post("/documents")
async def upload_document(file: UploadFile = File(...), session_id: str = DEFAULT_SESSION_ID):
    """上传文档到默认知识库。接口立即返回，后台异步索引（轮询 GET /documents 看进度）。"""
    _SESSION_STORE._touch_index(session_id)
    limit = int(config.get("storage.upload_max_bytes", 200 * 1024 * 1024))
    content = bytearray()
    try:
        while block := await file.read(min(1024 * 1024, limit + 1)):
            content.extend(block)
            if len(content) > limit:
                raise HTTPException(status_code=413, detail=f"文件超过 {limit // (1024 * 1024)}MB 上限")
    finally:
        await file.close()
    try:
        kb = await asyncio.to_thread(get_kb_manager)
        rec = await asyncio.to_thread(kb.add_document, file.filename, bytes(content), session_kb_id(session_id))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return rec


@app.get("/documents")
async def list_documents(session_id: str = DEFAULT_SESSION_ID):
    """文档列表（含索引状态与进度：processing / ready / error）。"""
    kb = await asyncio.to_thread(get_kb_manager)
    return {"docs": await asyncio.to_thread(kb.list_documents, session_kb_id(session_id))}


@app.post("/documents/{doc_id}/rebuild")
async def rebuild_document(doc_id: str, session_id: str = DEFAULT_SESSION_ID):
    engine = await _get_engine(session_id)
    async with engine._chat_lock:
        try:
            return await asyncio.to_thread(get_kb_manager().rebuild_document, doc_id, session_kb_id(session_id))
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str, session_id: str = DEFAULT_SESSION_ID):
    """删除文档：向量库 chunks + 上传原件 + 注册表记录一并清理。"""
    engine = await _get_engine(session_id)
    async with engine._chat_lock:
        try:
            ok = await asyncio.to_thread(get_kb_manager().delete_document, doc_id, session_kb_id(session_id))
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except Exception as e:
            log.exception("资料删除失败")
            raise HTTPException(status_code=500, detail="资料未能完整删除，请重试；未报告删除成功") from e
        if not ok:
            raise HTTPException(status_code=404, detail=f"文档不存在：{doc_id}")
    return {"deleted": doc_id}


# ================= 错题本（第六期） =================
class MistakeCaptureRequest(BaseModel):
    session_id: str
    question: str | None = None
    answer: str | None = None


class MistakeStatusRequest(BaseModel):
    status: str


@app.get("/mistakes")
async def list_mistakes(status: str | None = None):
    """错题卡片列表（可 ?status=未掌握/已掌握 过滤），附统计。"""
    engine = await _get_engine(DEFAULT_SESSION_ID)
    return {"items": engine.mistakes.list_all(status),
            "stats": engine.mistakes.count()}


@app.post("/mistakes")
async def capture_mistake(req: MistakeCaptureRequest):
    """手动收录：取指定会话最后一轮问答，LLM 提取主题/混淆点后强制收录。"""
    engine = await _get_engine(DEFAULT_SESSION_ID)
    msgs = engine.session.load_messages(req.session_id)
    pairs = []
    question = None
    for message in msgs:
        if message.get("meta", {}).get("mode") == "no_evidence":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if message.get("role") == "user" and not content.startswith("请结合这个新线索继续检索："):
            question = content
        elif message.get("role") == "assistant" and question:
            pairs.append((question, content))
            question = None
    if req.question is not None or req.answer is not None:
        selected = (req.question, req.answer)
        if selected not in pairs:
            raise HTTPException(status_code=409, detail="这条问答已不在会话记录中，请刷新后重试")
    elif pairs:
        selected = pairs[-1]
    else:
        raise HTTPException(status_code=400, detail="该会话没有可收录的完整问答")
    last_user, last_answer = selected
    for item in engine.mistakes.list_all():
        if (item["session_id"] == req.session_id and item["question"] == last_user
                and item["answer"] == last_answer[:3000]):
            return item
    card = await judge_exchange(last_user, last_answer, force=True)
    if not card:
        raise HTTPException(status_code=502, detail="收录失败：LLM 调用异常，请稍后重试")
    mid = engine.mistakes.add(
        question=last_user, answer=last_answer,
        confusion=card["confusion"], topic=card["topic"],
        session_id=req.session_id,
        session_title=engine.session_title(req.session_id))
    return engine.mistakes.get(mid)


@app.put("/mistakes/{mid}/status")
async def set_mistake_status(mid: int, req: MistakeStatusRequest):
    """切换掌握状态（未掌握 ↔ 已掌握）。"""
    engine = await _get_engine(DEFAULT_SESSION_ID)
    try:
        ok = engine.mistakes.set_status(mid, req.status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail=f"错题不存在：#{mid}")
    return {"id": mid, "status": req.status}


@app.delete("/mistakes/{mid}")
async def delete_mistake(mid: int):
    engine = await _get_engine(DEFAULT_SESSION_ID)
    if not engine.mistakes.delete(mid):
        raise HTTPException(status_code=404, detail=f"错题不存在：#{mid}")
    return {"deleted": mid}


@app.get("/mistakes/export")
async def export_mistakes():
    """导出 Markdown 错题本（浏览器直接下载）。"""
    engine = await _get_engine(DEFAULT_SESSION_ID)
    md = engine.mistakes.export_markdown()
    fname = f"错题本_{datetime.now():%Y-%m-%d}.md"
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(fname)}"})


# ================= 出题（第七期） =================
class ExamFromMistakesRequest(BaseModel):
    mistake_ids: list[int] = Field(min_length=1, max_length=10)
    session_id: str = DEFAULT_SESSION_ID
    count_per: int = Field(default=2, ge=1, le=5)  # 每个错题生成几道题


class ExamFromTopicRequest(BaseModel):
    topic: str
    session_id: str = DEFAULT_SESSION_ID
    count: int = Field(default=5, ge=1, le=10)


async def _generate_reviewed(chunks, count, context):
    try:
        questions = await generate_questions(chunks, count=count, context=context)
    except Exception as error:
        log.exception("出题失败")
        detail = str(error) if isinstance(error, ValueError) else "模型调用失败，请检查本地模型服务是否启动"
        raise HTTPException(status_code=502, detail=detail) from error
    reviews = await review_questions(questions, chunks)
    return apply_reviews(questions, reviews)


@app.post("/exam/from-mistakes")
async def generate_exam_from_mistakes(req: ExamFromMistakesRequest):
    engine = await _get_engine(req.session_id)
    async with engine._chat_lock:
        ids = list(dict.fromkeys(req.mistake_ids))
        allowed = {m['id'] for m in engine.mistakes.list_all() if m['session_id'] == req.session_id}
        if not set(ids).issubset(allowed):
            raise HTTPException(status_code=400, detail="请选择当前对话的错题")
        retrieved = await asyncio.to_thread(retrieve_for_mistakes, ids, top_k_per=3,
                                           kb_id=session_kb_id(req.session_id))
        all_questions, rejected = [], []
        for item in retrieved:
            if not item['chunks']:
                rejected.append({'mistake_id': item['mistake_id'], 'reason': '未找到相关资料，未生成题目'})
                continue
            questions, reasons = await _generate_reviewed(item['chunks'], req.count_per,
                                                          f"{item['topic']} - {item['confusion']}")
            all_questions.extend(questions)
            rejected.extend(dict(r, mistake_id=item['mistake_id']) for r in reasons)
        if not all_questions:
            raise HTTPException(status_code=422, detail={'message': '没有可用题目：资料不足或题目校验未通过',
                                                        'rejection_reasons': rejected})
        return exam_response(all_questions, '错题本薄弱点检测卷', len(ids) * req.count_per, rejected)


@app.post("/exam/from-topic")
async def generate_exam_from_topic(req: ExamFromTopicRequest):
    if not req.topic.strip():
        raise HTTPException(status_code=400, detail="请输入主题关键词")
    engine = await _get_engine(req.session_id)
    async with engine._chat_lock:
        chunks = await asyncio.to_thread(retrieve_for_topic, req.topic,
                                         kb_id=session_kb_id(req.session_id), top_k=10)
        if not chunks:
            raise HTTPException(status_code=404, detail=f"主题「{req.topic}」未检索到足够相关的资料")
        questions, rejected = await _generate_reviewed(chunks, req.count, f"主题：{req.topic}")
        if not questions:
            raise HTTPException(status_code=422, detail={'message': '题目未通过结构或内容校验，请调整主题重试',
                                                        'rejection_reasons': rejected})
        return exam_response(questions, f'「{req.topic}」主题检测卷', req.count, rejected)

"""
知识库管理器（第五期）：用户自建知识库的上传 / 异步索引 / 删除 / 快照缓存。

设计：
  - 数据三层：知识库(kb_id) → 文档(doc_id) → chunk（ChromaDB metadata 打标，where 过滤检索）
  - 上传即索引：后台 worker 线程顺序消费任务，接口立即返回（状态 processing → ready）
  - 注册表 data/kb_registry.json 记录文档状态与嵌入进度（临时文件 + 原子替换写入）
  - kb_rev 版本号：任何文档增删完成时 +1，检索侧快照（chunks+metadatas+BM25）检测到 rev 变化自动重建
  - chunks / metadatas 与 BM25 一起缓存，保证三者下标永远对齐
  - ChromaDB 仍为嵌入式本地库，不引入任何外部服务依赖
"""
import hashlib
import json
import os
import pickle
import queue
import threading
import time
from datetime import datetime

import chromadb

from core.config import config
from core.log import get_logger

log = get_logger(__name__)

DEFAULT_KB_ID = "default"


def session_kb_id(session_id: str) -> str:
    """稳定、路径安全的会话知识库 ID；不会绑定旧 default 演示库。"""
    return "session_" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]

ALLOWED_EXTS = {".pdf", ".txt", ".md", ".docx"}
EMBED_BATCH = 32          # 每批嵌入的 chunk 数（进度按批上报）

_embedder = None
_embedder_lock = threading.Lock()


def get_embedder():
    """m3e 嵌入模型进程级单例（引擎工具选择 / 切分 / 索引共用，避免重复加载）。"""
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                # HF 直连不稳时走镜像（重构时漏掉的原脚本 L11 设置，此处补回）。
                # 必须在导入 sentence_transformers 之前设置环境变量才生效。
                mirror = config.get("embedding.hf_endpoint")
                if mirror and not os.environ.get("HF_ENDPOINT"):
                    os.environ["HF_ENDPOINT"] = mirror
                from sentence_transformers import SentenceTransformer
                _embedder = SentenceTransformer(config.get("embedding.model"))
    return _embedder


class KBManager:
    """默认知识库的文档生命周期 + 检索快照管理（进程内单例）。"""

    def __init__(self):
        self.data_dir = config.abspath(config.get("storage.data_dir", "./data"))
        self.cache_dir = config.abspath(config.get("storage.cache_dir", "./cache"))
        self.chroma_path = config.abspath(config.get("storage.chroma_path", "./chroma_db"))
        self.uploads_dir = os.path.join(self.data_dir, "uploads")
        self.registry_path = os.path.join(self.data_dir, "kb_registry.json")
        os.makedirs(self.uploads_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

        self._lock = threading.RLock()
        self._snapshots: dict[str, dict] = {}     # kb_id -> {"rev", "chunks", "metadatas", "bm25"}
        self._jobs: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None

        client = chromadb.PersistentClient(path=self.chroma_path)
        # 注意：新版 chromadb 要求 collection 名 >=3 字符，故用 kb_store
        self.collection = client.get_or_create_collection(name="kb_store")

        self._registry = self._load_registry()
        self._recover_stale()

    # ================= 注册表 =================
    def _load_registry(self) -> dict:
        if os.path.exists(self.registry_path):
            try:
                with open(self.registry_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log.warning("注册表损坏，重建空表：%s", e)
        return {"kb_rev": 0, "docs": []}

    def _save_registry(self) -> None:
        with self._lock:
            tmp = self.registry_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._registry, f, ensure_ascii=False, indent=2)
            # Windows 文件索引/扫描器可能短暂占用目标文件，不要因此把已完成的索引
            # 标成失败；始终保留原子替换，重试耗尽仍抛错，不删除旧注册表。
            for attempt in range(5):
                try:
                    os.replace(tmp, self.registry_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(.05 * (2 ** attempt))

    def _get_doc(self, doc_id: str) -> dict | None:
        for d in self._registry["docs"]:
            if d["doc_id"] == doc_id:
                return d
        return None

    def _recover_stale(self) -> None:
        """上次进程中断导致的 processing 文档：清理半成品 chunks 并标记 error。"""
        stale = [d for d in self._registry["docs"] if d["status"] == "processing"]
        for d in stale:
            try:
                self.collection.delete(where={"doc_id": d["doc_id"]})
            except Exception:
                pass
            d["status"] = "error"
            d["error"] = "上次索引被中断，请点击重建索引重试"
        if stale:
            self._save_registry()
            log.warning("启动恢复：%d 个中断文档已清理并标记 error", len(stale))

    # ================= 后台索引 worker =================
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._worker_loop, daemon=True, name="kb-indexer")
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            doc_id = self._jobs.get()
            try:
                self._index_document(doc_id)
            except Exception as e:
                log.exception("文档索引失败：%s", doc_id)
                with self._lock:
                    d = self._get_doc(doc_id)
                    if d:
                        d.update(status="error", error=str(e)[:200])
                        self._save_registry()
            finally:
                self._jobs.task_done()

    def _update(self, doc_id: str, **kw) -> None:
        with self._lock:
            d = self._get_doc(doc_id)
            if d:
                d.update(kw)
                self._save_registry()

    def _index_document(self, doc_id: str) -> None:
        """后台执行：解析 → 切分 → 分批嵌入写入（实时进度）→ 就绪并 bump kb_rev。"""
        d = self._get_doc(doc_id)
        if not d:
            return
        path = os.path.join(self.uploads_dir, doc_id + d["ext"])
        self._update(doc_id, stage="解析文档中", progress=0)
        from rag.loader import load_document
        text = load_document(path)

        self._update(doc_id, stage="切分中", progress=0)
        from rag.chunker import chunk_document
        chunks = chunk_document(None, text)
        if not chunks:
            raise ValueError("文档切分后内容为空")

        # 必须清除旧版本，删除失败则本次重建失败，不伪报成功。
        with self._lock:
            self.collection.delete(where={"doc_id": doc_id})
            if self.collection.get(where={"doc_id": doc_id}, include=[])["ids"]:
                raise RuntimeError("旧索引未能完整清除，请重试")

        self._update(doc_id, stage="首次加载向量模型（可能需要稍候）", progress=0)
        embedder = get_embedder()
        from uuid import uuid4
        revision = uuid4().hex[:12]
        total = len(chunks)
        self._update(doc_id, stage="向量化中", progress=0, chunk_count=total)
        for i in range(0, total, EMBED_BATCH):
            batch = chunks[i:i + EMBED_BATCH]
            vectors = embedder.encode(batch)
            self.collection.add(
                documents=batch,
                embeddings=vectors,
                ids=[f"{doc_id}_{revision}_{i + j}" for j in range(len(batch))],
                metadatas=[{"kb_id": d["kb_id"], "doc_id": doc_id,
                            "doc_name": d["name"], "chunk_index": i + j, "revision": revision,
                            "chunk_id": f"{doc_id}_{revision}_{i+j}"}
                           for j in range(len(batch))],
            )
            self._update(doc_id, stage="向量化中",
                         progress=round((i + len(batch)) / total * 100))

        with self._lock:
            d2 = self._get_doc(doc_id)
            d2.update(status="ready", stage="就绪", progress=100, error=None, index_version="heading-v2", revision=revision)
            self._registry["kb_rev"] = self._registry.get("kb_rev", 0) + 1
            self._save_registry()
        log.info("✅ 文档索引完成：%s（%d chunks）", d["name"], total)

    # ================= 对外接口 =================
    def add_document(self, filename: str, content: bytes, kb_id: str = DEFAULT_KB_ID) -> dict:
        """登记文档并入队后台索引，立即返回记录（不阻塞）。"""
        if not content:
            raise ValueError("文件为空，请上传包含文字的资料")
        ext = os.path.splitext(filename or "")[1].lower()
        if ext not in ALLOWED_EXTS:
            raise ValueError(f"不支持的格式 {ext or '(无扩展名)'}，支持：pdf / txt / md / docx")
        doc_id = hashlib.sha256(kb_id.encode() + b"\0" + content).hexdigest()[:24]
        with self._lock:
            exists = self._get_doc(doc_id)
            if exists and exists["status"] in ("processing", "ready"):
                return {**exists, "duplicate": True}
        path = os.path.join(self.uploads_dir, doc_id + ext)
        with open(path, "wb") as f:
            f.write(content)

        rec = {"doc_id": doc_id, "kb_id": kb_id, "name": filename, "ext": ext,
               "status": "processing", "stage": "排队中", "progress": 0, "chunk_count": 0,
               "size": len(content),
               "uploaded_at": datetime.now().isoformat(timespec="seconds"),
               "error": None}
        with self._lock:
            self._registry["docs"] = [x for x in self._registry["docs"]
                                      if x["doc_id"] != doc_id]
            self._registry["docs"].insert(0, rec)
            self._save_registry()
        self._ensure_worker()
        self._jobs.put(doc_id)
        log.info("文档已入队：%s（%s，%.1f KB）", filename, doc_id, len(content) / 1024)
        return rec

    def list_documents(self, kb_id: str | None = None) -> list[dict]:
        with self._lock:
            return [dict(d) for d in self._registry["docs"] if kb_id is None or d["kb_id"] == kb_id]

    def rebuild_document(self, doc_id: str, kb_id: str):
        with self._lock:
            d = self._get_doc(doc_id)
            if not d or d['kb_id'] != kb_id:
                raise ValueError("资料不存在")
            if d['status'] == 'processing':
                raise ValueError("资料已在处理中")
            d.update(status='processing', stage='等待重建', progress=0)
            self._registry['kb_rev'] += 1
            self._snapshots.clear()
            self._save_registry()
            self._ensure_worker()
            self._jobs.put(doc_id)
            return dict(d)

    def delete_document(self, doc_id: str, kb_id: str | None = None) -> bool:
        # 删除和快照读取共用锁，不能在删除后重新写回旧检索缓存。
        with self._lock:
            d = self._get_doc(doc_id)
            if not d or (kb_id is not None and d["kb_id"] != kb_id):
                return False
            if d["status"] == "processing":
                raise ValueError("文档正在索引中，请稍后再删除")
            self.collection.delete(where={"doc_id": doc_id})
            if self.collection.get(where={"doc_id": doc_id}, include=[])["ids"]:
                raise RuntimeError("向量删除核验失败，请重试")
            # 立即清除所有内存快照和磁盘 BM25 快照；不等待下次查询。
            self._snapshots.clear()
            from pathlib import Path
            for cache in Path(self.cache_dir).glob("kb_*_bm25.pkl"):
                cache.unlink()
            path = os.path.join(self.uploads_dir, doc_id + d["ext"])
            if os.path.exists(path):
                os.remove(path)
            self._registry["docs"] = [x for x in self._registry["docs"] if x["doc_id"] != doc_id]
            self._registry["kb_rev"] = self._registry.get("kb_rev", 0) + 1
            self._save_registry()
            return True

    def delete_scope(self, kb_id: str) -> None:
        with self._lock:
            docs = self.list_documents(kb_id)
            if any(d["status"] == "processing" for d in docs):
                raise ValueError("资料仍在索引，请完成后再删除对话")
            for doc in docs:
                self.delete_document(doc["doc_id"], kb_id)

    # ================= 检索侧快照 =================
    def get_snapshot(self, kb_id: str = DEFAULT_KB_ID) -> tuple[list, list, object]:
        """返回 (chunks, metadatas, bm25)。kb_rev 变化时自动重建；三者下标永远对齐。"""
        with self._lock:
            rev = self._registry.get("kb_rev", 0)
            snap = self._snapshots.get(kb_id)
            if snap and snap["rev"] == rev:
                return snap["chunks"], snap["metadatas"], snap["bm25"]
            chunks, metadatas, bm25 = self._load_or_build(kb_id, rev)
            self._snapshots[kb_id] = {"rev": rev, "chunks": chunks,
                                      "metadatas": metadatas, "bm25": bm25}
            return chunks, metadatas, bm25

    def is_empty(self, kb_id: str = DEFAULT_KB_ID) -> bool:
        chunks, _metadatas, _bm25 = self.get_snapshot(kb_id)
        return len(chunks) == 0

    def _load_or_build(self, kb_id: str, rev: int):
        cache = os.path.join(self.cache_dir, f"kb_{kb_id}_bm25.pkl")
        if os.path.exists(cache):
            try:
                with open(cache, "rb") as f:
                    obj = pickle.load(f)
                # 旧格式缓存没有 metadatas，视同损坏直接重建
                if obj.get("rev") == rev and obj.get("format") == 2:
                    log.info("✅ 知识库快照自缓存加载（rev=%d，%d chunks）",
                             rev, len(obj["chunks"]))
                    return obj["chunks"], obj["metadatas"], obj["bm25"]
            except Exception as e:
                log.warning("快照缓存损坏，重建：%s", e)

        res = self.collection.get(where={"kb_id": kb_id},
                                  include=["documents", "metadatas"])
        chunks = list(res["documents"] or [])
        metadatas = [{**m, "chunk_id": cid} for m,cid in zip(res["metadatas"] or [],res["ids"])]
        if not chunks:
            log.info("知识库 %s 为空", kb_id)
            return chunks, metadatas, None

        import jieba
        from rank_bm25 import BM25Okapi
        log.info("正在构建 BM25 索引（%d chunks）...", len(chunks))
        bm25 = BM25Okapi([list(jieba.cut(c)) for c in chunks])
        with open(cache, "wb") as f:
            pickle.dump({"format": 2, "rev": rev, "chunks": chunks,
                         "metadatas": metadatas, "bm25": bm25}, f)
        return chunks, metadatas, bm25


_mgr: KBManager | None = None
_mgr_lock = threading.Lock()


def get_kb_manager() -> KBManager:
    """进程内单例。"""
    global _mgr
    if _mgr is None:
        with _mgr_lock:
            if _mgr is None:
                _mgr = KBManager()
    return _mgr

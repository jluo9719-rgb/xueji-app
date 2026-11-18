"""
索引管理：chunks 缓存 / ChromaDB 向量库 / BM25 索引 的构建与缓存。

修复点（对应说明文档）：
  - BUG-02：缓存失效分支补上 collection.add，空向量库问题根除
  - 缓存版本机制沿用原 cache_utils 设计（文档 MD5 + 代码版本号）
  - 路径全部来自 config.yaml，不再硬编码
"""
import hashlib
import json
import os
import pickle
import shutil

import chromadb
import jieba
from rank_bm25 import BM25Okapi

from core.config import config
from core.log import get_logger

log = get_logger(__name__)

# 切分逻辑或向量模型变更时 +1，强制重建缓存
CODE_VERSION = "2.0"
EMBED_VERSION = "2.0"


class IndexManager:
    """管理一个知识库的 chunks / 向量库 / BM25 三件套。"""

    def __init__(self, doc_paths: list[str], embedder):
        self.doc_paths = doc_paths
        self.embedder = embedder
        self.cache_dir = config.abspath(config.get("storage.cache_dir", "./cache"))
        self.chroma_path = config.abspath(config.get("storage.chroma_path", "./chroma_db"))
        self.chunks_cache = os.path.join(self.cache_dir, "chunks_cache.json")
        self.bm25_cache = os.path.join(self.cache_dir, "bm25_index.pkl")
        self.key_file = os.path.join(self.cache_dir, "cache_key.txt")
        self.version_file = os.path.join(self.cache_dir, "vector_version.txt")

        os.makedirs(self.cache_dir, exist_ok=True)
        self.collection = None
        self.bm25_index: BM25Okapi | None = None
        self.chunks: list[str] = []

    # ---------- 缓存 key ----------
    def _docs_hash(self) -> str | None:
        """所有源文档内容的联合 MD5；任一文档变化即失效。"""
        if not all(os.path.exists(p) for p in self.doc_paths):
            return None
        md5 = hashlib.md5()
        for p in sorted(self.doc_paths):
            with open(p, "rb") as f:
                md5.update(f.read())
        return md5.hexdigest()

    def _cache_key(self) -> str | None:
        h = self._docs_hash()
        return None if h is None else f"{h}_{CODE_VERSION}"

    def _is_cache_valid(self) -> bool:
        key = self._cache_key()
        if key is None or not os.path.exists(self.key_file):
            return False
        with open(self.key_file, "r", encoding="utf-8") as f:
            if f.read().strip() != key:
                return False
        return os.path.exists(self.chunks_cache)

    def _is_vectorstore_valid(self) -> bool:
        if not os.path.exists(self.chroma_path) or not os.path.exists(self.version_file):
            return False
        with open(self.version_file, "r", encoding="utf-8") as f:
            return f.read().strip() == EMBED_VERSION

    def _save_key(self) -> None:
        with open(self.key_file, "w", encoding="utf-8") as f:
            f.write(self._cache_key())

    def _save_vector_version(self) -> None:
        with open(self.version_file, "w", encoding="utf-8") as f:
            f.write(EMBED_VERSION)

    # ---------- 构建 ----------
    def build(self) -> None:
        """主入口：确保 chunks / 向量库 / BM25 全部就绪。"""
        cache_valid = self._is_cache_valid()
        vector_valid = self._is_vectorstore_valid()

        if cache_valid:
            with open(self.chunks_cache, "r", encoding="utf-8") as f:
                self.chunks = json.load(f)
            log.info("✅ 从缓存加载 %d 个 chunks", len(self.chunks))
        else:
            self.chunks = self._generate_chunks()
            with open(self.chunks_cache, "w", encoding="utf-8") as f:
                json.dump(self.chunks, f, ensure_ascii=False, indent=2)
            self._save_key()
            log.info("💾 已生成并缓存 %d 个 chunks", len(self.chunks))

        client = chromadb.PersistentClient(path=self.chroma_path)
        self.collection = client.get_or_create_collection(name="ljs")

        if not vector_valid or self.collection.count() != len(self.chunks):
            # BUG-02 修复：无论哪个分支进来，都完整执行"重建 + 写入 + 存版本"
            if os.path.exists(self.chroma_path) and self.collection.count() != len(self.chunks):
                log.info("向量库内容与 chunks 不一致，重建 ...")
                client.delete_collection("ljs")
                self.collection = client.get_or_create_collection(name="ljs")
            if self.collection.count() == 0:
                log.info("正在向量化并写入 %d 个 chunks ...", len(self.chunks))
                vectors = self.embedder.encode(self.chunks)
                self.collection.add(
                    documents=self.chunks,
                    embeddings=vectors,
                    ids=[f"id_policy_{i}" for i in range(len(self.chunks))],
                )
            self._save_vector_version()
            log.info("✅ 向量库就绪（%d 条）", self.collection.count())
        else:
            log.info("✅ 向量库已是最新（%d 条）", self.collection.count())

        self._ensure_bm25()

    def _generate_chunks(self) -> list[str]:
        from rag.chunker import chunk_document
        from rag.loader import load_document
        all_chunks: list[str] = []
        for p in self.doc_paths:
            text = load_document(p)
            all_chunks.extend(chunk_document(self.embedder, text))
        return all_chunks

    def _ensure_bm25(self) -> None:
        if os.path.exists(self.bm25_cache) and self._is_cache_valid():
            try:
                with open(self.bm25_cache, "rb") as f:
                    self.bm25_index = pickle.load(f)
                log.info("✅ BM25 索引自缓存加载")
                return
            except Exception as e:
                log.warning("BM25 缓存损坏，重建：%s", e)
        log.info("正在构建 BM25 索引 ...")
        tokenized = [list(jieba.cut(c)) for c in self.chunks]
        self.bm25_index = BM25Okapi(tokenized)
        with open(self.bm25_cache, "wb") as f:
            pickle.dump(self.bm25_index, f)
        log.info("💾 BM25 索引已缓存")

"""
混合检索器：向量检索 + BM25 → 去重合并 → reranker 重排。

修复点：
  - BUG-06：reranker 全局单例，不再每次检索重新加载 1.3GB 模型
  - Q-10：函数名拼写修正（multi_retrieve / rag_retrieve）
  - rerank 结果附带 chunk 下标，为后续引用溯源（5.4）预留 metadata 通道
  - 第五期：默认走 KBManager 快照（检索随用户上传文档走）；旧 index_manager
    签名保留兼容 demo.py；空库优雅降级（retrieve 返回空串）
"""
import numpy as np

from core.config import config
from core.log import get_logger
from rag.kb_manager import DEFAULT_KB_ID

log = get_logger(__name__)


class Reranker:
    """bge-reranker 单例包装。首次使用时才真正加载模型。"""

    _instance: "Reranker | None" = None

    def __init__(self):
        self._model = None

    @classmethod
    def instance(cls) -> "Reranker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_loaded(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder
            model_name = config.get("rerank.model", "BAAI/bge-reranker-large")
            log.info("加载重排模型 %s（仅此一次）...", model_name)
            self._model = CrossEncoder(model_name)

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        if not docs:
            return np.array([])
        self._ensure_loaded()
        pairs = [[query, d] for d in docs]
        import torch
        return self._model.predict(pairs, activation_fn=torch.nn.Sigmoid())

    def rerank(self, query: str, docs: list[str], top_n: int | None = None) -> list[str]:
        """返回按相关性降序的文档列表。"""
        top_n = top_n or config.get("retrieval.rerank_top_n", 5)
        scores = self.score(query, docs)
        order = scores.argsort()[::-1]
        return [docs[i] for i in order[:top_n]]


class HybridRetriever:
    """对索引执行混合检索 + 重排。

    两种后端：
      - KBManager 快照（第五期默认）：kb_id → 用户上传文档的实时索引
      - index_manager（第一期旧路径）：demo.py 等脚本兼容保留
    """

    def __init__(self, index_manager=None, embedder=None,
                 kb_id: str = DEFAULT_KB_ID):
        self.kb_id = kb_id
        self.index = index_manager      # rag.indexer.IndexManager（旧路径）
        if index_manager is None:
            from rag.kb_manager import get_embedder, get_kb_manager
            self.kb = get_kb_manager()
            self.embedder = embedder or get_embedder()
        else:
            self.kb = None
            self.embedder = embedder
        self.reranker = Reranker.instance()

    def is_empty(self) -> bool:
        """知识库没有任何可检索内容。"""
        if self.kb is not None:
            return self.kb.is_empty(self.kb_id)
        return len(self.index.chunks) == 0

    def _query_collection(self):
        return self.kb.collection if self.kb is not None else self.index.collection

    def _bm25_pair(self):
        """返回 (chunks, bm25)；kb 路径下 bm25 可能为 None（空库）。"""
        if self.kb is not None:
            chunks, _metadatas, bm25 = self.kb.get_snapshot(self.kb_id)
            return chunks, bm25
        return self.index.chunks, self.index.bm25_index

    def retrieve(self, query: str) -> str:
        """单查询混合检索，返回 Top N 文本块（换行连接）。空库/无命中返回空串。"""
        k_vec = config.get("retrieval.vector_top_k", 5)
        k_bm25 = config.get("retrieval.bm25_top_k", 5)
        top_n = config.get("retrieval.rerank_top_n", 5)

        # 向量通道
        q_vec = self.embedder.encode([query])
        query_kw = {"where": {"kb_id": self.kb_id}} if self.kb is not None else {}
        res = self._query_collection().query(query_embeddings=q_vec,
                                             n_results=k_vec, **query_kw)
        vec_docs = res["documents"][0] if res["documents"] else []

        # BM25 通道
        chunks, bm25 = self._bm25_pair()
        bm25_docs = []
        if bm25 is not None and chunks:
            import jieba
            scores = bm25.get_scores(list(jieba.cut(query)))
            top_idx = scores.argsort()[-k_bm25:][::-1]
            bm25_docs = [chunks[i] for i in top_idx]

        # 合并去重（保持出现顺序）
        combined, seen = [], set()
        for doc in list(vec_docs) + bm25_docs:
            if doc and doc not in seen:
                seen.add(doc)
                combined.append(doc)
        if not combined:
            log.info("检索无命中：%s...", query[:20])
            return ""

        result = self.reranker.rerank(query, combined, top_n=top_n)
        log.info("检索完成：%s... → %d/%d 个候选", query[:20], len(result), len(combined))
        return "\n".join(result)

    # 原多查询版检索（Multi-Query 改写在 tools/knowledge_search.py 中调用）
    def multi_retrieve(self, queries: list[str]) -> str:
        all_docs, seen = [], set()
        for q in queries:
            for doc in self.retrieve(q).split("\n"):
                if doc and doc not in seen:
                    seen.add(doc)
                    all_docs.append(doc)
        if not all_docs:
            return ""
        final = self.reranker.rerank(queries[0] if queries else "", all_docs,
                                     top_n=config.get("retrieval.rerank_top_n", 5))
        return "\n".join(final)

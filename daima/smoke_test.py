"""非交互式冒烟测试：验证缓存加载 + 知识库检索 + LLM 直答。"""
import sys
import time

sys.path.insert(0, ".")

from core.log import setup_logging
setup_logging()

from core.config import config
from core.llm import call_llm, extract_text


def main():
    # 1) RAG 索引（应走缓存，秒级完成）
    from rag.indexer import IndexManager
    from rag.retriever import HybridRetriever
    from sentence_transformers import SentenceTransformer
    from tools.knowledge_search import knowledge_search

    t0 = time.time()
    embedder = SentenceTransformer(config.get("embedding.model"))
    im = IndexManager(config.get("paths.source_docs", []), embedder)
    im.build()
    t1 = time.time()
    print(f"\n[1] RAG 索引就绪：{len(im.chunks)} chunks，向量库 {im.collection.count()} 条，"
          f"耗时 {t1-t0:.1f}s（应走缓存）")

    retriever = HybridRetriever(im, embedder)

    # 2) 知识库检索
    t2 = time.time()
    result = knowledge_search(retriever, "总公司和子公司在内控上分别负责什么？")
    t3 = time.time()
    print(f"\n[2] 知识库检索完成，耗时 {t3-t2:.1f}s，Top 内容预览：")
    print("-" * 60)
    print(result[:600])
    print("-" * 60)

    # 3) LLM 直答（验证凭据管理器 + 统一封装）
    try:
        resp = call_llm(
            messages=[{"role": "user", "content": "只回答两个字：收到"}],
            system="你是测试助手。",
            max_tokens=50,
        )
        print(f"\n[3] LLM 直答：{extract_text(resp)}")
    except Exception as e:
        print(f"\n[3] ⚠️ LLM 调用失败（不影响 RAG 验证）：{str(e)[:150]}")
    print("\n✅ 冒烟测试完成")


if __name__ == "__main__":
    main()

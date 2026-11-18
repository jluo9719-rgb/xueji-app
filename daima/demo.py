"""
第一期重构效果验证脚本（CLI）

功能：
  1. 初始化 RAG 索引（chunks / 向量库 / BM25，含缓存）
  2. 知识库检索演示（拆解 + Multi-Query + 混合检索 + 重排）
  3. 联网搜索演示（异步重试）
  4. LLM 直答演示（统一封装，密钥来自凭据管理器）

用法：
    python demo.py                 # 交互模式
    python demo.py --check         # 只做环境自检，不进入对话
    python demo.py --no-search     # 跳过联网搜索演示
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")

from core.log import setup_logging
setup_logging()  # 先配置日志（顺带强制控制台 UTF-8），再打印任何 emoji

from core.config import config
from core.credentials import get_anthropic_config, get_serper_key, mask
from core.log import get_logger
from core.llm import call_llm, extract_text

log = get_logger(__name__)

BANNER = """
============================================================
   多 Agent 智能体 — 第一期重构验证（RAG 管线 + 凭证系统）
============================================================
"""


def check_env() -> bool:
    """环境自检：配置 / 凭证 / 依赖模型。"""
    print("【1/4】配置文件 ...", end=" ")
    print("OK")
    print("       模型:", config.get("llm.model"),
          "| 重排:", config.get("rerank.model"),
          "| 向量:", config.get("embedding.model"))

    print("【2/4】凭证状态（Windows 凭据管理器 / 环境变量）")
    ok = True
    for name, fn in [("ANTHROPIC_API_KEY", lambda: get_anthropic_config()[0]),
                     ("ANTHROPIC_BASE_URL", lambda: get_anthropic_config()[1]),
                     ("SERPER_API_KEY", get_serper_key)]:
        try:
            v = fn()
            print(f"       {name:<20} ✅ {mask(v)}")
        except Exception as e:
            ok = False
            print(f"       {name:<20} ❌ {e}")

    print("【3/4】嵌入模型 m3e-base ...", end=" ")
    try:
        from sentence_transformers import SentenceTransformer
        embedder = SentenceTransformer(config.get("embedding.model"))
        print("OK")
    except Exception as e:
        print(f"FAIL: {e}")
        return False

    print("【4/4】源文档存在性")
    for p in config.get("paths.source_docs", []):
        import os
        exists = os.path.exists(p)
        print(f"       {'✅' if exists else '❌'} {p}")
        ok = ok and exists
    return ok


def build_rag():
    """构建 RAG 三件套，返回 (embedder, retriever)。"""
    from rag.indexer import IndexManager
    from rag.retriever import HybridRetriever
    from sentence_transformers import SentenceTransformer

    print("\n正在初始化 RAG 索引（首次运行需切分文档+向量化，请耐心等待）...")
    embedder = SentenceTransformer(config.get("embedding.model"))
    im = IndexManager(config.get("paths.source_docs", []), embedder)
    im.build()
    retriever = HybridRetriever(im, embedder)
    print(f"✅ RAG 就绪：{len(im.chunks)} chunks | 向量库 {im.collection.count()} 条\n")
    return embedder, retriever


async def demo_search():
    from tools.web_search import search_google_retry
    print("── 联网搜索演示 ──")
    q = input("搜索关键词（回车用默认）：").strip() or "北京今天天气"
    result = await search_google_retry(q)
    print(f"\n搜索结果：\n{result}\n")


def demo_knowledge(retriever):
    from tools.knowledge_search import knowledge_search
    print("── 知识库检索演示 ──")
    q = input("知识库问题（回车用默认）：").strip() or "总公司和子公司在内控上分别负责什么？"
    result = knowledge_search(retriever, q)
    print(f"\n检索到的 Top 内容：\n{'-'*60}\n{result}\n{'-'*60}\n")


def demo_llm():
    print("── LLM 直答演示（统一封装+重试，密钥走凭据管理器）──")
    resp = call_llm(
        messages=[{"role": "user", "content": "用一句话介绍你自己"}],
        system="你是一个测试助手，回答保持一句话。",
        max_tokens=100,
    )
    print(f"模型回复：{extract_text(resp)}\n")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只做环境自检")
    parser.add_argument("--no-search", action="store_true", help="跳过联网演示")
    args = parser.parse_args()

    print(BANNER)
    if not check_env():
        print("\n❌ 环境自检未通过，请先运行 python setup_credentials.py")
        return 1
    if args.check:
        print("\n✅ 环境自检通过")
        return 0

    _, retriever = build_rag()

    while True:
        print("可选演示：[1] 知识库检索  [2] 联网搜索  [3] LLM直答  [q] 退出")
        choice = input("请选择: ").strip().lower()
        if choice == "q":
            break
        try:
            if choice == "1":
                demo_knowledge(retriever)
            elif choice == "2" and not args.no_search:
                await demo_search()
            elif choice == "3":
                demo_llm()
            else:
                print("无效选择\n")
        except Exception as e:
            log.exception("演示出错")
            print(f"⚠️ 出错（已记录日志）：{e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

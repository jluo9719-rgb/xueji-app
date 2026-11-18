"""
知识库检索工具：查询拆解 + Multi-Query 改写 + 混合检索。
逻辑沿用原 query_rewrite + multi_retrive，LLM 调用走统一封装（带重试）。
"""
from core.config import config
from core.llm import call_llm, extract_text
from core.log import get_logger

log = get_logger(__name__)


def _split_query(query: str) -> list[str]:
    """判断是否需要拆解为多个子问题。返回子问题列表（至少一个）。"""
    prompt = f"""你是一个查询分析专家。请分析用户问题是否需要拆解为多个子问题
（例如：涉及多个对比对象、多个时间段、或多个并列概念）。
用户问题："{query}"
规则：
1. 如果需要拆分，必须严格按照此格式回答：拆分为：子问题1 | 子问题2
2. 如果不需要拆分，直接回答：不需要拆分
3. 只输出一行结果，不要输出任何其他解释。"""
    try:
        resp = call_llm(messages=[{"role": "user", "content": prompt}])
        result = extract_text(resp).strip().replace("拆分为：", "")
        if "不需要拆分" in result:
            return [query]
        subs = [s.strip() for s in result.split("|") if s.strip()]
        return subs[:config.get("retrieval.max_subqueries", 2)] or [query]
    except Exception as e:
        log.warning("问题拆解失败，使用原问题：%s", e)
        return [query]


def _multi_query(q: str) -> list[str]:
    """为一个子问题生成 N 个同义改写查询；失败时退回原查询。"""
    n = config.get("retrieval.multi_query_count", 3)
    prompt = (f"请针对以下问题生成{n}个意思相近但表达不同的搜索查询"
              f"（用于向量数据库检索），每行一个：\n问题：{q}")
    try:
        resp = call_llm(messages=[{"role": "user", "content": prompt}])
        import re
        lines = [re.sub(r"^\s*(?:\d+[.、)]|[-*])\s*", "", line).strip()
                 for line in extract_text(resp).splitlines() if line.strip()]
        return list(dict.fromkeys([q] + lines))[:n + 1]
    except Exception as e:
        log.warning("Multi-Query 生成失败，使用原查询：%s", e)
        return [q]


def knowledge_search(retriever, query: str) -> str:
    """完整知识库检索入口：拆解 → 改写 → 多路检索 → 终排。"""
    # 空库短路：不浪费 LLM 改写调用，让 Agent 转向联网搜索/自身知识
    if hasattr(retriever, "is_empty") and retriever.is_empty():
        log.info("知识库为空，跳过检索")
        return "（知识库当前为空，没有检索到任何资料。请基于你自己的知识回答，或改用联网搜索工具。）"
    if not config.get("retrieval.rewrite_enabled", False):
        return retriever.retrieve(query)
    sub_queries = _split_query(query)
    all_queries: list[str] = []
    for sq in sub_queries:
        all_queries.extend(_multi_query(sq))
    if not all_queries:
        all_queries = [query]
    log.info("改写后共 %d 个查询：%s", len(all_queries), all_queries)
    return retriever.multi_retrieve(all_queries)

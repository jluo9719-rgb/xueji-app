"""
Serper 联网搜索工具：异步 + 指数退避重试（沿用原逻辑）。
修复点：
  - Q-04：所有分支显式 return，不再隐式返回 None
  - L281 硬编码"温度："文案移除，answerBox 内容原样返回
"""
import asyncio
import os

import aiohttp

from core.config import config
from core.credentials import get_serper_key
from core.log import get_logger

log = get_logger(__name__)


async def search_google(query: str) -> str:
    url = "https://google.serper.dev/search"
    header = {"X-API-KEY": get_serper_key()}
    data = {"q": query}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=header,
                                    timeout=10) as response:
                if response.status != 200:
                    return f"状态码错误：{response.status}"
                try:
                    jr = await response.json()
                except Exception:
                    return "返回数据解析失败"
                if jr.get("error"):
                    return f"API错误:{jr['error']}"
                answer = jr.get("answerBox") or {}
                if answer:
                    parts = [f"{k}：{v}" for k, v in answer.items()
                             if k in ("answer", "source", "sourceLink") and v]
                    return "；".join(parts) or str(answer)
                organic = (jr.get("organic") or [])[:3]
                lines = [f"标题：{c.get('title','')}。链接：{c.get('link','')}。"
                         f"摘要：{c.get('snippet','')}" for c in organic]
                return "参考链接\n\n" + "\n\n".join(lines) if lines else "未找到相关结果"
    except aiohttp.ClientError as e:
        return f"搜索失败：{e}"
    except Exception as e:  # Q-04：兜底，绝不隐式返回 None
        return f"搜索异常：{e}"


async def search_google_retry(query: str, max_retries: int | None = None) -> str:
    """可重试搜索：5xx/429/520/503 与网络异常按指数退避重试。"""
    max_retries = max_retries or 3
    result = ""
    for attempt in range(max_retries):
        try:
            result = await search_google(query)
            if any(code in result for code in ("401", "400")):
                return result  # 认证/参数错误，重试无意义
            if any(code in result for code in ("500", "429", "520", "503")):
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    log.warning("搜索被限流，%ds 后重试", wait)
                    await asyncio.sleep(wait)
                    continue
                return result
            return result
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return f"检索失败：{e}"
    return result or "搜索失败：未知原因"

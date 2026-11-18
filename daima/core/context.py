"""
上下文压缩模块：token 计数 → 超阈值时旧对话 LLM 总结 + 强制窗口遗忘。

修复点：
  - Q-03：tiktoken 编码器全局创建一次（原代码每次调用都重建）
  - 原代码注释写 20000 token 实际阈值 2000 → 阈值统一走 config.yaml
"""
import tiktoken

from core.config import config
from core.llm import call_llm
from core.log import get_logger

log = get_logger(__name__)

# 全局唯一编码器（Q-03）
_ENCODING = None


def _get_encoding():
    global _ENCODING
    if _ENCODING is None:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def count_tokens(text: str) -> int:
    return len(_get_encoding().encode(text))


def count_messages_tokens(messages: list) -> int:
    """统计 messages 列表总 token：支持 str 与 block 列表两种 content。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("text"):
                        total += count_tokens(block["text"])
                    elif block.get("content"):     # tool_result 的 content
                        c = block["content"]
                        total += count_tokens(c) if isinstance(c, str) else 0
                else:                              # SDK 对象（thinking/text/tool_use）
                    if getattr(block, "text", None):
                        total += count_tokens(block.text)
                    elif getattr(block, "thinking", None):
                        total += count_tokens(block.thinking)
    return total


class ContextCompressor:
    """对话历史压缩器：软压缩（总结）+ 硬遗忘（窗口截断）。"""

    def __init__(self):
        self.threshold = config.get("context.compress_threshold_tokens", 2000)
        self.keep_pairs = config.get("context.keep_recent_pairs", 5)
        self.hard_window = self.keep_pairs * 6   # 硬遗忘上限（条数）

    def need_compress(self, messages: list) -> bool:
        return count_messages_tokens(messages) > self.threshold

    def compress(self, messages: list) -> list:
        """
        主入口：超过阈值时，把旧对话总结成一条 system 消息，
        保留最近 N 对完整对话。
        """
        if not self.need_compress(messages):
            return messages

        keep = 2 * self.keep_pairs
        cut = self._safe_cut(messages, max(0, len(messages) - keep))
        old_messages = messages[:cut]
        recent = messages[cut:]

        if not old_messages:
            return messages

        summary = self._summarize(old_messages)
        log.info("上下文压缩：%d 条旧消息 → 总结 %d 字，保留最近 %d 条",
                 len(old_messages), len(summary), len(recent))
        return [{"role": "system", "content": f"对话历史总结：{summary}"}] + recent

    def force_window(self, messages: list) -> list:
        """硬性长度窗口：条数超限时直接丢最旧的（原 L598 的主动遗忘机制）。"""
        if len(messages) <= self.hard_window:
            return messages
        cut = self._safe_cut(messages, len(messages) - self.hard_window)
        if not cut:
            return messages
        prefix = [messages[0]] if messages[0].get("role") == "system" else []
        return prefix + messages[cut:]

    @staticmethod
    def _safe_cut(messages: list, desired: int) -> int:
        # 仅在真实用户轮次边界截断，不能拆开 tool_use / tool_result。
        boundaries = [i for i, message in enumerate(messages)
                      if i <= desired and message.get("role") == "user"
                      and isinstance(message.get("content"), str)
                      and not message["content"].startswith("请结合这个新线索继续检索：")]
        return max(boundaries, default=0)

    def _summarize(self, old_messages: list) -> str:
        lines = []
        for m in old_messages:
            role = "用户" if m.get("role") == "user" else "AI"
            content = m.get("content", "")
            if isinstance(content, list):   # 工具调用块转文字摘要
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("text"):
                            parts.append(b["text"])
                        elif b.get("name"):
                            parts.append(f"[调用工具 {b['name']}]")
                content = " ".join(parts)
            lines.append(f"{role}：{content}")

        prompt = f"""请简洁总结以下对话，提取：
1. 用户关键需求
2. 已确定的偏好/限制
3. 重要上下文
只返回一段总结，不要其他内容。
对话：
{chr(10).join(lines)}"""
        try:
            resp = call_llm(
                messages=[{"role": "user", "content": prompt}],
                system="你是一个对话压缩专家，用最简洁的话总结对话核心内容。",
            )
            from core.llm import extract_text
            return extract_text(resp).strip() or "无有效内容"
        except Exception as e:
            log.warning("对话总结失败，使用简单截断：%s", str(e)[:120])
            return "（历史对话较多，已省略早期内容）"

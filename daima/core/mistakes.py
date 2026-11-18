"""
错题本模块（第六期）：SQLite 知识点卡片存储 + LLM 收录判断 + Markdown 导出。

产品定位（学习/备考助手）的沉淀环节：
  聊天暴露薄弱点 → 错题本卡片 → （七期）出类似题检测 → 更新掌握状态

卡片来源：
  - 自动捕捉：engine 每轮回答后由 judge_exchange() 判断"是否暴露知识困惑"
  - 手动收录：前端点"收录"，对会话最后一轮问答以 force=True 强制提取

失败策略：收录是增强功能，LLM 调用失败一律静默跳过，绝不阻断对话主流程。
"""
import sqlite3
from collections import OrderedDict
from datetime import datetime

from core.config import config
from core.log import get_logger

log = get_logger(__name__)

STATUS_UNLEARNED = "未掌握"
STATUS_LEARNED = "已掌握"

_JUDGE_PROMPT = """判断下面这轮问答是否暴露了学习者的知识困惑或易错点。{force_note}
判定为"是"的典型情况：问了某个概念或原理说明没掌握、回答纠正了学习者的误解、
询问易混淆知识的对比、对某个知识点反复追问。
判定为"否"的典型情况：寒暄闲聊、让助手总结翻译或写作、操作类指令、
无困惑表现的事实性查询。

用户提问：{query}

助手回答：{answer}

如果判定为"是"，严格按照以下三行格式输出：
收录
主题：〈知识点主题，不超过10个字〉
混淆点：〈学习者容易搞混或出错的地方，不超过50个字〉

否则只输出一个词：跳过"""


class MistakeBook:
    """基于 SQLite 的错题本（知识点卡片库）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.abspath(
            config.get("storage.mistakes_db", "./data/mistakes.db"))
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_table()

    def _init_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS mistakes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT DEFAULT '',
                confusion TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                session_id TEXT DEFAULT '',
                session_title TEXT DEFAULT '',
                status TEXT DEFAULT '未掌握',
                created_at TEXT,
                updated_at TEXT
            )
        """)
        self.conn.commit()

    # ---------- 写入 ----------
    def add(self, question: str, answer: str, confusion: str, topic: str = "",
            session_id: str = "", session_title: str = "") -> int:
        """收录一张卡片，返回卡片 id。answer 截断存储避免库膨胀。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = self.conn.execute(
            "INSERT INTO mistakes (topic, confusion, question, answer, session_id,"
            " session_title, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (topic, confusion, question, answer[:3000], session_id,
             session_title, STATUS_UNLEARNED, now, now))
        self.conn.commit()
        log.info("📕 错题本收录 #%d：%s（来源：%s）",
                 cur.lastrowid, topic or confusion[:20], session_title or session_id)
        return cur.lastrowid

    def get(self, mistake_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM mistakes WHERE id = ?", (mistake_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def set_status(self, mistake_id: int, status: str) -> bool:
        """切换掌握状态（未掌握 ↔ 已掌握）。"""
        if status not in (STATUS_UNLEARNED, STATUS_LEARNED):
            raise ValueError(f"非法状态：{status}（只能为 未掌握/已掌握）")
        cur = self.conn.execute(
            "UPDATE mistakes SET status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mistake_id))
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, mistake_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM mistakes WHERE id = ?", (mistake_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self.conn.close()

    # ---------- 读取 ----------
    @staticmethod
    def _row_to_dict(row) -> dict:
        cols = ["id", "topic", "confusion", "question", "answer",
                "session_id", "session_title", "status", "created_at", "updated_at"]
        return dict(zip(cols, row))

    def list_all(self, status: str | None = None) -> list[dict]:
        """全部卡片（新在前），可按状态过滤。"""
        if status:
            rows = self.conn.execute(
                "SELECT * FROM mistakes WHERE status = ? ORDER BY id DESC",
                (status,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM mistakes ORDER BY id DESC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count(self) -> dict:
        total, learned = self.conn.execute(
            "SELECT COUNT(*), SUM(status = '已掌握') FROM mistakes").fetchone()
        total = total or 0
        learned = learned or 0
        return {"total": total, "learned": learned, "unlearned": total - learned}

    # ---------- 导出 ----------
    def export_markdown(self) -> str:
        """错题本整体导出为 Markdown（按主题分组，未掌握优先展示）。"""
        cards = self.list_all()
        stats = self.count()
        lines = [
            "# 我的错题本",
            "",
            f"> 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}"
            f" · 共 {stats['total']} 题（未掌握 {stats['unlearned']}"
            f" / 已掌握 {stats['learned']}）",
            "",
        ]
        if not cards:
            lines.append("（错题本还是空的，去聊天里暴露一些困惑吧）")
            return "\n".join(lines)

        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for c in cards:
            groups.setdefault(c["topic"] or "未分类", []).append(c)
        # 未掌握多的主题排前面
        topics = sorted(groups.keys(),
                        key=lambda t: sum(1 for c in groups[t]
                                          if c["status"] == STATUS_LEARNED))
        n = 0
        for topic in topics:
            lines += [f"## {topic}", ""]
            for c in groups[topic]:
                n += 1
                mark = "✅ 已掌握" if c["status"] == STATUS_LEARNED else "❌ 未掌握"
                ans = c["answer"]
                if len(ans) > 600:
                    ans = ans[:600] + "……"
                src = c["session_title"] or c["session_id"] or "未知会话"
                lines += [
                    f"### {n}. {c['confusion']} 【{mark}】",
                    "",
                    f"- **原问题**：{c['question']}",
                    f"- **混淆点**：{c['confusion']}",
                    f"- **当时解答**：{ans.replace(chr(10), ' ')}",
                    f"- 来源：{src} · {c['created_at'][:10]}",
                    "",
                ]
        return "\n".join(lines)

    def deduplicate_cards(self, threshold: float = 0.85) -> int:
        """去重相似卡片：topic + confusion 向量化，余弦相似度 > threshold 的合并

        Args:
            threshold: 相似度阈值（0~1）

        Returns:
            合并的卡片数
        """
        from rag.kb_manager import get_embedder
        import numpy as np

        all_cards = self.list_all()
        if len(all_cards) < 2:
            return 0

        # 向量化
        embedder = get_embedder()
        texts = [f"{c['topic'] or ''} {c['confusion']}" for c in all_cards]
        vectors = embedder.encode(texts, normalize_embeddings=True)

        # 计算相似度矩阵，找出重复组
        merged_count = 0
        visited = set()

        for i, card_i in enumerate(all_cards):
            if card_i["id"] in visited:
                continue

            # 找出与 card_i 相似的所有卡片
            similar_ids = []
            for j in range(i + 1, len(all_cards)):
                if all_cards[j]["id"] in visited:
                    continue
                sim = np.dot(vectors[i], vectors[j])
                if sim >= threshold:
                    similar_ids.append(all_cards[j]["id"])

            # 合并：保留 card_i，删除相似卡片
            if similar_ids:
                visited.add(card_i["id"])
                for sid in similar_ids:
                    self.delete(sid)
                    visited.add(sid)
                    merged_count += 1
                log.info(f"错题去重：#{card_i['id']} 合并了 {len(similar_ids)} 张相似卡片")

        return merged_count


async def judge_exchange(query: str, answer: str,
                         force: bool = False) -> dict | None:
    """
    LLM 判断一轮问答是否值得收录进错题本。
    返回 {"topic", "confusion"}；不值得收录或 LLM 失败返回 None。
    force=True：用户手动确认收录，跳过"是否收录"判断只提取字段。
    """
    from core.llm import acall_llm, extract_text
    prompt = _JUDGE_PROMPT.format(
        force_note="（学习者已手动确认要收录本题，请直接按格式输出）" if force else "",
        query=query[:800], answer=answer[:1500])
    try:
        resp = await acall_llm(
            messages=[{"role": "user", "content": prompt}],
            system="你是学习诊断助手，负责从问答中识别易错知识点",
            max_tokens=config.get("llm.max_tokens_util", 400))
        text = extract_text(resp).strip()
    except Exception as e:
        log.warning("错题判断 LLM 调用失败（跳过收录）：%s", str(e)[:150])
        return None
    log.info("错题判断结果：%s", text[:60])

    if "收录" not in text and not force:
        return None
    topic, confusion = "", ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("主题：", "主题:")):
            topic = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        elif line.startswith(("混淆点：", "混淆点:")):
            confusion = line.split("：", 1)[-1].split(":", 1)[-1].strip()
    if not confusion:                     # 模型没按格式时兜底
        confusion = query[:40] or "未概括"
    return {"topic": topic[:20], "confusion": confusion[:80]}

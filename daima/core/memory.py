"""
长时记忆模块：SQLite 存储用户实体（偏好/限制），注入 system prompt。

修复点（对应说明文档）：
  - BUG-01：原代码 prompt 缺 f 前缀，{query} 原样发出，提取从未生效 → 已修
  - BUG-04：原代码每次提取到新实体先 DELETE 全表，旧记忆被抹掉
            → 改为纯 upsert（key 相同更新、不同新增），记忆可持续累积
"""
import json
import sqlite3

from core.config import config
from core.llm import call_llm, acall_llm, extract_text
from core.log import get_logger

log = get_logger(__name__)

_EXTRACT_PROMPT = """判断以下用户消息是否包含用户偏好或限制信息（如过敏、口味、宗教、金额限制等）。
如果有，请提取为JSON数组格式，每行一个字典：每行格式：{{"key": "实体名", "value": "实体值"}}
如果没有相关信息，请只返回字符串"无"。
用户消息：{query}
只返回提取结果，不要其他解释。"""


class LongTermMemory:
    """基于 SQLite 的实体记忆库。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.abspath(
            config.get("storage.sqlite_path", "./entity_memory.db"))
        self.conn = sqlite3.connect(self.db_path)
        self._init_table()

    def _init_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS entities(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_key TEXT UNIQUE,
                entity_value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # entity_key 加 UNIQUE 约束配合 INSERT OR REPLACE 实现 upsert；
        # 老库若已存在无约束的表，建索引兜底
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_key ON entities(entity_key)")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    # ---------- 提取 ----------
    def extract_from(self, query: str) -> int:
        """
        用 LLM 从用户消息中提取实体并入库。
        返回本次写入条数。失败静默返回 0（记忆是增强功能，不能阻断主流程）。
        """
        prompt = _EXTRACT_PROMPT.format(query=query)   # BUG-01 修复：真正的 f-string 替代
        try:
            resp = call_llm(
                messages=[{"role": "user", "content": prompt}],
                system="你是一个实体识别专家，从文本中提取偏好与限制信息，返回JSON格式",
            )
            result = extract_text(resp).strip()
        except Exception as e:
            log.warning("实体提取 LLM 调用失败（跳过）：%s", str(e)[:150])
            return 0

        return self._save_result(result)

    async def aextract_from(self, query: str) -> int:
        try:
            response = await acall_llm(
                [{"role": "user", "content": _EXTRACT_PROMPT.format(query=query)}],
                system="提取用户偏好与限制，返回 JSON 数组；没有则返回无。")
            return self._save_result(extract_text(response).strip())
        except Exception as error:
            log.warning("记忆提取失败（跳过）：%s", type(error).__name__)
            return 0

    def _save_result(self, result: str) -> int:
        if not result or "无" in result[:5]:
            return 0
        # 兼容模型输出被 ```json 包裹的情况
        cleaned = result.replace("```json", "").replace("```", "").strip()
        try:
            entities = json.loads(cleaned)
            if isinstance(entities, dict):       # 单对象容错 {"key":..,"value":..}
                entities = [entities]
        except json.JSONDecodeError:
            log.warning("实体提取结果非合法 JSON，跳过：%s", cleaned[:100])
            return 0

        if not isinstance(entities, list):
            return 0
        saved = 0
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            key, value = ent.get("key"), ent.get("value")
            if key and value:
                # BUG-04 修复：不再 DELETE 全表，直接 upsert
                self.conn.execute(
                    "INSERT OR REPLACE INTO entities (entity_key, entity_value) "
                    "VALUES (?, ?)", (str(key), str(value)))
                saved += 1
        if saved:
            self.conn.commit()
            log.info("长时记忆更新 %d 条", saved)
        return saved

    # ---------- 读取 ----------
    def load_memory_text(self) -> str:
        """全部记忆转成提示文字；无记忆返回空串。"""
        rows = self.conn.execute(
            "SELECT entity_key, entity_value FROM entities").fetchall()
        if not rows:
            return ""
        return "用户记忆：" + "，".join(f"{k}={v}" for k, v in rows)

    def list_all(self) -> list[tuple[str, str]]:
        """供未来记忆管理页使用。"""
        return self.conn.execute(
            "SELECT entity_key, entity_value FROM entities ORDER BY id").fetchall()

    def delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM entities WHERE entity_key = ?", (key,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

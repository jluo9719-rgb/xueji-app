"""
会话持久化模块：SQLite 为主存储，Redis 为可选镜像；首次可导入旧 Redis。

多会话键设计（第三期升级）：
  session:{id}          → 某个会话的 messages JSON
  sessions:index        → 会话索引（JSON 数组：[{id, title, created_at}]）

修复点：
  - Q-15：原代码 Redis 连不上直接 exit(1) → 现在降级运行并告警
"""
import json

from core.config import config
from core.log import get_logger

log = get_logger(__name__)

DEFAULT_SESSION_ID = "default"

# 反思机制注入的合成提问（不是用户真实发言，统计轮次/自动标题时要排除）
SYNTHETIC_HINT = "请结合这个新线索继续检索："


def count_user_turns(messages: list) -> int:
    """
    统计真实用户提问轮次。

    会话消息里混有两种非用户发言的 role=user 记录，不能直接数：
      - 工具回传：content 是 block 列表而非字符串
      - 反思补检索提示：content 是以 SYNTHETIC_HINT 开头的字符串
    早期实现把它们都算成一轮，导致用过检索工具的会话 user_turns 永远 > 1，
    首轮提问自动改标题的逻辑（api/app.py）因此失效，时间线显示"会话 s_xxx"。
    """
    return sum(1 for m in messages
               if m.get("role") == "user"
               and isinstance(m.get("content"), str)
               and not m["content"].startswith(SYNTHETIC_HINT))


class SessionStore:
    """多会话存储。SQLite 持久化；Redis 可用时同步镜像。"""

    def __init__(self):
        self._memory: dict[str, str] = {}     # 降级后备：与 Redis 同构的 KV
        self.redis = None
        import sqlite3
        from pathlib import Path
        db_path = config.abspath(config.get("storage.sessions_db", "./data/sessions.db"))
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS session_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.commit()
        if config.get("redis.enabled", True):
            self._connect()

    def _connect(self) -> None:
        import redis
        host = config.get("redis.host", "localhost")
        port = config.get("redis.port", 6380)
        timeout = config.get("redis.socket_timeout", 5)
        try:
            client = redis.Redis(host=host, port=port,
                                 socket_timeout=timeout,
                                 decode_responses=True)
            client.ping()
            self.redis = client
            if not self._db.execute("SELECT 1 FROM session_kv WHERE key='migration:redis'").fetchone():
                # 仅首次从旧 Redis 导入；以后 SQLite 为准，避免旧缓存复活已删会话。
                if not self._db.execute("SELECT 1 FROM session_kv WHERE key='sessions:index'").fetchone():
                    raw_index = client.get("sessions:index")
                    if raw_index:
                        items = json.loads(raw_index)
                        for item in items:
                            key = "session:" + item["id"]
                            value = client.get(key)
                            if value:
                                self._db.execute("INSERT OR IGNORE INTO session_kv VALUES (?,?)", (key, value))
                        self._db.execute("INSERT OR IGNORE INTO session_kv VALUES (?,?)", ("sessions:index", raw_index))
                self._db.execute("INSERT OR REPLACE INTO session_kv VALUES ('migration:redis','done')")
                self._db.commit()
            log.info("✅ Redis 会话存储已连接（%s:%d）", host, port)
        except Exception as e:
            self.redis = None
            log.warning("⚠️ Redis 连接失败（%s:%d）：%s\n"
                        "   已降级为 SQLite 会话，"
                        "程序退出后会话仍会保留。", host, port, str(e)[:120])

    @property
    def persistent(self) -> bool:
        return True

    # ---------- 底层 KV ----------
    def _get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM session_kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO session_kv VALUES (?, ?)", (key, value))
        self._db.commit()
        try:
            if self.redis:
                self.redis.set(key, value)
                return
        except Exception as e:
            log.warning("Redis 写入失败，转内存：%s", str(e)[:100])
            self.redis = None
        self._memory[key] = value

    def _delete(self, key: str) -> None:
        try:
            if self.redis:
                self.redis.delete(key)
        except Exception as e:
            log.warning("Redis 删除失败，转内存：%s", str(e)[:100])
            self.redis = None
        self._memory.pop(key, None)
        self._db.execute("DELETE FROM session_kv WHERE key=?", (key,))
        self._db.commit()

    # ---------- 会话索引 ----------
    def _load_index(self) -> list[dict]:
        data = self._get("sessions:index")
        return json.loads(data) if data else []

    def _save_index(self, index: list[dict]) -> None:
        self._set("sessions:index", json.dumps(index, ensure_ascii=False))

    def _touch_index(self, session_id: str, title: str | None = None,
                     stats: dict | None = None) -> None:
        """确保会话在索引里；不存在则登记。每次保存都刷新 updated_at 与消息统计。"""
        index = self._load_index()
        for item in index:
            if item["id"] == session_id:
                if title:
                    item["title"] = title
                if stats:
                    item.update(stats)
                item["updated_at"] = _now()
                self._save_index(index)
                return
        new_item = {"id": session_id,
                    "title": title or f"会话 {session_id}",
                    "created_at": _now(),
                    "updated_at": _now()}
        if stats:
            new_item.update(stats)
        index.insert(0, new_item)
        self._save_index(index)

    # ---------- 公开接口 ----------
    def create_session(self, session_id: str, title: str | None = None) -> dict:
        """新建（或重置）一个会话。返回会话元信息。"""
        meta = {"id": session_id,
                "title": title or f"会话 {session_id}",
                "created_at": _now(),
                "updated_at": _now()}
        self.save_messages(session_id, [])
        # 索引置顶并更新标题
        index = [i for i in self._load_index() if i["id"] != session_id]
        index.insert(0, meta)
        self._save_index(index)
        log.info("已创建会话 %s", session_id)
        return meta

    def list_sessions(self) -> list[dict]:
        """全部会话元信息（新在前）。附带消息条数。

        统计在保存时冗余进索引，避免每次列出会话都全量解析各会话消息；
        旧索引项没有统计时惰性计算一次，下次保存补齐。
        """
        result = []
        for item in self._load_index():
            if "user_turns" in item:
                result.append({**item})
                continue
            msgs = self.load_messages(item["id"])
            result.append({**item,
                           "message_count": len(msgs),
                           "user_turns": count_user_turns(msgs)})
        return result

    def delete_session(self, session_id: str) -> bool:
        existed = any(i["id"] == session_id for i in self._load_index())
        self._delete(f"session:{session_id}")
        self._save_index([i for i in self._load_index()
                          if i["id"] != session_id])
        if existed:
            log.info("已删除会话 %s", session_id)
        return existed

    def rename_session(self, session_id: str, title: str) -> bool:
        index = self._load_index()
        for item in index:
            if item["id"] == session_id:
                item["title"] = title
                item["title_manual"] = True
                self._save_index(index)
                return True
        return False

    def archive_session(self, session_id: str, archived: bool) -> bool:
        index = self._load_index()
        for item in index:
            if item["id"] == session_id:
                item["archived"] = archived
                self._save_index(index)
                return True
        return False

    def load_messages(self, session_id: str = DEFAULT_SESSION_ID) -> list:
        data = self._get(f"session:{session_id}")
        return json.loads(data) if data else []

    def save_messages(self, session_id: str, messages: list,
                      title: str | None = None) -> None:
        self._set(f"session:{session_id}",
                  json.dumps(messages, ensure_ascii=False))
        self._touch_index(session_id, title,
                          stats={"message_count": len(messages),
                                 "user_turns": count_user_turns(messages)})

    # ---------- 兼容旧接口（CLI engine 用 default 会话） ----------
    def save(self, messages: list, title: str | None = None) -> None:
        self.save_messages(DEFAULT_SESSION_ID, messages, title)

    def load(self) -> list:
        return self.load_messages(DEFAULT_SESSION_ID)


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

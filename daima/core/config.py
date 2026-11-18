"""配置加载模块：读取 config.yaml，提供全局唯一的 Config 对象。"""
import os
from pathlib import Path

import yaml

# 项目根目录 = daima/
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    """把 config.yaml 的嵌套字典包装成属性访问：cfg.llm.model"""

    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name: str):
        value = self._data[name]
        if isinstance(value, dict) and not isinstance(value, Config):
            return Config(value)
        return value

    def get(self, dotted: str, default=None):
        """支持点号路径取值：cfg.get('llm.model')"""
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def abspath(self, rel_path: str) -> str:
        """把相对路径解析为基于项目根目录的绝对路径"""
        p = Path(rel_path)
        return str(p if p.is_absolute() else (PROJECT_ROOT / p).resolve())

    def __repr__(self):
        return f"Config({self._data!r})"


def load_config(path: str | None = None) -> Config:
    cfg_path = Path(path) if path else PROJECT_ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    override = os.getenv("XUEJI_DATA_DIR")
    if override:
        base = Path(override).resolve()
        data.setdefault("storage", {}).update({
            "data_dir": str(base / "data"), "chroma_path": str(base / "chroma"),
            "cache_dir": str(base / "cache"), "sessions_db": str(base / "sessions.db"),
            "sqlite_path": str(base / "memory.db"), "mistakes_db": str(base / "mistakes.db")})
        data.setdefault("logging", {})["dir"] = str(base / "logs")
        data.setdefault("redis", {})["enabled"] = False
    return Config(data)


# 全局单例：各模块 from core.config import config 直接使用
config = load_config()

"""统一日志模块：控制台 + 按天滚动文件，替换原代码的全量 print（Q-07）。"""
import logging
import sys
from logging.handlers import TimedRotatingFileHandler

from core.config import config

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_configured = False


def _force_utf8_console():
    """Windows 控制台默认 GBK，emoji/特殊符号会崩，强制切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _force_utf8_console()
    level = getattr(logging, config.get("logging.level", "INFO").upper(), logging.INFO)
    log_dir = config.abspath(config.get("logging.dir", "./logs"))
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        from pathlib import Path
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            f"{log_dir}/agent.log", when="midnight", backupCount=14, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass  # 日志目录不可写时不致命
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)


# 安全红线：任何日志调用都不允许直接输出 api_key，
# 需要展示时使用 core.credentials.mask()

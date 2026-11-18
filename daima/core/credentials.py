"""
凭证安全管理模块（对应说明文档 BUG-08 / 8.6 节）

优先级：Windows 凭据管理器（keyring）→ .env 文件/环境变量兜底
密钥永不打印到日志。

分发模式：新用户复制 .env.example 为 .env，填入自己的 key/base_url/model。
开发者模式：setup_credentials.py 写入凭据管理器，优先于 .env。
"""
import os

from dotenv import load_dotenv

from core.config import config

# 项目根目录下的 .env（不存在则静默跳过；不覆盖已有环境变量）
load_dotenv(config.abspath(".env"))

# 凭据集名称：控制面板 → 凭据管理器 → Windows 凭据 里以此分组显示
SERVICE_NAME = config.get("credentials.service_name", "MultiAgentApp")

# .env 变量名映射
_ENV_NAMES = {
    "api_key": "ANTHROPIC_API_KEY",
    "base_url": "ANTHROPIC_BASE_URL",
    "serper_key": "SERPER_API_KEY",
}


def mask(value: str | None) -> str:
    """日志用掩码：只露末 4 位"""
    if not value:
        return "<未设置>"
    if len(value) <= 8:
        return "****"
    return f"****{value[-4:]}"


def get_credential(field: str, required: bool = True) -> str | None:
    """
    读取单个凭证字段。
    field 取值：api_key / base_url / serper_key
    """
    # 1) 系统凭据管理器（OS 级隐藏存储）
    try:
        import keyring
        value = keyring.get_password(SERVICE_NAME, field)
    except Exception:
        value = None
    # 2) .env 文件 / 环境变量兜底
    if not value:
        value = os.getenv(_ENV_NAMES.get(field, field.upper()))
    if required and not value:
        raise RuntimeError(
            f"缺少凭证 [{field}]。\n"
            f"请任选其一配置：\n"
            f"  a) 复制 .env.example 为 .env 并填入 {field.upper()}\n"
            f"  b) 运行 python setup_credentials.py 写入系统凭据管理器\n"
            f"  c) 设置环境变量 {field.upper()}"
        )
    return value


def set_credential(field: str, value: str) -> None:
    """写入/更新凭证（供 setup_credentials.py 使用）"""
    import keyring
    keyring.set_password(SERVICE_NAME, field, value)


def get_llm_model() -> str | None:
    """用户自定义模型名（.env 的 LLM_MODEL），未设置返回 None 走 config.yaml 默认。"""
    if config.get("llm.provider", "anthropic") == "ollama":
        return os.getenv("OLLAMA_MODEL") or config.get("llm.model", "qwen3:8b")
    return os.getenv("LLM_MODEL") or config.get("llm.model")


def get_anthropic_config() -> tuple[str, str]:
    """返回 (api_key, base_url)，base_url 缺省走官方地址"""
    provider = config.get("llm.provider", "anthropic")
    if provider == "ollama":
        # 本地模式不读旧云端凭据，避免模型名和服务地址意外混用。
        return "ollama", (os.getenv("OLLAMA_BASE_URL") or
                          config.get("llm.base_url", "http://localhost:11434"))
    if provider != "anthropic":
        raise ValueError(f"不支持的 llm.provider: {provider}")
    api_key = get_credential("api_key")
    base_url = get_credential("base_url", required=False) or "https://api.anthropic.com"
    return api_key, base_url


def get_serper_key() -> str:
    return get_credential("serper_key")

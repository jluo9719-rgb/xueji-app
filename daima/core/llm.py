"""统一 Anthropic Messages 协议入口：本地 Ollama / 兼容云端、超时、重试和耗时。"""
import asyncio
import time
from urllib.parse import urlparse

import anthropic
import httpx

from core.config import config
from core.credentials import get_anthropic_config, get_llm_model
from core.log import get_logger

log = get_logger(__name__)
_client = None
_async_client = None


def _client_kwargs(asynchronous=False):
    key, url = get_anthropic_config()
    kwargs = dict(api_key=key, base_url=url,
                  timeout=float(config.get('llm.llm_timeout', 120)), max_retries=0)
    # 仅回环地址不走系统代理；云端仍使用原来的网络配置。
    if urlparse(url).hostname in ('localhost', '127.0.0.1', '::1'):
        cls = httpx.AsyncClient if asynchronous else httpx.Client
        kwargs['http_client'] = cls(trust_env=False)
    else:
        kwargs['default_headers'] = {'User-Agent': 'Mozilla/5.0'}
    return kwargs


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(**_client_kwargs())
    return _client


def get_async_client():
    global _async_client
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic(**_client_kwargs(True))
    return _async_client


def _build_kwargs(messages, system=None, max_tokens=None, tools=None):
    # 历史总结必须放进顶层 system，不能作为 Messages API 的 role=system。
    summaries = [m['content'] for m in messages
                 if m.get('role') == 'system' and isinstance(m.get('content'), str)]
    kwargs = dict(model=get_llm_model(),
                  max_tokens=max_tokens or config.get('llm.max_tokens_util', 1200),
                  messages=[{k: v for k, v in m.items() if k in ('role', 'content')} for m in messages if m.get('role') != 'system'])
    combined = '\n\n'.join(filter(None, [system, *summaries]))
    if combined:
        kwargs['system'] = combined
    if tools:
        kwargs['tools'] = tools
    if config.get('llm.provider') == 'ollama' and not config.get('llm.thinking', False):
        kwargs['thinking'] = {'type': 'disabled'}
    return kwargs


def _is_quota_error(error):
    return any(word in str(error).lower() for word in
               ('quota', 'insufficient', 'billing', '欠费', '配额', '余额'))


def _retryable(error):
    if isinstance(error, anthropic.RateLimitError):
        return not _is_quota_error(error)
    return (isinstance(error, (anthropic.APIConnectionError, anthropic.APITimeoutError))
            or isinstance(error, anthropic.APIStatusError) and error.status_code >= 500)


def _record(response, started):
    usage = getattr(response, 'usage', None)
    log.info('LLM model=%s elapsed=%.2fs stop=%s input=%s output=%s',
             get_llm_model(), time.perf_counter() - started,
             getattr(response, 'stop_reason', None),
             getattr(usage, 'input_tokens', None), getattr(usage, 'output_tokens', None))
    return response


def call_llm(messages, system=None, max_tokens=None, tools=None, retries=None):
    kwargs = _build_kwargs(messages, system, max_tokens, tools)
    attempts = max(1, int(retries if retries is not None else config.get('llm.retries', 1)))
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            return _record(get_client().messages.create(**kwargs), started)
        except Exception as error:
            if attempt + 1 >= attempts or not _retryable(error):
                raise
            log.warning('LLM retry %d/%d: %s', attempt + 1, attempts, type(error).__name__)
            time.sleep(min(2 ** attempt, 8))


async def acall_llm(messages, system=None, max_tokens=None, tools=None, retries=None):
    kwargs = _build_kwargs(messages, system, max_tokens, tools)
    attempts = max(1, int(retries if retries is not None else config.get('llm.retries', 1)))
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            return _record(await get_async_client().messages.create(**kwargs), started)
        except Exception as error:
            if attempt + 1 >= attempts or not _retryable(error):
                raise
            log.warning('LLM retry %d/%d: %s', attempt + 1, attempts, type(error).__name__)
            await asyncio.sleep(min(2 ** attempt, 8))


def extract_text(response):
    return ''.join(b.text for b in (getattr(response, 'content', None) or [])
                   if getattr(b, 'type', '') == 'text' and getattr(b, 'text', None))

"""Direct-only HTTP with shared model concurrency limits; never retry via proxies."""
from __future__ import annotations

import urllib.parse
import urllib.request


def get_proxies() -> dict[str, str]:
    return {}


def proxy_hint() -> str:
    return '（本项目使用直连，不使用系统或环境代理）'


def reset_proxy_state() -> None:
    """Compatibility with old callers; there is no proxy fallback state."""


def _clone(request):
    return urllib.request.Request(request.full_url, data=request.data,
                                  headers=dict(request.header_items()), method=request.get_method())


def _direct(request, timeout):
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(_clone(request), timeout=timeout)


def urlopen(request: urllib.request.Request, timeout: float, *, prefer_direct: bool = False):
    # prefer_direct remains accepted for compatibility; every request is direct.
    from .scheduler import configured_pool, ScheduledResponse
    path = urllib.parse.urlparse(request.full_url).path.rstrip('/')
    model_request = request.get_method() == 'POST' and (
        path.endswith('/chat/completions') or path.endswith('/multimodal-generation/generation'))
    pool = configured_pool() if model_request else None
    if pool is None:
        return _direct(request, timeout)
    lease = pool.acquire()
    try:
        return ScheduledResponse(_direct(request, timeout), lease)
    except BaseException:
        lease.close()
        raise

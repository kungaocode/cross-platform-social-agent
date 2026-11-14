from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

DOMESTIC_ENDPOINTS = {
    'bocha_api': 'https://api.bocha.cn/v1/web-search',
    'baidu_api': 'https://qianfan.baidubce.com/v2/ai_search/web_search',
}
DEFAULT_BACKENDS = ("ddgs_duckduckgo", "baidu_api", "bocha_api")
BACKENDS = frozenset(DEFAULT_BACKENDS)


class SearchProviderError(RuntimeError):
    """Deliberately omit raw provider messages, which may echo credentials."""


class NoSearchRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SearchProviderError('search_api_redirect_rejected')


class ResearchModule:
    """四端共用：有序搜索后端、空结果回退和按后端短暂熔断。"""

    def __init__(self, timeout_seconds=6, searcher: Callable[..., Any] | None = None,
                 *, backends=DEFAULT_BACKENDS, cooldown_seconds=60, bocha_key_env="BOCHA_API_KEY",
                 baidu_key_env="BAIDU_SEARCH_API_KEY",
                 credentials_file="", domestic_direct=True, duckduckgo_mode='http',
                 duckduckgo_profile='', duckduckgo_channel='msedge', duckduckgo_timeout_seconds=15, clock=time.monotonic):
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.searcher = searcher
        self.backends = tuple(dict.fromkeys(backends))
        if not self.backends or any(name not in BACKENDS for name in self.backends):
            raise ValueError("未知或空的搜索后端列表")
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self.bocha_key_env, self.baidu_key_env = bocha_key_env, baidu_key_env
        self.domestic_direct = True  # Compatibility argument cannot enable a proxy.
        if duckduckgo_mode not in {'http', 'browser'}:
            raise ValueError('unsupported_duckduckgo_mode')
        self.duckduckgo_mode, self.duckduckgo_profile = duckduckgo_mode, duckduckgo_profile
        self.duckduckgo_channel = duckduckgo_channel
        self.duckduckgo_timeout_seconds = max(.05, min(15., float(duckduckgo_timeout_seconds)))
        self._duck_browser = None
        self._duck_failed_token = None
        self._file_keys = {}
        self.credentials_status = 'not_set'
        if credentials_file:
            try:
                data = json.loads(Path(credentials_file).read_text(encoding='utf-8-sig'))
                if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
                    raise ValueError('search_credentials_invalid')
                self._file_keys, self.credentials_status = data, 'loaded'
            except FileNotFoundError:
                self.credentials_status = 'missing'
            except (OSError, ValueError):
                self.credentials_status = 'invalid'
        self._clock = clock
        self._failed_until = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, runtime):
        # 兼容适配器测试中的 Mock 或精简 runtime 对象。
        def setting(name, default, types):
            value = getattr(runtime, name, default)
            return value if isinstance(value, types) else default
        return cls(timeout_seconds=setting("research_timeout_seconds", 6, (int, float)),
                   backends=setting("research_backends", DEFAULT_BACKENDS, (list, tuple)),
                   cooldown_seconds=setting("research_cooldown_seconds", 60, (int, float)),
                   bocha_key_env=setting("research_bocha_key_env", "BOCHA_API_KEY", str),
                   baidu_key_env=setting("research_baidu_key_env", "BAIDU_SEARCH_API_KEY", str),
                   credentials_file=setting("research_credentials_file", "", str),
                   domestic_direct=setting("research_domestic_direct", True, bool),
                   duckduckgo_mode=setting('research_duckduckgo_mode', 'http', str),
                   duckduckgo_profile=setting('research_duckduckgo_profile', '', str),
                   duckduckgo_channel=setting('research_duckduckgo_browser_channel', 'msedge', str),
                   duckduckgo_timeout_seconds=setting('research_duckduckgo_timeout_seconds', 15, (int, float)))

    def browser_client(self):
        from .duckduckgo_browser import DuckDuckGoBrowserSearch
        if not self.duckduckgo_profile:
            raise ValueError('duckduckgo_profile_required')
        if self._duck_browser is None:
            self._duck_browser = DuckDuckGoBrowserSearch(self.duckduckgo_profile, channel=self.duckduckgo_channel,
                                                       timeout_seconds=self.duckduckgo_timeout_seconds)
        return self._duck_browser

    def _key(self, env):
        return os.environ.get(env, '').strip() or self._file_keys.get(env, '').strip()

    def status(self):
        return dict(credentials_file=self.credentials_status, domestic_direct=self.domestic_direct,
                    duckduckgo=dict(mode=self.duckduckgo_mode, timeout_seconds=self.duckduckgo_timeout_seconds,
                                    browser_channel=self.duckduckgo_channel, profile=self.duckduckgo_profile),
                    backends=[dict(provider=name, configured=self._available(name)) for name in self.backends])

    def _post_json(self, url, payload, key):
        request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json',
                                   'Accept': 'application/json', 'User-Agent': 'SocialBotResearch/1.0'}, method='POST')
        handlers = [NoSearchRedirect(), ProxyHandler({})]
        # Search APIs use a single direct request by default, independent of system proxy state.
        with build_opener(*handlers).open(request, timeout=self.timeout_seconds) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise SearchProviderError('search_api_response_too_large')
        parsed = json.loads(data)
        if not isinstance(parsed, dict):
            raise SearchProviderError('search_api_invalid_response')
        if parsed.get('code') not in (None, 0, '0', 200, '200') or parsed.get('error_code'):
            raise SearchProviderError('search_api_rejected')
        return parsed

    def _available(self, name):
        if name == 'bocha_api':
            return bool(self._key(self.bocha_key_env))
        if name == 'baidu_api':
            return bool(self._key(self.baidu_key_env))
        return True

    def _search_backend(self, name, query, limit):
        if name == 'bocha_api':
            data = self._post_json(DOMESTIC_ENDPOINTS[name],
                dict(query=query, freshness='noLimit', summary=True, count=limit), self._key(self.bocha_key_env))
            body = data.get('data', data)
            if not isinstance(body, dict) or not isinstance(body.get('webPages'), dict):
                raise SearchProviderError('bocha_invalid_response')
            return body['webPages'].get('value', [])
        if name == 'baidu_api':
            data = self._post_json(DOMESTIC_ENDPOINTS[name],
                dict(messages=[dict(role='user', content=query)], search_source='baidu_search_v2',
                     resource_type_filter=[dict(type='web', top_k=limit)]), self._key(self.baidu_key_env))
            if not isinstance(data.get('references'), list):
                raise SearchProviderError('baidu_invalid_response')
            return data['references']
        if name != 'ddgs_duckduckgo':
            raise ValueError('unsupported_search_backend')
        if self.duckduckgo_mode == 'browser':
            return self.browser_client().search(query, limit)
        from .duckduckgo import DuckDuckGoSearch
        return DuckDuckGoSearch(timeout_seconds=min(self.timeout_seconds, self.duckduckgo_timeout_seconds)).search(query, limit)

    @staticmethod
    def _normalize(rows, limit):
        results, seen = [], set()
        for index, row in enumerate(rows or []):
            if index >= 100:
                break
            if not isinstance(row, dict):
                continue
            url = str(row.get("href") or row.get("url") or "").strip()
            if urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc or url in seen:
                continue
            title = str(row.get('title') or row.get('name') or '').strip()[:300]
            snippet = str(row.get('summary') or row.get('snippet') or row.get('body') or row.get('description') or row.get('content') or '').strip()[:1800]
            if not title and not snippet:
                continue
            seen.add(url)
            results.append(dict(title=title, url=url[:4096], snippet=snippet))
            if len(results) >= limit:
                break
        return results

    def search(self, query: str, max_results=5):
        query = str(query or "").strip()
        if not query:
            return {"success": False, "reason": "query_required", "results": []}
        if len(query) > 500:
            return {"success": False, "reason": "query_too_long", "results": []}
        limit = max(1, min(8, int(max_results)))
        attempts = []
        for name in (("custom",) if self.searcher else self.backends):
            if name != "custom" and not self._available(name):
                attempts.append({"provider": name, "status": "not_configured"})
                continue
            with self._lock:
                if name == 'ddgs_duckduckgo' and self.duckduckgo_mode == 'browser' and self._duck_failed_token is not None:
                    token = self.browser_client().recovery_token()
                    if token and token != self._duck_failed_token:
                        self._failed_until.pop(name, None)
                        self._duck_failed_token = None
                cooling = self._failed_until.get(name, 0) > self._clock()
            if cooling:
                attempts.append({"provider": name, "status": "cooldown"})
                continue
            try:
                rows = self.searcher(query, limit) if self.searcher else self._search_backend(name, query, limit)
                results = self._normalize(rows, limit)
            except Exception as exc:
                from .duckduckgo import DuckDuckGoError
                cooldown = exc.cooldown_seconds if isinstance(exc, DuckDuckGoError) else self.cooldown_seconds
                with self._lock:
                    self._failed_until[name] = self._clock() + max(self.cooldown_seconds, cooldown)
                    if name == 'ddgs_duckduckgo' and self.duckduckgo_mode == 'browser':
                        self._duck_failed_token = self.browser_client().recovery_token()
                # 不回传原始异常文字，避免密钥、完整请求地址泄漏给模型和聊天。
                attempts.append({"provider": name, "status": "error", "error_type": type(exc).__name__})
                from urllib.error import HTTPError
                if isinstance(exc, HTTPError):
                    attempts[-1]['http_status'] = exc.code
                if isinstance(exc, DuckDuckGoError):
                    attempts[-1]['reason'] = exc.reason
                    if exc.http_status is not None:
                        attempts[-1]['http_status'] = exc.http_status
                    attempts[-1]['retry_after_seconds'] = max(self.cooldown_seconds, cooldown)
                continue
            attempts.append({"provider": name, "status": "ok" if results else "empty"})
            if results:
                with self._lock:
                    self._failed_until.pop(name, None)
                return {"success": True, "query": query, "provider": name, "results": results, "attempts": attempts}
        return {"success": False, "reason": "all_search_backends_unavailable", "query": query,
                "results": [], "attempts": attempts}

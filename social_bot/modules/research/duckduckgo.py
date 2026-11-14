"""DuckDuckGo's public HTML/Lite GET pages, with explicit failure classification."""
from __future__ import annotations

import math
import os
import time
from urllib.parse import parse_qs, urljoin, urlsplit


class DuckDuckGoError(RuntimeError):
    def __init__(self, reason, *, http_status=None, cooldown_seconds=60):
        super().__init__(reason)
        self.reason, self.http_status, self.cooldown_seconds = reason, http_status, cooldown_seconds


class DuckDuckGoSearch:
    ENDPOINTS = ('https://html.duckduckgo.com/html/', 'https://lite.duckduckgo.com/lite/')

    def __init__(self, timeout_seconds=6, *, client=None, clock=time.monotonic):
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.client, self.clock = client, clock

    @staticmethod
    def _url(href, endpoint):
        if not href or not href.strip():
            return ''
        url = urljoin(endpoint, href)
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
        if host == 'duckduckgo.com' or host.endswith('.duckduckgo.com'):
            if parts.path.startswith('/y.js'):
                return ''
            if parts.path.startswith('/l/'):
                url = parse_qs(parts.query).get('uddg', [''])[0]
                parts = urlsplit(url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            return ''
        return url

    @classmethod
    def parse(cls, html, endpoint, limit):
        from lxml import html as lhtml
        from lxml.etree import ParserError
        try:
            tree = lhtml.fromstring(html)
        except (ValueError, TypeError, ParserError):
            raise DuckDuckGoError('duckduckgo_page_changed') from None
        anchors = tree.xpath("//a[contains(concat(' ',normalize-space(@class),' '),' result__a ') "
                             "or contains(concat(' ',normalize-space(@class),' '),' result-link ')]")
        rows, seen = [], set()
        for anchor in anchors[:100]:
            try:
                url = cls._url(anchor.get('href', ''), endpoint)
            except ValueError:
                continue
            title = ' '.join(anchor.text_content().split())
            if not url or not title or url in seen:
                continue
            containers = anchor.xpath("ancestor::*[contains(concat(' ',normalize-space(@class),' '),' result ')][1]")
            snippets = containers[0].xpath(".//*[contains(@class,'result__snippet')]") if containers else []
            if not snippets:
                snippets = anchor.xpath("ancestor::tr[1]/following-sibling::tr[position()<=2]"
                                        "//*[contains(@class,'result-snippet')]")
            snippet = ' '.join(snippets[0].text_content().split()) if snippets else ''
            rows.append(dict(title=title[:300], href=url, body=snippet[:1800]))
            seen.add(url)
            if len(rows) >= limit:
                return rows
        if rows:
            return rows
        if tree.xpath("//*[contains(@class,'no-results')]") or 'no results found for' in tree.text_content().lower():
            return []
        raise DuckDuckGoError('duckduckgo_page_changed')

    def search(self, query, limit):
        from ddgs.exceptions import TimeoutException
        from ddgs.http_client import HttpClient
        deadline, last = self.clock()+self.timeout_seconds, None
        for endpoint in self.ENDPOINTS:
            remaining = deadline-self.clock()
            if remaining <= 0:
                break
            client = self.client or HttpClient(proxy=None,
                                               timeout=max(1, math.ceil(remaining)), verify=True)
            try:
                # Normal GET search pages work where DDGS's POST can receive a challenge.
                response = client.request('GET', endpoint, params={'q': query, 'kl': 'us-en'})
            except Exception as exc:
                reason = 'duckduckgo_timeout' if isinstance(exc, (TimeoutError, TimeoutException)) else 'duckduckgo_network_error'
                last = DuckDuckGoError(reason)
                continue
            status, html = response.status_code, response.text
            if not isinstance(html, str) or len(html) > 2*1024*1024:
                raise DuckDuckGoError('duckduckgo_response_invalid', http_status=status)
            lower = html.lower()
            if any(marker in lower for marker in ('anomaly.js', 'challenge-form', 'bots use duckduckgo')):
                # Stop on challenges instead of retrying another entry point or solving a CAPTCHA.
                raise DuckDuckGoError('duckduckgo_captcha_required', http_status=status, cooldown_seconds=600)
            if status == 429:
                raise DuckDuckGoError('duckduckgo_rate_limited', http_status=status, cooldown_seconds=300)
            if status == 403:
                raise DuckDuckGoError('duckduckgo_access_denied', http_status=status, cooldown_seconds=300)
            if status != 200:
                last = DuckDuckGoError('duckduckgo_http_error', http_status=status)
                if status < 500:
                    raise last
                continue
            try:
                return self.parse(html, endpoint, max(1, min(8, int(limit))))
            except DuckDuckGoError as exc:
                last = exc
        raise last or DuckDuckGoError('duckduckgo_timeout')

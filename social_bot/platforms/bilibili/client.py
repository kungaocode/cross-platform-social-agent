"""Read-only web API client. WBI algorithm adapted from dsh-bilibili (MIT).

See bilibili/THIRD_PARTY_LICENSE.txt. These are website endpoints, not a
guaranteed public developer API. Credential-bearing redirects are refused.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import re
import time
from http.cookies import SimpleCookie
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, ProxyHandler


class BilibiliError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BilibiliError("REDIRECT_REFUSED")


MIXIN = (46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
         37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52)


def wbi_sign(params, img, sub, now):
    if len(img + sub) != 64:
        raise BilibiliError("WBI_KEYS_UNAVAILABLE")
    key = "".join((img + sub)[i] for i in MIXIN)[:32]
    clean = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in dict(params, wts=now).items()}
    query = urlencode(sorted(clean.items()), quote_via=quote)
    return dict(clean, w_rid=hashlib.md5((query + key).encode()).hexdigest())


def video_id(value):
    value = value.strip()
    if re.fullmatch(r"BV[A-Za-z0-9]{10}", value):
        return value
    url = urlsplit(value)
    if url.scheme == "https" and url.hostname in ("www.bilibili.com", "bilibili.com"):
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10})/?", url.path)
        if match:
            return match[1]
    raise ValueError("请提供 BV 号或 https://www.bilibili.com/video/BV… 链接；暂不支持短链接")


class BilibiliClient:
    def __init__(self, sessdata="", timeout=20, opener=None):
        if any(c in sessdata for c in "\r\n;"):
            raise ValueError("只填写 SESSDATA 的值，不要填写整段 Cookie")
        self.sessdata = sessdata
        self.timeout = timeout
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self._read_fallback = opener is None
        self._direct_opener = build_opener(ProxyHandler({}), NoRedirect())
        self._prefer_direct_until = 0
        self.cookies = {}

    def _open(self, request):
        # Credential-bearing redirects stay prohibited. Only read requests get a
        # bounded transport fallback; social writes are always attempted once.
        if request.get_method() != "GET" or not self._read_fallback:
            return self.opener.open(request, timeout=self.timeout)
        url, data, headers, method = request.full_url, request.data, dict(request.header_items()), request.get_method()
        def fresh():
            return Request(url, data=data, headers=headers, method=method)
        primary, secondary = ((self._direct_opener, self.opener) if time.monotonic() < self._prefer_direct_until
                              else (self.opener, self._direct_opener))
        try:
            return primary.open(fresh(), timeout=self.timeout)
        except HTTPError:
            raise
        except (URLError, TimeoutError, OSError):
            response = secondary.open(fresh(), timeout=self.timeout)
            self._prefer_direct_until = time.monotonic() + 60 if secondary is self._direct_opener else 0
            return response

    def _fetch(self, url, *, api=False, bootstrap=False):
        host = urlsplit(url).hostname or ""
        if urlsplit(url).scheme != "https":
            raise BilibiliError("UNSAFE_URL")
        if api and host != "api.bilibili.com":
            raise BilibiliError("UNSAFE_URL")
        if not api and not bootstrap and not (host.endswith(".hdslb.com") or host.endswith(".bilibili.com")):
            raise BilibiliError("UNTRUSTED_SUBTITLE_HOST")
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
                   "Referer": "https://www.bilibili.com/", "Accept": "application/json"}
        if api:
            parts = [f"{k}={v}" for k, v in self.cookies.items()]
            if self.sessdata:
                parts.append("SESSDATA=" + self.sessdata)
            if parts:
                headers["Cookie"] = "; ".join(parts)
        try:
            with self._open(Request(url, headers=headers)) as response:
                if bootstrap:
                    for line in response.headers.get_all("Set-Cookie", []):
                        cookie = SimpleCookie()
                        cookie.load(line)
                        for name in ("buvid3", "buvid4"):
                            if name in cookie:
                                self.cookies[name] = cookie[name].value
                    return {}
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise BilibiliError("RESPONSE_TOO_LARGE")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise BilibiliError("BAD_RESPONSE")
                return result
        except HTTPError as exc:
            raise BilibiliError(f"HTTP_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise BilibiliError("NETWORK_ERROR") from None
        except (ValueError, UnicodeError):
            raise BilibiliError("BAD_RESPONSE") from None

    def api(self, path, params=None, *, anonymous_nav=False):
        envelope = self._fetch("https://api.bilibili.com" + path + "?" + urlencode(params or {}, quote_via=quote), api=True)
        code = envelope.get("code")
        if code != 0 and not (anonymous_nav and code == -101):
            raise BilibiliError({-101: "LOGIN_REQUIRED", -412: "RISK_CONTROL", -403: "FORBIDDEN", -404: "NOT_FOUND"}.get(code, "API_ERROR"))
        if not isinstance(envelope.get("data"), dict):
            raise BilibiliError("BAD_RESPONSE")
        return envelope["data"]

    def account(self):
        data = self.api("/x/web-interface/nav", anonymous_nav=True)
        return {"logged_in": data.get("isLogin") is True, "uid": str(data.get("mid", "")), "name": data.get("uname", "")}

    def search(self, keyword):
        if not keyword.strip() or len(keyword) > 200:
            raise ValueError("搜索词须为 1～200 字符")
        if not self.cookies:
            self._fetch("https://www.bilibili.com/", bootstrap=True)
        data = self.api("/x/web-interface/search/type", {"search_type": "video", "keyword": keyword, "page": 1, "page_size": 10})
        return [{"bvid": row["bvid"], "title": html.unescape(re.sub(r"<[^>]*>", "", row.get("title", ""))),
                 "author": row.get("author", ""), "url": "https://www.bilibili.com/video/" + row["bvid"]}
                for row in (data.get("result") or []) if isinstance(row, dict) and row.get("bvid")][:10]

    def video(self, value, page=None):
        bvid = video_id(value)
        if page is None:
            page = int(parse_qs(urlsplit(value).query).get("p", ["1"])[0])
        data = self.api("/x/web-interface/view", {"bvid": bvid})
        pages = data.get("pages") or []
        if page < 1 or page > len(pages):
            raise ValueError("分 P 页码不存在")
        selected = pages[page - 1]
        return {"bvid": bvid, "aid": data.get("aid"), "cid": selected["cid"], "page": page, "pages": len(pages),
                "duration_seconds": selected.get("duration", data.get("duration")),
                "title": data.get("title", ""), "part": selected.get("part", ""),
                "author": data.get("owner", {}).get("name", ""), "description": data.get("desc", ""),
                "url": f"https://www.bilibili.com/video/{bvid}?p={page}"}

    def subtitles(self, detail):
        nav = self.api("/x/web-interface/nav", anonymous_nav=True)
        keys = nav.get("wbi_img", {})
        img, sub = (PurePosixPath(urlsplit(keys.get(name, "")).path).stem for name in ("img_url", "sub_url"))
        params = wbi_sign({"bvid": detail["bvid"], "cid": detail["cid"]}, img, sub, int(time.time()))
        data = self.api("/x/player/wbi/v2", params)
        tracks = data.get("subtitle", {}).get("subtitles") or []
        if not tracks:
            raise BilibiliError("SUBTITLES_UNAVAILABLE")
        track = next((t for t in tracks if t.get("lan") == "zh-CN"), tracks[0])
        url = track.get("subtitle_url", "")
        if url.startswith("//"):
            url = "https:" + url
        data = self._fetch(url)
        text = "\n".join(f"[{row.get('from', 0)}s] {row['content']}" for row in data.get("body", [])
                         if isinstance(row, dict) and isinstance(row.get("content"), str))
        if not text.strip():
            raise BilibiliError("SUBTITLES_UNAVAILABLE")
        segments, used = [], 0
        for row in data.get("body", []):
            if not isinstance(row, dict) or not isinstance(row.get("content"), str):
                continue
            try:
                start, end = float(row.get("from", 0)), float(row.get("to", 0))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end or used >= 12000 or len(segments) >= 500:
                continue
            content = row["content"][:12000-used]
            segments.append(dict(start=start, end=end, text=content))
            used += len(content)
        return {"language": track.get("lan"), "text": text[:12000], "truncated": len(text) > 12000,
                "segments": segments, "segments_truncated": len(segments) < len(data.get("body", []))}

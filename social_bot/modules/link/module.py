"""读取日常链接并把内容转换为可供模型使用的文本。

解析器只负责抓取和清理内容，不调用模型 API。不同平台共享同一个入口：

* 普通网页和小红书公开笔记：优先使用 Jina Reader，失败时回退到本地 HTML 清理；
* 微信公众号：优先使用可选的 ``wechat-article-parser``，再回退到 Jina；
* B 站和抖音：优先使用可选的 ``yt-dlp`` 获取字幕，失败时返回页面元数据。

视频没有字幕时仍然返回标题、作者和简介，并通过 ``partial`` 标记，避免模型把
页面简介误认为完整视频内容。
"""

from __future__ import annotations

from social_bot.cache import cache_task

import html
import ipaddress
import json
import re
import socket
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from urllib.request import Request

from ... import net


def urlopen(request: Request, timeout: float):
    """通过项目统一网络层访问链接，自动绕过失效的系统代理。"""
    return net.urlopen(request, timeout=timeout)


@dataclass(frozen=True)
class LinkOutcome:
    success: bool
    reason: str = ""
    url: str = ""
    source_type: str = "website"
    title: str = ""
    text: str = ""
    partial: bool = False


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.parts: list[str] = []
        self._title_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "title":
            self._title_depth += 1
        if tag in {"script", "style", "noscript", "svg", "template"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in {"script", "style", "noscript", "svg", "template"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        clean = data.strip()
        if not clean or self._skip_depth:
            return
        if self._title_depth:
            self.title += " " + clean
        else:
            self.parts.append(clean)


class LinkModule:
    _URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

    def __init__(
        self,
        *,
        timeout_seconds: int = 15,
        max_bytes: int = 5 * 1024 * 1024,
        max_chars: int = 12000,
        jina_reader_url: str = "https://r.jina.ai/",
    ) -> None:
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_bytes = max(1024, int(max_bytes))
        self.max_chars = max(100, int(max_chars))
        self.jina_reader_url = jina_reader_url.rstrip("/") + "/"

    @staticmethod
    def _source_type(host: str) -> str:
        host = host.lower().split(":", 1)[0].rstrip(".")
        if host == "mp.weixin.qq.com" or host.endswith(".weixin.qq.com"):
            return "wechat_article"
        if host == "b23.tv" or host.endswith(".bilibili.com") or host == "bilibili.com":
            return "bilibili"
        if (
            host in {"douyin.com", "iesdouyin.com"}
            or host.endswith(".douyin.com")
            or host.endswith(".iesdouyin.com")
        ):
            return "douyin"
        if host in {"xiaohongshu.com", "xhslink.com"} or host.endswith(".xiaohongshu.com") or host.endswith(".xhslink.com"):
            return "xiaohongshu"
        return "website"

    @classmethod
    def extract_url(cls, value: Any) -> str:
        """从文本或消息字段中提取第一个 HTTP/HTTPS 链接。"""
        if not isinstance(value, str):
            return ""
        match = cls._URL_RE.search(value)
        if not match:
            return ""
        return match.group(0).rstrip(".,;:!?)]}>")

    @classmethod
    def message_url(cls, message: Any) -> str:
        """尝试从 wxauto 链接/笔记消息的公开字段中直接取 URL。"""
        for name in ("url", "link", "href", "url_value"):
            try:
                value = getattr(message, name, None)
            except Exception:  # noqa: BLE001 - UIA 属性读取失败时继续尝试菜单
                value = None
            url = cls.extract_url(value)
            if url:
                return url
        for name in ("content", "text", "title", "description"):
            try:
                value = getattr(message, name, "")
            except Exception:  # noqa: BLE001
                value = ""
            url = cls.extract_url(value)
            if url:
                return url
        return ""

    @classmethod
    def _message_text_value(cls, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Mapping):
            parts = []
            for key in ("content", "text", "body", "markdown", "title", "description"):
                if key in value:
                    text = cls._message_text_value(value.get(key))
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        if isinstance(value, (list, tuple, set)):
            return "\n".join(
                text for item in value
                if (text := cls._message_text_value(item))
            ).strip()
        return ""

    @classmethod
    def _read_note_message(cls, message: Any) -> tuple[str, str] | None:
        """读取 wxauto NoteMessage 的正文，绕过小红书卡片不暴露 URL 的情况。"""
        try:
            card_title = cls._message_text_value(getattr(message, "content", ""))
        except Exception:
            card_title = ""
        get_content = getattr(message, "get_content", None)
        if callable(get_content):
            for kwargs in ({"wait": 5}, {"wait": 3}, {}):
                try:
                    text = cls._message_text_value(get_content(**kwargs))
                except TypeError:
                    continue
                except Exception:
                    text = ""
                if text and (not card_title or text.strip() != card_title.strip() or len(text) > len(card_title) + 20):
                    return card_title, text

        to_markdown = getattr(message, "to_markdown", None)
        if callable(to_markdown):
            with cache_task("link-card") as folder:
                for kwargs in ({"dir_path": folder, "wait": 5}, {"dir_path": folder}, {}):
                    try:
                        result = to_markdown(**kwargs)
                    except TypeError:
                        continue
                    except Exception:
                        result = None
                    candidate = Path(str(result)).expanduser() if result else None
                    if candidate and candidate.is_file():
                        try:
                            text = candidate.read_text(encoding="utf-8-sig", errors="replace").strip()
                        except OSError:
                            text = ""
                        if text and (not card_title or text.strip() != card_title.strip() or len(text) > len(card_title) + 20):
                            return card_title, text
        return None

    def read_message(self, message: Any, resolve_url: Any = None) -> LinkOutcome:
        """读取文本链接或微信文章/小红书卡片消息。

        ``resolve_url`` 由桌面适配层提供，用于点击卡片并从右键菜单复制真实 URL。
        解析器本身不直接操作 UI。
        """
        url = self.message_url(message)
        message_type = str(getattr(message, "type", "") or "").strip().lower()
        if not url and message_type in {"note", "link"}:
            note_content = self._read_note_message(message)
            if note_content:
                title, text = note_content
                return LinkOutcome(
                    True,
                    reason="message_content",
                    source_type="xiaohongshu",
                    title=title,
                    text=text[: self.max_chars],
                )
        if not url and callable(resolve_url):
            try:
                url = self.extract_url(resolve_url(message))
            except Exception as exc:  # noqa: BLE001 - UI 解析失败交给工具层说明
                return LinkOutcome(False, f"link_card_error:{type(exc).__name__}:{exc}", source_type="website")
        if not url:
            return LinkOutcome(False, "link_url_not_available", source_type="website")
        return self.read(url)

    @staticmethod
    def _safe_url(url: str) -> str:
        value = url.strip()
        parsed = urlparse(value)
        if len(value) > 4000:
            raise ValueError("url_too_long")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("only_http_https_urls_are_supported")
        if parsed.username or parsed.password:
            raise ValueError("url_credentials_are_not_allowed")
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"}:
            raise ValueError("local_urls_are_not_allowed")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("private_urls_are_not_allowed")
        if address is None:
            try:
                for resolved in socket.getaddrinfo(host, None):
                    ip = ipaddress.ip_address(resolved[4][0])
                    if ip.is_private or ip.is_loopback or ip.is_link_local:
                        raise ValueError("private_urls_are_not_allowed")
            except socket.gaierror:
                pass
        return value

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        text = html.unescape(str(value)).replace("\x00", " ")
        return re.sub(r"[ \t\r\f\v]+", " ", text).strip()

    def _fetch(self, url: str, *, accept: str = "*/*") -> tuple[bytes, str, str]:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 social-bot link reader",
                "Accept": accept,
                "Accept-Encoding": "identity",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read(self.max_bytes + 1)
            charset = response.headers.get_content_charset() or "utf-8"
            content_type = response.headers.get_content_type()
        if len(body) > self.max_bytes:
            body = body[: self.max_bytes]
        return body, charset, content_type

    def _fetch_text(self, url: str, *, accept: str = "*/*") -> str:
        body, charset, _ = self._fetch(url, accept=accept)
        return body.decode(charset, errors="replace")

    @staticmethod
    def _parse_html(decoded: str) -> tuple[str, str]:
        parser = _PageParser()
        parser.feed(decoded)
        text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
        title = re.sub(r"\s+", " ", parser.title).strip()
        if not title:
            match = re.search(
                r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]+content=["\']([^"\']+)',
                decoded,
                flags=re.IGNORECASE,
            )
            if not match:
                match = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\']',
                    decoded,
                    flags=re.IGNORECASE,
                )
            title = LinkModule._clean_text(match.group(1)) if match else ""
        return title, text

    def _read_direct_page(self, url: str) -> tuple[str, str]:
        body, charset, content_type = self._fetch(url, accept="text/html, text/plain;q=0.9, */*;q=0.8")
        decoded = body.decode(charset, errors="replace")
        if content_type == "text/html" or "<html" in decoded[:2000].lower():
            return self._parse_html(decoded)
        return "", self._clean_text(decoded)

    def _read_jina(self, url: str) -> tuple[str, str]:
        body, charset, content_type = self._fetch(self.jina_reader_url + url, accept="text/plain, text/markdown;q=0.9, */*;q=0.8")
        text = body.decode(charset, errors="replace").strip()
        if content_type == "text/html" or "<html" in text[:2000].lower():
            return self._parse_html(text)
        title = ""
        for line in text.splitlines():
            candidate = re.sub(r"^#+\s*", "", line).strip()
            if candidate:
                title = candidate[:300]
                break
        return self._clean_text(title), text

    @staticmethod
    def _parser_value(result: Any, *names: str) -> str:
        for name in names:
            value = result.get(name) if isinstance(result, Mapping) else getattr(result, name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _read_wechat_parser(self, url: str) -> tuple[str, str] | None:
        try:
            from wechat_article_parser import parse  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            result = parse(url)
        except Exception:
            return None
        text = self._parser_value(result, "article_markdown", "markdown", "content", "text")
        title = self._parser_value(result, "title", "article_title")
        return (title, text) if text else None

    @staticmethod
    def _subtitle_candidates(info: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        tracks: list[Mapping[str, Any]] = []
        preferred = ("zh-Hans", "zh-CN", "zh-cn", "zh", "chi", "Chinese", "en")
        for field in ("subtitles", "automatic_captions"):
            values = info.get(field)
            if not isinstance(values, Mapping):
                continue
            keys = list(values)
            keys.sort(key=lambda key: (preferred.index(key) if key in preferred else len(preferred), str(key)))
            for key in keys:
                entries = values.get(key)
                if isinstance(entries, list):
                    tracks.extend(item for item in entries if isinstance(item, Mapping) and item.get("url"))
        tracks.sort(key=lambda item: (0 if str(item.get("ext", "")).lower() in {"json3", "json", "vtt", "srt"} else 1))
        return tracks

    @staticmethod
    def _subtitle_text(raw: str) -> str:
        value = raw.strip()
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, list):
            chunks = []
            for item in data:
                if isinstance(item, Mapping):
                    content = item.get("content") or item.get("text") or item.get("body")
                    if content:
                        chunks.append(str(content).strip())
            return " ".join(chunks).strip()
        if isinstance(data, Mapping):
            for key in ("body", "content", "events"):
                nested = data.get(key)
                if isinstance(nested, list):
                    return LinkModule._subtitle_text(json.dumps(nested, ensure_ascii=False))
        lines = []
        for line in value.splitlines():
            line = line.strip().lstrip("\ufeff")
            if not line or line.upper() in {"WEBVTT", "NOTE"}:
                continue
            if re.fullmatch(r"\d+", line) or re.search(r"\d{2}:\d{2}(?::\d{2})?[,.]\d{3}\s+-->\s+", line):
                continue
            lines.append(re.sub(r"<[^>]+>", "", line))
        return " ".join(lines).strip()

    def _read_video_with_ytdlp(self, url: str) -> tuple[str, str, str] | None:
        try:
            import yt_dlp  # type: ignore[import-not-found]
        except ImportError:
            return None
        options = {
            "proxy": "",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "extract_flat": False,
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
        except Exception:
            return None
        if not isinstance(info, Mapping):
            return None
        title = self._clean_text(info.get("title"))
        description = self._clean_text(info.get("description"))
        author = self._clean_text(info.get("uploader") or info.get("channel") or info.get("creator"))
        metadata = "\n".join(
            part for part in (
                f"标题：{title}" if title else "",
                f"作者：{author}" if author else "",
                f"简介：{description}" if description else "",
            ) if part
        )
        for track in self._subtitle_candidates(info):
            track_url = track.get("url")
            if not isinstance(track_url, str) or not track_url.startswith(("http://", "https://")):
                continue
            try:
                transcript = self._subtitle_text(self._fetch_text(track_url, accept="text/vtt, application/json, text/plain"))
            except Exception:
                continue
            if transcript:
                return title, (metadata + "\n字幕：" + transcript).strip(), ""
        return title, metadata, "video_transcript_unavailable"

    def _read_video(self, url: str, source_type: str) -> LinkOutcome:
        extracted = self._read_video_with_ytdlp(url)
        if extracted is not None:
            title, text, reason = extracted
            if text:
                return LinkOutcome(True, reason, url, source_type, title, text[: self.max_chars], partial=bool(reason))
        try:
            title, text = self._read_direct_page(url)
        except Exception as exc:
            return LinkOutcome(False, f"link_error:{type(exc).__name__}:{exc}", url=url, source_type=source_type)
        if not text:
            return LinkOutcome(False, "video_content_empty", url, source_type, title, partial=True)
        return LinkOutcome(True, "video_transcript_unavailable", url, source_type, title, text[: self.max_chars], partial=True)

    def read(self, url: str) -> LinkOutcome:
        try:
            safe_url = self._safe_url(url)
            parsed = urlparse(safe_url)
            source_type = self._source_type(parsed.hostname or "")
            if source_type in {"bilibili", "douyin"}:
                return self._read_video(safe_url, source_type)

            title = ""
            text = ""
            if source_type == "wechat_article":
                parsed_result = self._read_wechat_parser(safe_url)
                if parsed_result:
                    title, text = parsed_result
            if not text:
                try:
                    title, text = self._read_jina(safe_url)
                except Exception:
                    title, text = self._read_direct_page(safe_url)
            if not text:
                return LinkOutcome(False, "link_content_empty", safe_url, source_type, title)
            return LinkOutcome(True, url=safe_url, source_type=source_type, title=title, text=text[: self.max_chars])
        except Exception as exc:  # noqa: BLE001 - 网络、解析和页面编码错误交回工具层
            return LinkOutcome(False, f"link_error:{type(exc).__name__}:{exc}", url=url)

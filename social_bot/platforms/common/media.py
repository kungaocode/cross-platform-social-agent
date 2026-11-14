"""B站/抖音实际消息媒体读取；图片与原始媒体地址不进入聊天模型或记忆。"""
from __future__ import annotations

import base64
import ipaddress
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.request import ProxyHandler
from urllib.error import HTTPError,URLError

from ...cache import cache_task
from ...modules.vision import QwenVisionAnalyzer
from ...modules.video import OmniVideoAnalyzer
from ...online_video import OnlineVideoAnalyzer, platform_url


class SocialMediaError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SocialMediaError("media_redirect_refused")


class SocialMediaReader:
    IMAGE_LIMIT = 12 * 1024 * 1024
    DOMAINS = {
        "bilibili": ("hdslb.com", "biliimg.com", "bilivideo.com"),
        "douyin": ("douyinpic.com", "byteimg.com", "douyincdn.com", "douyinvod.com", "ibytedtos.com", "bytecdn.cn", "douyin.com"),
    }

    def __init__(self, settings, platform, *, image_enabled=True, video_enabled=True, cookies=lambda: (),
                 vision=None, video=None, online=None):
        if platform not in self.DOMAINS:
            raise ValueError("unsupported_social_platform")
        self.platform, self.image_enabled, self.video_enabled = platform, image_enabled, video_enabled
        self.cookies = cookies
        self.vision = vision if vision is not None else QwenVisionAnalyzer.from_settings(settings.multimodal)
        if isinstance(self.vision,QwenVisionAnalyzer) and (urlsplit(self.vision.base_url).hostname or '').endswith('.aliyuncs.com'):
            self.vision.prefer_direct=True
        self.video = video if video is not None else OmniVideoAnalyzer.from_settings(settings)
        self.online = online or OnlineVideoAnalyzer(self.video)

    def safe_url(self, url):
        if not isinstance(url, str) or len(url) > 8192:
            raise SocialMediaError("media_url_invalid")
        if url.startswith("//"):
            url = "https:" + url
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not any(host == domain or host.endswith("." + domain) for domain in self.DOMAINS[self.platform])):
            raise SocialMediaError("media_host_not_allowed")
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise SocialMediaError("media_address_not_public")
        return url

    @staticmethod
    def image_suffix(data):
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        if data.startswith(b"BM"):
            return ".bmp"
        raise SocialMediaError("media_image_format_unsupported")

    def _download(self, url, root, *, video=False):
        url = self.safe_url(url)
        limit = OmniVideoAnalyzer.MAX_SOURCE_BYTES if video else self.IMAGE_LIMIT
        path = Path(root) / ("source.mp4" if video else "source.image")
        start, total = time.monotonic(), 0
        request=Request(url,headers={"User-Agent":"Mozilla/5.0"})
        try:
            response=build_opener(ProxyHandler({}),NoRedirect()).open(request,timeout=20)
        except HTTPError:
            raise
        except (URLError,OSError):
            response=build_opener(NoRedirect()).open(Request(url,headers={"User-Agent":"Mozilla/5.0"}),timeout=20)
        with response:
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise SocialMediaError("media_download_too_large")
            with path.open("wb") as output:
                while True:
                    chunk = response.read(min(256 * 1024, limit + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit or time.monotonic() - start > 120:
                        raise SocialMediaError("media_download_limit")
                    output.write(chunk)
        if total == 0:
            raise SocialMediaError("media_empty")
        return path

    def image(self, url, question, root, resolve_inline=None):
        if not self.image_enabled or self.vision is None:
            raise SocialMediaError("image_model_unavailable_or_disabled")
        if url.startswith(("blob:", "dom-image:")):
            if resolve_inline is None:
                raise SocialMediaError("image_blob_unavailable")
            url = resolve_inline(url)
        if url.startswith("data:image/"):
            if len(url) > self.IMAGE_LIMIT * 4 // 3 + 200 or ";base64," not in url:
                raise SocialMediaError("image_inline_limit")
            data = base64.b64decode(url.split(",", 1)[1], validate=True)
        else:
            path = self._download(url, root)
            data = path.read_bytes()
        if not data or len(data) > self.IMAGE_LIMIT:
            raise SocialMediaError("image_size_limit")
        path = Path(root) / ("image" + self.image_suffix(data))
        path.write_bytes(data)
        answer = self.vision(str(path), question)
        if not isinstance(answer, str) or not answer.strip():
            raise SocialMediaError("image_empty_response")
        return {"success": True, "kind": "image", "text": answer[:7000]}

    def read(self, media, question="", *, resolve_inline=None):
        if not isinstance(media, (list, tuple)) or not 1 <= len(media) <= 2:
            return {"success": False, "reason": "media_requires_1_to_2_items"}
        results = []
        for item in media:
            try:
                kind, url = item["kind"], item["url"]
                with cache_task(self.platform + "-chat") as root:
                    if kind == "image":
                        result = self.image(url, question or "描述可见图片内容并提取文字，无法确认时说明。", root, resolve_inline)
                    elif kind in ("video", "video_file"):
                        if not self.video_enabled or self.video is None:
                            raise SocialMediaError("video_model_unavailable_or_disabled")
                        if kind == "video":
                            platform, clean_url = platform_url(url)
                            if platform != self.platform:
                                raise SocialMediaError("video_platform_mismatch")
                            result = self.online.analyze(clean_url, question, seconds=180, cookies=self.cookies())
                        else:
                            path = self._download(url, root, video=True)
                            result = {"success": True, **self.video.analyze_path(path, question)}
                        if result.get("success") is False or not isinstance(result.get("text"), str) or not result["text"].strip():
                            raise SocialMediaError("video_empty_or_failed_response")
                        result = {key: result[key] for key in ("success", "text", "note", "partial", "coverage_start_seconds", "coverage_end_seconds", "coverage_seconds") if key in result}
                        result["kind"] = "video"
                    else:
                        raise SocialMediaError("media_kind_unsupported")
                results.append(result)
            except Exception as exc:
                from ...video_evidence import safe_code
                reason=str(exc) if isinstance(exc,SocialMediaError) else safe_code(exc)
                results.append({"success": False, "reason": reason, "kind": item.get("kind", "unknown") if isinstance(item, dict) else "unknown"})
        return {"success": all(row.get("success") is True for row in results), "results": results,
                "note": "只依据成功识别的内容回答；失败或部分范围不得补造画面、声音。"}

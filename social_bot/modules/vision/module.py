"""视觉问答适配层。

模块负责把 wxauto 图片消息下载为本地文件，再交给 OpenAI 兼容的视觉模型。
默认适配 Token Plan 中的 Qwen 视觉模型；保留 ``analyzer`` 注入口，方便测试
或以后替换成其他视觉服务。
"""

from __future__ import annotations

from social_bot.cache import CacheOwner, retained_media

import base64
import json
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ... import net


@dataclass(frozen=True)
class VisionOutcome:
    success: bool
    reason: str = ""
    answer: str = ""
    image_path: str = ""
    request: Mapping[str, Any] | None = None


class QwenVisionAnalyzer:
    """用 OpenAI 兼容的 Chat Completions 接口发送图片。

    Qwen 视觉接口接收 ``image_url`` 数据 URL，因此不需要把微信图片再次上传到
    公网。图片只在本地编码后随 HTTPS 请求发送给配置的视觉模型。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        model: str = "qwen3.8-flash",
        timeout_seconds: int = 45,
        max_tokens: int = 600,
        max_image_bytes: int = 12 * 1024 * 1024,
        prefer_direct: bool = False,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = self._normalize_url(base_url)
        self.model = str(model or "qwen3.8-flash").strip()
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_tokens = max(1, int(max_tokens))
        self.max_image_bytes = max(1024, int(max_image_bytes))
        self.prefer_direct = prefer_direct

    @staticmethod
    def _normalize_url(value: str) -> str:
        url = str(value or "").strip().rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if url.endswith("/v1"):
            return url + "/chat/completions"
        return url + "/chat/completions" if url else ""

    @classmethod
    def from_settings(cls, settings: Any) -> "QwenVisionAnalyzer | None":
        """从 ``MultimodalSettings`` 构造 analyzer；未配置 key 时返回 None。"""
        if settings is None:
            return None
        raw_api_key = getattr(settings, "api_key", "")
        raw_base_url = getattr(settings, "base_url", "")
        # 测试替身或未加载配置时不要把 Mock/任意对象字符串误认为真实 key。
        api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else ""
        base_url = raw_base_url.strip() if isinstance(raw_base_url, str) else ""
        if not api_key or not base_url:
            return None
        raw_model = getattr(settings, "model", "qwen3.8-flash")
        raw_timeout = getattr(settings, "timeout_seconds", 45)
        raw_max_tokens = getattr(settings, "max_tokens", 600)
        try:
            timeout_seconds = int(raw_timeout)
            max_tokens = int(raw_max_tokens)
        except (TypeError, ValueError):
            timeout_seconds, max_tokens = 45, 600
        return cls(
            api_key,
            base_url,
            model=raw_model if isinstance(raw_model, str) and raw_model.strip() else "qwen3.8-flash",
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, Mapping):
                    value = item.get("text") or item.get("content")
                    if value:
                        parts.append(str(value))
            return "".join(parts).strip()
        if isinstance(content, Mapping):
            return str(content.get("text") or content.get("content") or "").strip()
        return ""

    def __call__(self, image_path: str, question: str) -> str:
        if not self.api_key:
            raise RuntimeError("vision_api_key_not_configured")
        if not self.base_url:
            raise RuntimeError("vision_api_url_not_configured")
        path = Path(image_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise RuntimeError("image_file_not_found")
        size = path.stat().st_size
        if size > self.max_image_bytes:
            raise RuntimeError("image_too_large")
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        prompt = str(question or "").strip() or "请描述这张图片，并提取其中对用户有帮助的信息。"
        payload = {
            "model": self.model,
            "stream": False,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                    ],
                }
            ],
        }
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            options = {"prefer_direct": True} if self.prefer_direct else {}
            with net.urlopen(request, timeout=self.timeout_seconds, **options) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"vision_http_{exc.code}:{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"vision_network_error:{exc}{net.proxy_hint()}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("vision_invalid_json") from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("vision_response_format_invalid") from exc
        answer = self._content_text(content)
        if not answer:
            raise RuntimeError("vision_empty_response")
        return answer


class QwenTextAnalyzer:
    """用同一 Qwen Chat Completions 接口理解解析后的文件文本。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        model: str = "qwen3.8-flash",
        timeout_seconds: int = 45,
        max_tokens: int = 600,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = QwenVisionAnalyzer._normalize_url(base_url)
        self.model = str(model or "qwen3.8-flash").strip()
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_tokens = max(1, int(max_tokens))

    @classmethod
    def from_settings(cls, settings: Any) -> "QwenTextAnalyzer | None":
        if settings is None:
            return None
        api_key = getattr(settings, "api_key", "")
        base_url = getattr(settings, "base_url", "")
        if not isinstance(api_key, str) or not api_key.strip() or not isinstance(base_url, str) or not base_url.strip():
            return None
        try:
            timeout = int(getattr(settings, "timeout_seconds", 45))
            max_tokens = int(getattr(settings, "max_tokens", 600))
        except (TypeError, ValueError):
            timeout, max_tokens = 45, 600
        model = getattr(settings, "model", "qwen3.8-flash")
        return cls(
            api_key,
            base_url,
            model=model if isinstance(model, str) and model.strip() else "qwen3.8-flash",
            timeout_seconds=timeout,
            max_tokens=max_tokens,
        )

    def __call__(self, text: str, question: str = "") -> str:
        if not self.api_key:
            raise RuntimeError("qwen_api_key_not_configured")
        if not self.base_url:
            raise RuntimeError("qwen_api_url_not_configured")
        source = str(text or "").strip()
        if not source:
            raise RuntimeError("document_text_empty")
        prompt = str(question or "").strip() or "请概括以下文件内容，并提取对用户有帮助的关键信息。"
        payload = {
            "model": self.model,
            "stream": False,
            "max_tokens": self.max_tokens,
            "messages": [
                {
                    "role": "user",
                    "content": f"{prompt}\n\n以下是文件解析内容：\n{source}",
                }
            ],
        }
        return self._complete(payload)

    def analyze_file(self, file_path: str, question: str = "") -> str:
        """把 PDF 原文件交给 Qwen，保留页面图片和版面信息。"""
        path = Path(file_path).expanduser().resolve(strict=True)
        if path.suffix.lower() != ".pdf":
            raise RuntimeError("qwen_file_only_supports_pdf")
        if path.stat().st_size > 150 * 1024 * 1024:
            raise RuntimeError("pdf_too_large_for_qwen")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        prompt = str(question or "").strip() or "请理解这份 PDF 的文字、图片和图表，并概括其中对用户有帮助的关键信息。"
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "file",
                            "file": {
                                "file_data": f"data:application/pdf;base64,{encoded}",
                                "filename": path.name,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        # PDF 页面解析可能明显慢于普通文本请求，Qwen 文档说明首 token
        # 等待时间可达 300 秒；这里单独放宽 PDF 请求超时。
        return self._complete(payload, timeout_seconds=max(self.timeout_seconds, 300))

    def _complete(self, payload: dict[str, Any], timeout_seconds: int | None = None) -> str:
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with net.urlopen(request, timeout=timeout_seconds or self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"qwen_http_{exc.code}:{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"qwen_network_error:{exc}{net.proxy_hint()}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("qwen_invalid_json") from exc
        try:
            answer = QwenVisionAnalyzer._content_text(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("qwen_response_format_invalid") from exc
        if not answer:
            raise RuntimeError("qwen_empty_response")
        return answer


class VisionModule(CacheOwner):
    """准备图片并交给可选的多模态 analyzer。"""

    def __init__(
        self,
        analyzer: Callable[[str, str], str | Mapping[str, Any]] | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.analyzer = analyzer
        from ...cache import configured_cache_root
        self.cache_dir = Path(cache_dir) if cache_dir is not None else configured_cache_root()

    @classmethod
    def from_settings(cls, settings: Any, cache_dir: str | Path | None = None) -> "VisionModule":
        return cls(analyzer=QwenVisionAnalyzer.from_settings(settings), cache_dir=cache_dir)

    def _download(self, message: Any) -> Path:
        download = getattr(message, "download", None)
        if not callable(download):
            raise ValueError("image_download_not_supported")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        before = {path for path in self.cache_dir.iterdir() if path.is_file()}
        try:
            result = download(dir_path=str(self.cache_dir), original=True)
        except TypeError:
            result = download()
        if result:
            path = Path(str(result)).expanduser().resolve()
            if path.is_file():
                return path
        # 不同 wxauto 版本有的返回文件路径，有的只返回成功标志；第二种情况
        # 从本模块的缓存目录找本次下载新增或刚更新的图片，避免把 True 当成路径。
        image_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}
        candidates = [
            path
            for path in self.cache_dir.iterdir()
            if path.is_file() and path.suffix.lower() in image_suffixes and path not in before
        ]
        if candidates:
            return max(candidates, key=lambda item: item.stat().st_mtime)
        if not result:
            raise ValueError("image_download_failed")
        raise ValueError("downloaded_image_not_found")

    @staticmethod
    def build_request(image_path: str, question: str) -> dict[str, Any]:
        """返回厂商无关的请求描述，供后续多模态客户端转换。"""
        return {
            "image_path": image_path,
            "question": question,
            "content": [
                {"type": "text", "text": question or "请描述这张图片并提取有用信息。"},
                {"type": "image", "path": image_path},
            ],
        }

    @retained_media("visionmodule")
    def ask(self, message: Any, question: str = "") -> VisionOutcome:
        try:
            image_path = self._download(message)
        except (OSError, ValueError) as exc:
            return VisionOutcome(False, str(exc))
        except Exception as exc:  # noqa: BLE001 - wxauto UI 异常统一交回工具层
            return VisionOutcome(False, f"image_download_error:{type(exc).__name__}:{exc}")
        request = self.build_request(str(image_path), question.strip())
        try:
            if self.analyzer is None:
                return VisionOutcome(
                    False,
                    "vision_model_not_configured",
                    image_path=str(image_path),
                    request=request,
                )
            try:
                result = self.analyzer(str(image_path), question.strip())
            except Exception as exc:  # noqa: BLE001 - 适配器错误交回工具层
                return VisionOutcome(
                    False,
                    f"vision_error:{type(exc).__name__}:{exc}",
                    image_path=str(image_path),
                    request=request,
                )
            if isinstance(result, Mapping):
                answer = str(result.get("answer") or result.get("text") or "").strip()
            else:
                answer = str(result or "").strip()
            return VisionOutcome(
                bool(answer),
                "" if answer else "vision_empty",
                answer=answer,
                image_path=str(image_path),
                request=request,
            )
        finally:
            try:
                pass  # Retained until manual cache cleanup.
            except OSError:
                pass

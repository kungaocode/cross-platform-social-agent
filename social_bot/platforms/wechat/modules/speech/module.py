"""微信语音转写和语音模型适配。"""

from __future__ import annotations

from social_bot.cache import CacheOwner, retained_media

import base64
import json
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from social_bot import net


class QwenAudioTranscriber:
    """调用 api_audio.txt 对应的 DashScope Qwen ASR 接口。"""

    AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".amr", ".flac", ".opus"}

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        model: str = "qwen-audio-3.1-asr-flash",
        timeout_seconds: int = 45,
        sample_rate: int = 16000,
        max_bytes: int = 20 * 1024 * 1024,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").strip()
        self.model = str(model or "qwen-audio-3.1-asr-flash").strip()
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.sample_rate = max(8000, int(sample_rate))
        self.max_bytes = max(1024, int(max_bytes))

    @classmethod
    def from_settings(cls, settings: Any) -> "QwenAudioTranscriber | None":
        if settings is None:
            return None
        raw_key = getattr(settings, "api_key", "")
        raw_url = getattr(settings, "base_url", "")
        if not isinstance(raw_key, str) or not raw_key.strip() or not isinstance(raw_url, str) or not raw_url.strip():
            return None
        model = getattr(settings, "model", "qwen-audio-3.1-asr-flash")
        try:
            timeout = int(getattr(settings, "timeout_seconds", 45))
            sample_rate = int(getattr(settings, "sample_rate", 16000))
        except (TypeError, ValueError):
            timeout, sample_rate = 45, 16000
        return cls(
            raw_key,
            raw_url,
            model=model if isinstance(model, str) and model.strip() else "qwen-audio-3.1-asr-flash",
            timeout_seconds=timeout,
            sample_rate=sample_rate,
        )

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace").strip()
        if isinstance(value, Mapping):
            for key in ("text", "content", "transcript", "result", "message", "choices", "output", "data"):
                answer = QwenAudioTranscriber._text(value.get(key))
                if answer:
                    return answer
            return ""
        if isinstance(value, (list, tuple)):
            return "".join(QwenAudioTranscriber._text(item) for item in value).strip()
        return ""

    @classmethod
    def _response_text(cls, data: Any) -> str:
        if not isinstance(data, Mapping):
            return ""
        output = data.get("output")
        for candidate in (
            data.get("text"),
            data.get("transcript"),
            output.get("text") if isinstance(output, Mapping) else None,
            output.get("transcript") if isinstance(output, Mapping) else None,
            output.get("choices") if isinstance(output, Mapping) else None,
            data.get("choices"),
        ):
            answer = cls._text(candidate)
            if answer:
                return answer
        return ""

    def __call__(self, audio_path: str) -> str:
        if not self.api_key:
            raise RuntimeError("audio_api_key_not_configured")
        path = Path(audio_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise RuntimeError("audio_file_not_found")
        if path.stat().st_size > self.max_bytes:
            raise RuntimeError("audio_too_large")
        suffix = path.suffix.lower()
        audio_format = suffix.lstrip(".") if suffix in self.AUDIO_SUFFIXES else "wav"
        mime = mimetypes.guess_type(path.name)[0] or "audio/wav"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": f"data:{mime};base64,{encoded}"},
                            }
                        ],
                    }
                ]
            },
            "parameters": {"format": audio_format, "sample_rate": self.sample_rate},
        }
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-DashScope-SSE": "disable",
            },
            method="POST",
        )
        try:
            with net.urlopen(request, timeout=self.timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"audio_http_{exc.code}:{detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"audio_network_error:{exc}{net.proxy_hint()}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError("audio_invalid_json") from exc
        answer = self._response_text(data)
        if not answer:
            raise RuntimeError("audio_empty_response")
        return answer


class SpeechModule(CacheOwner):
    """优先使用 Qwen ASR，未配置时兼容 wxauto 的 ``to_text()``。"""

    AUDIO_SUFFIXES = QwenAudioTranscriber.AUDIO_SUFFIXES

    def __init__(
        self,
        analyzer: Callable[[str], str | Mapping[str, Any]] | None = None,
        cache_dir: str | Path | None = None,
    ) -> None:
        self.analyzer = analyzer
        from social_bot.cache import configured_cache_root
        self.cache_dir = Path(cache_dir) if cache_dir is not None else configured_cache_root()

    @classmethod
    def from_settings(cls, settings: Any, cache_dir: str | Path | None = None) -> "SpeechModule":
        return cls(analyzer=QwenAudioTranscriber.from_settings(settings), cache_dir=cache_dir)

    @staticmethod
    def _path_from_result(result: Any) -> Path | None:
        if isinstance(result, (str, Path)) and str(result).strip():
            path = Path(str(result)).expanduser().resolve()
            return path if path.is_file() else None
        if isinstance(result, Mapping):
            for key in ("path", "file_path", "audio_path", "data"):
                path = SpeechModule._path_from_result(result.get(key))
                if path:
                    return path
        return None

    def _download(self, message: Any) -> tuple[Path, bool]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        for name in ("audio_path", "file_path", "path"):
            value = getattr(message, name, None)
            path = self._path_from_result(value)
            if path:
                # 这些路径来自当前微信语音消息，处理后可安全作为一次性缓存清理。
                return path, True
        info_path = self._path_from_result(getattr(message, "info", None))
        if info_path:
            return info_path, True
        download = getattr(message, "download", None)
        if not callable(download):
            raise ValueError("audio_download_not_supported")
        before = {path for path in self.cache_dir.iterdir() if path.is_file()}
        try:
            try:
                result = download(dir_path=str(self.cache_dir), timeout=30)
            except TypeError:
                try:
                    result = download(dir_path=str(self.cache_dir), force_click=False, timeout=30)
                except TypeError:
                    result = download()
        except TypeError:
            result = download()
        path = self._path_from_result(result)
        if path:
            return path, True
        candidates = [
            path
            for path in self.cache_dir.iterdir()
            if path.is_file() and path.suffix.lower() in self.AUDIO_SUFFIXES and path not in before
        ]
        if candidates:
            return max(candidates, key=lambda item: item.stat().st_mtime), True
        if not result:
            raise ValueError("audio_download_failed")
        raise ValueError("downloaded_audio_not_found")

    @staticmethod
    def _cleanup(path: Path | None, owned: bool) -> None:
        if path is None or not owned:
            return
        try:
            pass  # Retained until manual cache cleanup.
        except OSError:
            pass

    @retained_media("speechmodule")
    def transcribe(self, message: Any) -> dict[str, Any]:
        if self.analyzer is not None:
            path: Path | None = None
            owned = False
            try:
                path, owned = self._download(message)
                result = self.analyzer(str(path))
                text = self._result_text(result)
                return {
                    "success": bool(text),
                    "reason": "" if text else "speech_empty",
                    "text": text,
                    "path": str(path),
                }
            except (OSError, ValueError) as exc:
                # wxauto v4 的 VoiceMessage 没有公开 download()，保留其内置
                # to_text() 作为可用后备，避免因为新 ASR 取不到音频而完全失声。
                if str(exc) == "audio_download_not_supported":
                    fallback = self._fallback_to_text(message)
                    if fallback["success"]:
                        fallback["reason"] = "speech_fallback_wxauto"
                    return fallback
                return {"success": False, "reason": str(exc), "text": ""}
            except Exception as exc:  # noqa: BLE001 - 外部 ASR 错误交回工具层
                return {"success": False, "reason": f"speech_error:{type(exc).__name__}:{exc}", "text": ""}
            finally:
                self._cleanup(path, owned)

        return self._fallback_to_text(message)

    def _fallback_to_text(self, message: Any) -> dict[str, Any]:
        to_text = getattr(message, "to_text", None)
        if not callable(to_text):
            return {"success": False, "reason": "speech_to_text_not_supported", "text": ""}
        try:
            text = to_text()
        except Exception as exc:  # noqa: BLE001 - UI 自动化错误交回模型
            return {"success": False, "reason": f"speech_error:{type(exc).__name__}:{exc}", "text": ""}
        text = self._result_text(text)
        return {"success": bool(text), "reason": "" if text else "speech_empty", "text": text}

    def synthesize(self, text: str) -> dict[str, Any]:
        """TTS 预留入口；当前 api_audio.txt 只提供 ASR。"""
        if not isinstance(text, str) or not text.strip():
            return {"success": False, "reason": "tts_text_empty", "audio_path": ""}
        return {
            "success": False,
            "reason": "tts_model_not_configured",
            "audio_path": "",
            "text": text.strip(),
        }

    @staticmethod
    def _result_text(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result.strip()
        if isinstance(result, bytes):
            return result.decode("utf-8", errors="replace").strip()
        if isinstance(result, Mapping):
            for key in ("text", "content", "result", "transcript", "answer"):
                value = result.get(key)
                if value:
                    return SpeechModule._result_text(value)
        for name in ("text", "content", "result", "transcript"):
            try:
                value = getattr(result, name, None)
            except Exception:  # noqa: BLE001
                value = None
            if value:
                return str(value).strip()
        return str(result).strip()

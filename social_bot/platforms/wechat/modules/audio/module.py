"""向当前会话发送已有音频文件。

TTS 生成由后续外部语音模型适配器负责；本模块只负责本地文件校验和 wxauto
SendAudio 调用，因此当前不会发起任何模型请求。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".ogg", ".amr"})


@dataclass(frozen=True)
class AudioOutcome:
    success: bool
    reason: str = ""
    response: Any = None
    path: str = ""


class AudioModule:
    def __init__(self, *, max_bytes: int = 20 * 1024 * 1024) -> None:
        self.max_bytes = max(1, int(max_bytes))

    def _path(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("audio_path_required")
        result = Path(path.strip()).expanduser().resolve(strict=True)
        if not result.is_file():
            raise ValueError("audio_path_must_be_file")
        if result.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError("unsupported_audio_type")
        if result.stat().st_size > self.max_bytes:
            raise ValueError("audio_too_large")
        return result

    def send_audio(self, chat: Any, path: str, duration: int | None = None, start: int = 0) -> AudioOutcome:
        resolved = self._path(path)
        sender = getattr(chat, "SendAudio", None)
        if not callable(sender):
            return AudioOutcome(False, "send_audio_not_supported", path=str(resolved))
        try:
            result = sender(str(resolved), duration=duration, start=int(start))
        except TypeError:
            result = sender(str(resolved))
        except Exception as exc:  # noqa: BLE001
            return AudioOutcome(False, f"send_audio_error:{type(exc).__name__}:{exc}", path=str(resolved))
        if isinstance(result, dict) and result.get("status") not in (None, "成功"):
            return AudioOutcome(False, str(result.get("message") or result), result, str(resolved))
        return AudioOutcome(True, response=result, path=str(resolved))

"""聊天图片和表情包发送能力。

wxauto 将聊天图片、GIF 和表情包都作为文件发送。模块只接受本机已存在的
图片文件，并把发送结果统一成工具层可以消费的结构化结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
MAX_IMAGE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class MediaOutcome:
    success: bool
    reason: str = ""
    response: Any = None
    path: str = ""


def _failed_response(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") not in (None, "成功")


class MediaModule:
    """发送聊天图片或表情包。

    发送动作仍然由当前 wxauto ``Chat`` 对象执行，避免工具参数把消息发到
    未监听的联系人。路径校验在调用 UI 自动化前完成。
    """

    def __init__(self, *, max_bytes: int = MAX_IMAGE_BYTES) -> None:
        self.max_bytes = max(1, int(max_bytes))

    def validate_image(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path 必须是图片文件路径")
        candidate = Path(path.strip()).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError("图片文件不存在") from exc
        except OSError as exc:
            raise ValueError(f"图片路径不可用: {exc}") from exc
        if not resolved.is_file():
            raise ValueError("path 必须指向文件")
        if resolved.suffix.lower() not in IMAGE_EXTENSIONS:
            raise ValueError("只支持 jpg、jpeg、png、gif、webp 或 bmp 图片")
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise ValueError(f"无法读取图片大小: {exc}") from exc
        if size > self.max_bytes:
            raise ValueError(f"图片不能超过 {self.max_bytes // (1024 * 1024)} MB")
        return resolved

    def send_image(self, chat: Any, path: str) -> MediaOutcome:
        resolved = self.validate_image(path)
        send_files = getattr(chat, "SendFiles", None)
        if not callable(send_files):
            return MediaOutcome(False, "send_files_not_supported", path=str(resolved))
        try:
            result = send_files(str(resolved))
        except Exception as exc:  # noqa: BLE001 - UI 自动化错误交回工具层
            return MediaOutcome(False, f"send_image_error:{type(exc).__name__}:{exc}", path=str(resolved))
        if _failed_response(result):
            return MediaOutcome(False, str(result.get("message") or result), result, str(resolved))
        return MediaOutcome(True, response=result, path=str(resolved))

    def send_sticker(self, chat: Any, path: str) -> MediaOutcome:
        """表情包在 wxauto 中也是图片文件，保留语义更清晰的入口。"""
        return self.send_image(chat, path)

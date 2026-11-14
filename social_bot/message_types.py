"""wxauto 消息类型的兼容归一化。"""

from __future__ import annotations

from typing import Any
import re
from pathlib import Path


_IMAGE_TYPES = frozenset(
    {
        "image",
        "img",
        "photo",
        "picture",
        "image_msg",
        "image_message",
        "picture_msg",
        "图片",
        "图片消息",
    }
)
_VOICE_TYPES = frozenset(
    {
        "voice",
        "audio",
        "voice_msg",
        "voice_message",
        "audio_msg",
        "语音",
        "语音消息",
    }
)
_FILE_TYPES = frozenset({"file", "document", "doc", "文件", "文件消息"})
_VIDEO_TYPES = frozenset({'video', 'video_msg', 'video_message', '视频', '视频消息'})
_POKE_TYPES = frozenset({"poke", "pat", "tickle", "拍一拍", "拍了拍"})


def canonical_message_type(value: Any) -> str:
    """将 wxauto 不同版本的类型名称统一成 image/voice 等值。"""
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text in _IMAGE_TYPES:
        return "image"
    if text in _VOICE_TYPES:
        return "voice"
    if text in _FILE_TYPES:
        return "file"
    if text in _VIDEO_TYPES:
        return 'video'
    if text in _POKE_TYPES:
        return "poke"
    return text


def infer_message_type(message: Any) -> str:
    """优先读取 type/msg_type，必要时根据 wxauto 消息类和方法兜底。"""
    unknown_type = ""
    for name in ("type", "msg_type"):
        try:
            value = getattr(message, name, None)
        except Exception:  # noqa: BLE001 - UIA 属性读取失败时继续兜底
            value = None
        normalized = canonical_message_type(value)
        if normalized in {"image", "voice"}:
            return normalized
        if normalized == 'file' and any(
            isinstance(getattr(message, attr, None), (str, Path)) and re.search(r'\.mp4(?:\b|$)', str(getattr(message, attr)), re.I)
            for attr in ('file_path', 'path', 'filename', 'file_name', 'content')):
            return 'video'
        if normalized in {"file", "poke", 'video'}:
            return normalized
        if normalized and not unknown_type:
            unknown_type = normalized

    class_name = str(getattr(getattr(message, "__class__", None), "__name__", "")).lower()
    if 'video' in class_name:
        return 'video'
    if "image" in class_name or callable(getattr(message, "ocr", None)):
        return "image"
    if "voice" in class_name or "audio" in class_name or callable(getattr(message, "to_text", None)):
        return "voice"
    if "file" in class_name or "document" in class_name:
        return "file"
    if any(value in class_name for value in ("poke", "pat", "tickle")):
        return "poke"
    content = str(getattr(message, "content", "") or "")
    if unknown_type in {"", "note", "other"} and any(
        phrase in content for phrase in ("拍一拍", "拍了拍", "戳了戳")
    ):
        return "poke"
    return unknown_type

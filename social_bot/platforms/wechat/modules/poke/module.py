"""拍一拍消息识别和发送。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


POKE_TYPES = frozenset({"poke", "pat", "tickle", "拍一拍", "拍了拍"})
POKE_PHRASES = ("拍一拍", "拍了拍", "戳了戳", "拍了我")


def is_poke_message(message: Any) -> bool:
    known_type = ""
    for name in ("type", "msg_type"):
        value = getattr(message, name, None)
        normalized = str(value or "").strip().lower()
        if normalized in POKE_TYPES:
            return True
        if normalized:
            known_type = normalized
    class_name = str(getattr(getattr(message, "__class__", None), "__name__", "")).lower()
    if any(value in class_name for value in ("pokemessage", "patmessage", "ticklemessage")):
        return True
    content = str(getattr(message, "content", "") or "")
    return known_type in {"", "note", "other"} and any(phrase in content for phrase in POKE_PHRASES)


@dataclass(frozen=True)
class PokeOutcome:
    success: bool
    reason: str = ""
    text: str = ""
    response: Any = None


class PokeModule:
    def recognize(self, message: Any) -> PokeOutcome:
        if not is_poke_message(message):
            return PokeOutcome(False, "not_poke_message")
        text = str(getattr(message, "content", "") or "").strip() or "[拍一拍消息]"
        return PokeOutcome(True, text=text, response={"sender": str(getattr(message, "sender", "") or "")})

    def poke(self, message: Any) -> PokeOutcome:
        tickle = getattr(message, "tickle", None)
        if not callable(tickle):
            return PokeOutcome(False, "poke_not_supported")
        try:
            result = tickle()
        except Exception as exc:  # noqa: BLE001 - UI 自动化错误交回工具层
            return PokeOutcome(False, f"poke_error:{type(exc).__name__}:{exc}")
        if isinstance(result, dict) and result.get("status") not in (None, "成功"):
            return PokeOutcome(False, str(result.get("message") or result), response=result)
        return PokeOutcome(True, response=result)

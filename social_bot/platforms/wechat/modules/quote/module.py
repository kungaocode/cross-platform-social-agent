from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QuoteOutcome:
    success: bool
    reason: str = ""
    response: Any = None


def _is_failed_response(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") not in (None, "成功")


class QuoteModule:
    """调用 wxauto 的消息引用能力，发送引用消息并可选择 @ 对方。"""

    def quote(
        self,
        message: Any,
        text: str,
        at: str | list[str] | None = None,
        timeout: int = 3,
    ) -> QuoteOutcome:
        quote = getattr(message, "quote", None)
        if not callable(quote):
            return QuoteOutcome(False, "quote_not_supported")
        try:
            response = quote(text, at=at, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 由调用方决定是否重试
            return QuoteOutcome(False, f"quote_error:{type(exc).__name__}:{exc}")
        if _is_failed_response(response):
            return QuoteOutcome(False, "quote_failed", response)
        return QuoteOutcome(True, response=response)

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


RECALL_OPTION = "撤回"


@dataclass(frozen=True)
class RecallOutcome:
    success: bool
    reason: str = ""
    response: Any = None


def _is_failed_response(result: Any) -> bool:
    return result is False or (isinstance(result, dict) and (
        result.get("success") is False or result.get("status") not in (None, "成功")
    ))


class RecallModule:
    """通过消息右键菜单撤回一条自己发送的消息。"""

    def __init__(self, option: str = RECALL_OPTION) -> None:
        self.option = option

    @staticmethod
    def _is_self_message(message: Any) -> bool:
        for name in ("attr", "msg_attr", "from_type", "type"):
            value = getattr(message, name, None)
            if value is not None and str(value).strip().lower() == "self":
                return True
        return False

    def recall(self, message: Any, timeout: int = 2, require_self: bool = True) -> RecallOutcome:
        """撤回消息。

        微信只允许撤回自己发送的消息，默认检查 ``attr == 'self'``；测试或其他版本
        可以显式传 ``require_self=False`` 关闭这层检查。
        """
        if require_self and not self._is_self_message(message):
            return RecallOutcome(False, "not_self_message")

        select_option = getattr(message, "select_option", None)
        if not callable(select_option):
            return RecallOutcome(False, "recall_not_supported")
        try:
            response = select_option(self.option, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 交给调用方决定如何提示
            return RecallOutcome(False, f"recall_error:{type(exc).__name__}:{exc}")
        if _is_failed_response(response):
            return RecallOutcome(False, "recall_failed", response)
        return RecallOutcome(True, response=response)

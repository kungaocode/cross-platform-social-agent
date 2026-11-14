"""当前微信群聊的管理动作。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any


GROUP_VALUES = frozenset({"group", "groupchat", "chatroom"})
LOGGER = logging.getLogger(__name__)


def is_group_chat(chat: Any) -> bool:
    """尽量兼容 wxauto4/wxautox4 的群聊标记。"""
    for name in ("chat_type", "type"):
        try:
            value = getattr(chat, name, None)
        except Exception:  # noqa: BLE001 - UIA 属性失败时继续检查
            value = None
        if isinstance(value, str) and value.strip().lower() in GROUP_VALUES:
            return True
    try:
        value = getattr(chat, "is_group", False)
        if callable(value):
            value = value()
        return value is True
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class GroupOutcome:
    success: bool
    reason: str = ""
    response: Any = None


class GroupModule:
    """封装群公告、群内昵称和群备注修改。"""

    def _run(self, chat: Any, method_name: str, value: str) -> GroupOutcome:
        def finish(outcome):
            log = LOGGER.info if outcome.success else LOGGER.warning
            log("微信群设置执行结果 method=%s success=%s reason=%s", method_name, outcome.success, outcome.reason or 'ok')
            return outcome

        if not is_group_chat(chat):
            return finish(GroupOutcome(False, "group_chat_required"))
        method = getattr(chat, method_name, None)
        if not callable(method):
            return finish(GroupOutcome(False, f"{method_name}_not_supported"))
        LOGGER.info("开始执行微信群设置 method=%s chars=%s", method_name, len(value))
        try:
            result = method(value)
        except Exception as exc:  # noqa: BLE001 - UI 自动化错误交回工具层
            return finish(GroupOutcome(False, f"{method_name}_error:{type(exc).__name__}:{exc}"))
        if result is False:
            return finish(GroupOutcome(False, f"{method_name}_failed", result))
        if isinstance(result, dict) and (result.get("success") is False or result.get("status") not in (None, "成功")):
            return finish(GroupOutcome(False, str(result.get("message") or result), result))
        return finish(GroupOutcome(True, response=result))

    def set_announcement(self, chat: Any, text: str) -> GroupOutcome:
        return self._run(chat, "SetGroupAnnouncement", text)

    def set_my_nickname(self, chat: Any, nickname: str) -> GroupOutcome:
        return self._run(chat, "SetGroupMyNickname", nickname)

    def set_remark(self, chat: Any, remark: str) -> GroupOutcome:
        return self._run(chat, "SetGroupRemark", remark)

    # 语义更完整的别名，便于适配器或外部命令模块直接调用。
    set_group_announcement = set_announcement
    set_group_my_nickname = set_my_nickname
    set_group_remark = set_remark

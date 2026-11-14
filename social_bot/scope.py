from __future__ import annotations

from dataclasses import dataclass

from .settings import ScopeSettings


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    reason: str


class ScopeGuard:
    """决定一条消息是否允许进入模型调用流程。"""

    def __init__(self, settings: ScopeSettings) -> None:
        self.settings = settings

    def group_is_allowed(self, group_id: str) -> bool:
        return self.settings.enabled and str(group_id).strip() in self.settings.groups

    def group_command(self, text: str) -> str | None:
        """返回 activate/deactivate；控制词为空或未匹配时返回 None。"""
        if not self.settings.group_activation_enabled:
            return None
        content = str(text).strip()
        if self.settings.group_activate_keyword and self.settings.group_activate_keyword in content:
            return "activate"
        if self.settings.group_deactivate_keyword and self.settings.group_deactivate_keyword in content:
            return "deactivate"
        return None

    def check(self, message: dict) -> ScopeDecision:
        if not self.settings.enabled:
            return ScopeDecision(False, "scope_disabled")

        text = str(message.get("text", "")).strip()
        if not text:
            return ScopeDecision(False, "empty_message")
        if len(text) > self.settings.max_input_chars:
            return ScopeDecision(False, "message_too_long")

        chat_type = str(message.get("chat_type", "")).lower()
        if chat_type == "private":
            user_id = str(message.get("sender_id") or message.get("chat_id") or "").strip()
            if user_id in self.settings.private_users:
                return ScopeDecision(True, "private_user_allowed")
            return ScopeDecision(False, "private_user_not_allowed")

        if chat_type == "group":
            group_id = str(message.get("chat_id", "")).strip()
            if not self.group_is_allowed(group_id):
                return ScopeDecision(False, "group_not_allowed")
            if self.settings.group_require_mention and not bool(message.get("is_mentioned", False)):
                return ScopeDecision(False, "group_mention_required")
            if self.settings.group_keywords:
                if not any(keyword in text for keyword in self.settings.group_keywords):
                    return ScopeDecision(False, "group_keyword_not_matched")
            return ScopeDecision(True, "group_allowed")

        return ScopeDecision(False, "unknown_chat_type")

import unittest

from social_bot.scope import ScopeGuard
from social_bot.settings import ScopeSettings


class ScopeGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = ScopeGuard(
            ScopeSettings(
                private_users=frozenset({"u1"}),
                groups=frozenset({"g1"}),
                group_require_mention=True,
                group_keywords=("机器人",),
            )
        )

    def test_private_allowlist(self) -> None:
        self.assertTrue(self.guard.check({"chat_type": "private", "sender_id": "u1", "text": "hi"}).allowed)
        self.assertFalse(self.guard.check({"chat_type": "private", "sender_id": "u2", "text": "hi"}).allowed)

    def test_group_requires_scope_mention_and_keyword(self) -> None:
        base = {"chat_type": "group", "chat_id": "g1", "text": "机器人 你好"}
        self.assertFalse(self.guard.check(base).allowed)
        base["is_mentioned"] = True
        self.assertTrue(self.guard.check(base).allowed)
        base["text"] = "你好"
        self.assertFalse(self.guard.check(base).allowed)

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from social_bot.service import BotService
from social_bot.settings import AppSettings, DeepSeekSettings, RuntimeSettings, ScopeSettings, WeChatSettings


class Client:
    def __init__(self):
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return "收到"


class GroupMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = AppSettings(api_key="unused", deepseek=DeepSeekSettings(),
            scope=ScopeSettings(groups=frozenset({"g1", "g2"})),
            runtime=RuntimeSettings(database_path=str(Path(self.tmp.name) / "memory.sqlite3"),
                                    cooldown_seconds=0, wechat_group_memory_enabled=True),
            wechat=WeChatSettings())
        self.client = Client()
        self.bot = BotService(self.settings, self.client)

    def tearDown(self):
        self.bot.close()
        self.tmp.cleanup()

    def msg(self, **kwargs):
        return dict(platform="wechat", chat_type="group", chat_id="g1", sender_id="alice",
                    sender_name="小甲", text="周六下午三点集合", message_id="one") | kwargs

    def test_unmentioned_and_inactive_messages_survive_restart_and_reach_context(self):
        self.assertFalse(self.bot.handle(self.msg())["accepted"])
        self.assertFalse(self.bot.handle(self.msg())["accepted"])
        self.assertEqual(self.client.calls, [])
        self.bot.close()
        self.bot = BotService(self.settings, self.client)
        self.bot.store.set_group_active("wechat", "g1", True)
        self.bot.handle(self.msg(text="我带饮料", message_id="two", sender_id="bob", sender_name="小乙"))
        result = self.bot.handle(self.msg(text="@机器人 几点集合？", message_id="three", is_mentioned=True))
        self.assertTrue(result["accepted"])
        context = str(self.client.calls[-1])
        self.assertIn("小甲", context)
        self.assertIn("小乙", context)
        self.assertIn("周六下午三点集合", context)
        rows = self.bot.store.connection.execute("SELECT COUNT(*) FROM messages WHERE message_id='one'").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_other_groups_and_disabled_scope_are_not_archived(self):
        self.bot.handle(self.msg(chat_id="other"))
        self.bot.scope.settings = replace(self.settings.scope, enabled=False)
        self.bot.handle(self.msg())
        self.assertEqual(self.bot.store.history("wechat:group:g1", 20), [])
        self.assertEqual(self.bot.store.history("wechat:group:other", 20), [])

    def test_group_context_does_not_leak_to_another_group(self):
        self.bot.handle(self.msg())
        self.bot.store.set_group_active("wechat", "g2", True)
        self.bot.handle(self.msg(chat_id="g2", text="@机器人 你好", message_id="different", is_mentioned=True))
        self.assertNotIn("周六下午三点集合", str(self.client.calls[-1]))

    def test_cooldown_message_is_archived_without_model_call(self):
        self.bot.settings = replace(self.settings, runtime=replace(self.settings.runtime, cooldown_seconds=999))
        self.bot.store.set_group_active("wechat", "g1", True)
        self.bot.handle(self.msg(is_mentioned=True))
        self.bot.handle(self.msg(text="补充说明", message_id="two", is_mentioned=True))
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("补充说明", str(self.bot.store.history("wechat:group:g1", 20)))

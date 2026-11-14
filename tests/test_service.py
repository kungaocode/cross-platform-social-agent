import tempfile
import unittest
from pathlib import Path

from social_bot.service import BotService
from social_bot.settings import AppSettings, DeepSeekSettings, RuntimeSettings, ScopeSettings, WeChatSettings


class FakeClient:
    def __init__(self) -> None:
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return "测试回复"


class ServiceTests(unittest.TestCase):
    def test_scope_blocks_before_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(
                api_key="unused",
                deepseek=DeepSeekSettings(),
                scope=ScopeSettings(private_users=frozenset({"u1"})),
                runtime=RuntimeSettings(database_path=str(Path(folder) / "bot.sqlite3"), cooldown_seconds=0),
                wechat=WeChatSettings(),
            )
            client = FakeClient()
            service = BotService(settings, client=client)
            result = service.handle({"platform": "qq", "chat_type": "private", "sender_id": "u2", "chat_id": "u2", "text": "hi"})
            self.assertFalse(result["accepted"])
            self.assertEqual(result["reason"], "private_user_not_allowed")
            self.assertEqual(client.calls, [])
            service.close()

    def test_allowed_message_uses_session_history(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(
                api_key="unused",
                deepseek=DeepSeekSettings(),
                scope=ScopeSettings(private_users=frozenset({"u1"})),
                runtime=RuntimeSettings(database_path=str(Path(folder) / "bot.sqlite3"), cooldown_seconds=0),
                wechat=WeChatSettings(),
            )
            client = FakeClient()
            service = BotService(settings, client=client)
            message = {"platform": "qq", "chat_type": "private", "sender_id": "u1", "chat_id": "u1", "text": "hi", "message_id": "m1"}
            result = service.handle(message)
            self.assertTrue(result["accepted"])
            self.assertEqual(result["reply"], "测试回复")
            self.assertIn("情绪或小心思", client.calls[0][1]["content"])
            second = dict(message, text="再见", message_id="m2")
            service.handle(second)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(client.calls[1][-2]["role"], "assistant")
            service.close()

    def test_group_activation_and_deactivation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(
                api_key="unused",
                deepseek=DeepSeekSettings(),
                scope=ScopeSettings(groups=frozenset({"g1"}), group_require_mention=True),
                runtime=RuntimeSettings(database_path=str(Path(folder) / "bot.sqlite3"), cooldown_seconds=0),
                wechat=WeChatSettings(),
            )
            client = FakeClient()
            service = BotService(settings, client=client)
            base = {"platform": "qq", "chat_type": "group", "chat_id": "g1", "sender_id": "u1"}
            inactive = service.handle(dict(base, text="@机器人 你好", is_mentioned=True, message_id="m1"))
            self.assertEqual(inactive["reason"], "group_not_active")
            started = service.handle(dict(base, text="转人机", is_mentioned=False, message_id="m2"))
            self.assertEqual(started["reply"], "大肥鱼收到")
            answered = service.handle(dict(base, text="@机器人 你好", is_mentioned=True, message_id="m3"))
            self.assertTrue(answered["accepted"])
            stopped = service.handle(dict(base, text="转人工", is_mentioned=False, message_id="m4"))
            self.assertEqual(stopped["control"], "deactivate")
            after_stop = service.handle(dict(base, text="@机器人 还在吗", is_mentioned=True, message_id="m5"))
            self.assertEqual(after_stop["reason"], "group_not_active")
            self.assertEqual(len(client.calls), 1)
            service.close()

    def test_global_group_media_can_enter_without_mention(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(
                api_key="unused",
                deepseek=DeepSeekSettings(),
                scope=ScopeSettings(groups=frozenset({"g1"}), group_require_mention=True),
                runtime=RuntimeSettings(
                    database_path=str(Path(folder) / "bot.sqlite3"),
                    cooldown_seconds=0,
                    tool_calling_enabled=True,
                    multimodal_mode="global",
                ),
                wechat=WeChatSettings(),
            )
            client = FakeClient()
            service = BotService(settings, client=client)
            base = {"platform": "wechat", "chat_type": "group", "chat_id": "g1", "sender_id": "u1"}
            service.handle(dict(base, text="转人机", message_id="activate-media"))
            result = service.handle(
                dict(base, text="[图片消息]", message_type="image", is_mentioned=False, message_id="image-media")
            )

            self.assertTrue(result["accepted"])
            self.assertEqual(result["reply"], "测试回复")
            service.close()

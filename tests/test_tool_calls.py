from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from social_bot.deepseek import DeepSeekClient
from social_bot.service import BotService
from social_bot.settings import AppSettings, DeepSeekSettings, RuntimeSettings, ScopeSettings, WeChatSettings
from social_bot.tools import WeChatToolExecutor


class StubDeepSeekClient(DeepSeekClient):
    def __init__(self, responses):
        super().__init__("unused", DeepSeekSettings())
        self.responses = list(responses)
        self.requests = []

    def _request(self, payload):
        self.requests.append(payload)
        return self.responses.pop(0)


class ToolCallProtocolTests(unittest.TestCase):
    def test_tool_call_is_returned_to_model_before_final_answer(self) -> None:
        client = StubDeepSeekClient(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "wechat_quote",
                                            "arguments": '{"text":"收到","at":null}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {"choices": [{"message": {"role": "assistant", "content": "已引用"}}]},
            ]
        )

        result = client.complete_with_tools(
            [{"role": "user", "content": "引用回复"}],
            [{"type": "function", "function": {"name": "wechat_quote"}}],
            lambda name, args: {"success": True, "message_sent": True, "side_effect_performed": True},
        )

        self.assertEqual(result.content, "已引用")
        self.assertTrue(result.side_effect_performed)
        self.assertEqual(client.requests[1]["messages"][-1]["role"], "tool")
        self.assertEqual(client.requests[1]["messages"][-1]["tool_call_id"], "call_1")

    def test_content_tool_stops_unrelated_followup_tools(self) -> None:
        client = StubDeepSeekClient(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_link",
                                        "type": "function",
                                        "function": {
                                            "name": "link_read",
                                            "arguments": '{"url":"https://example.com"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {"choices": [{"message": {"role": "assistant", "content": "链接内容已读完"}}]},
            ]
        )

        result = client.complete_with_tools(
            [{"role": "user", "content": "读取这个链接 https://example.com"}],
            [
                {"type": "function", "function": {"name": "link_read"}},
                {"type": "function", "function": {"name": "weather_forecast"}},
            ],
            lambda name, args: {"success": True, "text": "文章内容"},
        )

        self.assertEqual(result.content, "链接内容已读完")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn("tools", client.requests[1])

    def test_time_then_weather_is_the_only_allowed_chain(self) -> None:
        client = StubDeepSeekClient(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_time",
                                        "type": "function",
                                        "function": {"name": "time_now", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_weather",
                                        "type": "function",
                                        "function": {"name": "weather_forecast", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
                {"choices": [{"message": {"role": "assistant", "content": "天气已查询"}}]},
            ]
        )

        result = client.complete_with_tools(
            [{"role": "user", "content": "现在天气怎么样"}],
            [
                {"type": "function", "function": {"name": "time_now"}},
                {"type": "function", "function": {"name": "weather_forecast"}},
            ],
            lambda name, args: {"success": True},
        )

        self.assertEqual(result.content, "天气已查询")
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(len(client.requests), 3)
        self.assertNotIn("tools", client.requests[2])


class WeChatToolExecutorTests(unittest.TestCase):
    def test_quote_tool_prepares_then_commits_final_text(self) -> None:
        adapter = mock.Mock()
        adapter.quote_message.return_value = SimpleNamespace(success=True, reason="")
        executor = WeChatToolExecutor(adapter, "incoming", "chat")

        result = executor.execute("wechat_quote", {"text": "收到", "at": None})

        self.assertTrue(result["success"])
        self.assertFalse(result["side_effect_performed"])
        adapter.quote_message.assert_not_called()
        executor.commit_reply("最终正文",result["reply_delivery"])
        adapter.quote_message.assert_called_once_with("incoming", "最终正文", at=None)

    def test_recall_requires_recent_self_message_and_confirmation(self) -> None:
        adapter = mock.Mock()
        adapter.last_self_message.return_value = "outgoing"
        adapter.recall_message.return_value = SimpleNamespace(success=True, reason="")
        executor = WeChatToolExecutor(
            adapter,
            SimpleNamespace(content="请撤回你刚才发错的消息"),
            "chat",
            allow_recall=True,
        )

        missing_confirmation = executor.execute("wechat_recall", {"reason": "用户要求", "confirm": False})
        self.assertFalse(missing_confirmation["success"])
        adapter.recall_message.assert_not_called()

        result = executor.execute("wechat_recall", {"reason": "用户要求", "confirm": True})
        self.assertTrue(result["success"])
        adapter.recall_message.assert_called_once_with("outgoing")


class ServiceToolIntegrationTests(unittest.TestCase):
    def test_service_passes_enabled_tools_to_client_and_marks_sent_action(self) -> None:
        class FakeClient:
            def __init__(self):
                self.executor = None

            def complete_with_tools(self, messages, definitions, execute, max_rounds, tool_choice="auto"):
                self.executor = (messages, definitions, max_rounds, tool_choice)
                result = execute("wechat_quote", {"text": "收到", "at": None})
                self.assert_result = result
                return SimpleNamespace(content="已引用", tool_calls=1, side_effect_performed=True, message_sent=True)

        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(
                api_key="unused",
                deepseek=DeepSeekSettings(),
                scope=ScopeSettings(private_users=frozenset({"u1"})),
                runtime=RuntimeSettings(
                    database_path=str(Path(folder) / "bot.sqlite3"),
                    cooldown_seconds=0,
                    tool_calling_enabled=True,
                ),
                wechat=WeChatSettings(),
            )
            client = FakeClient()
            service = BotService(settings, client=client)
            executor = SimpleNamespace(definitions=[{"type": "function"}], execute=lambda name, args: {"success": True})

            result = service.handle(
                {"platform": "wechat", "chat_type": "private", "sender_id": "u1", "chat_id": "u1", "text": "引用", "message_id": "m1"},
                tool_executor=executor,
            )

            self.assertTrue(result["tool_used"])
            self.assertTrue(result["tool_already_sent"])
            self.assertEqual(client.executor[2], 3)
            self.assertEqual(client.executor[3], "auto")
            service.close()

    def test_image_message_forces_ocr_tool_choice(self) -> None:
        class FakeClient:
            def __init__(self):
                self.tool_choice = None

            def complete_with_tools(self, messages, definitions, execute, max_rounds, tool_choice="auto"):
                self.tool_choice = tool_choice
                return SimpleNamespace(content="图片中写着测试", tool_calls=1, side_effect_performed=False)

        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(
                api_key="unused",
                deepseek=DeepSeekSettings(),
                scope=ScopeSettings(private_users=frozenset({"u1"})),
                runtime=RuntimeSettings(
                    database_path=str(Path(folder) / "bot.sqlite3"),
                    cooldown_seconds=0,
                    tool_calling_enabled=True,
                ),
                wechat=WeChatSettings(),
            )
            client = FakeClient()
            service = BotService(settings, client=client)
            executor = SimpleNamespace(
                definitions=[
                    {"type": "function", "function": {"name": "wechat_ocr"}},
                    {"type": "function", "function": {"name": "wechat_quote"}},
                ],
                execute=lambda name, args: {"success": True},
            )

            result = service.handle(
                {
                    "platform": "wechat",
                    "chat_type": "private",
                    "sender_id": "u1",
                    "chat_id": "u1",
                    "text": "[图片消息]",
                    "message_type": "image",
                    "message_id": "image-1",
                },
                tool_executor=executor,
            )

            self.assertTrue(result["accepted"])
            self.assertEqual(
                client.tool_choice,
                {"type": "function", "function": {"name": "wechat_ocr"}},
            )
            service.close()

    def test_global_image_prefers_visual_tool_when_ocr_also_enabled(self) -> None:
        class FakeClient:
            def __init__(self):
                self.tool_choice = None

            def complete_with_tools(self, messages, definitions, execute, max_rounds, tool_choice="auto"):
                self.tool_choice = tool_choice
                return SimpleNamespace(content="图片识别完成", tool_calls=1, side_effect_performed=False)

        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(
                api_key="unused",
                deepseek=DeepSeekSettings(),
                scope=ScopeSettings(private_users=frozenset({"u1"})),
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
            executor = SimpleNamespace(
                multimodal_mode="global",
                multimodal_auto_read=True,
                definitions=[
                    {"type": "function", "function": {"name": "wechat_ocr"}},
                    {"type": "function", "function": {"name": "wechat_visual_question"}},
                ],
                execute=lambda name, args: {"success": True},
            )

            result = service.handle(
                {
                    "platform": "wechat",
                    "chat_type": "private",
                    "sender_id": "u1",
                    "chat_id": "u1",
                    "text": "[图片消息]",
                    "message_type": "image",
                    "message_id": "image-visual-1",
                },
                tool_executor=executor,
            )

            self.assertTrue(result["accepted"])
            self.assertEqual(
                client.tool_choice,
                {"type": "function", "function": {"name": "wechat_visual_question"}},
            )
            service.close()

    def test_mention_mode_leaves_media_choice_to_model(self) -> None:
        class FakeClient:
            def __init__(self):
                self.tool_choice = None

            def complete_with_tools(self, messages, definitions, execute, max_rounds, tool_choice="auto"):
                self.tool_choice = tool_choice
                return SimpleNamespace(content="我可以帮你识别图片", tool_calls=0, side_effect_performed=False)

        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(
                api_key="unused",
                deepseek=DeepSeekSettings(),
                scope=ScopeSettings(private_users=frozenset({"u1"})),
                runtime=RuntimeSettings(
                    database_path=str(Path(folder) / "bot.sqlite3"),
                    cooldown_seconds=0,
                    tool_calling_enabled=True,
                    multimodal_mode="mention",
                ),
                wechat=WeChatSettings(),
            )
            client = FakeClient()
            service = BotService(settings, client=client)
            executor = SimpleNamespace(
                multimodal_mode="mention",
                multimodal_auto_read=False,
                definitions=[
                    {"type": "function", "function": {"name": "wechat_ocr"}},
                    {"type": "function", "function": {"name": "wechat_quote"}},
                ],
                execute=lambda name, args: {"success": True},
            )

            result = service.handle(
                {
                    "platform": "wechat",
                    "chat_type": "private",
                    "sender_id": "u1",
                    "chat_id": "u1",
                    "text": "@机器人帮我识别图片",
                    "is_mentioned": True,
                    "message_type": "image",
                    "message_id": "image-mention-1",
                },
                tool_executor=executor,
            )

            self.assertTrue(result["accepted"])
            self.assertEqual(client.tool_choice, "auto")
            service.close()


if __name__ == "__main__":
    unittest.main()

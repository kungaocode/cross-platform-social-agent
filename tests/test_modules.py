from __future__ import annotations

import types
import unittest
from collections import deque
from unittest import mock

from social_bot.modules.quote import QuoteModule
from social_bot.modules.recall import RecallModule
from social_bot.modules.text_reply import TextReplyModule
from social_bot.settings import ScopeSettings, WeChatSettings
from social_bot.wechat import WeChatAdapter


class TextReplyModuleTests(unittest.TestCase):
    def _module(self, service: object, send_reply=None) -> TextReplyModule:
        return TextReplyModule(
            service=service,
            bot_name="大肥鱼",
            normalize=lambda *_args, **_kwargs: {
                "chat_id": "测试联系人",
                "chat_type": "private",
                "sender_id": "测试联系人",
                "text": "你好",
            },
            accept_self_if=None,
            sent_replies=deque(),
            send_reply=send_reply,
        )

    def test_sends_service_reply_and_records_it(self) -> None:
        service = mock.Mock()
        service.handle.return_value = {"accepted": True, "reply": "你好呀"}
        chat = mock.Mock()
        chat.SendMsg.return_value = {"status": "成功"}
        module = self._module(service)

        outcome = module.handle(object(), chat)

        self.assertTrue(outcome.sent)
        self.assertEqual(outcome.reply, "你好呀")
        chat.SendMsg.assert_called_once_with("你好呀")
        self.assertEqual(list(module.sent_replies), ["你好呀"])

    def test_service_exception_is_contained(self) -> None:
        service = mock.Mock()
        service.handle.side_effect = RuntimeError("模型失败")
        chat = mock.Mock()

        outcome = self._module(service).handle(object(), chat)

        self.assertTrue(outcome.handled)
        self.assertEqual(outcome.reason, "service_error")
        chat.SendMsg.assert_not_called()


class RecallModuleTests(unittest.TestCase):
    def test_only_self_message_is_recalled_by_default(self) -> None:
        message = mock.Mock(attr="friend")

        outcome = RecallModule().recall(message)

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, "not_self_message")
        message.select_option.assert_not_called()

    def test_selects_recall_option_for_self_message(self) -> None:
        message = mock.Mock(attr="self")
        message.select_option.return_value = {"status": "成功"}

        outcome = RecallModule().recall(message, timeout=4)

        self.assertTrue(outcome.success)
        message.select_option.assert_called_once_with("撤回", timeout=4)

    def test_supports_legacy_self_marker_in_type(self) -> None:
        message = mock.Mock(attr="friend", type="self")
        message.select_option.return_value = {"status": "成功"}

        outcome = RecallModule().recall(message)

        self.assertTrue(outcome.success)


class QuoteModuleTests(unittest.TestCase):
    def test_quotes_message(self) -> None:
        message = mock.Mock()
        message.quote.return_value = {"status": "成功"}

        outcome = QuoteModule().quote(message, "这是引用回复", at="张三", timeout=5)

        self.assertTrue(outcome.success)
        message.quote.assert_called_once_with("这是引用回复", at="张三", timeout=5)

    def test_missing_quote_api_is_reported(self) -> None:
        outcome = QuoteModule().quote(object(), "回复")

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, "quote_not_supported")


class WeChatModuleIntegrationTests(unittest.TestCase):
    @staticmethod
    def _service() -> object:
        service = mock.Mock()
        service.settings = types.SimpleNamespace(scope=ScopeSettings())
        service.handle.return_value = {"accepted": True, "reply": "引用回复"}
        return service

    def test_quote_mode_uses_quote_module_for_model_reply(self) -> None:
        adapter = WeChatAdapter(
            self._service(),
            WeChatSettings(enabled=True, targets=("测试联系人",), reply_mode="quote"),
        )
        message = mock.Mock()
        message.quote.return_value = {"status": "成功"}
        chat = mock.Mock()

        sent = adapter._send_reply(message, chat, "引用回复")

        self.assertEqual(sent["status"], "成功")
        message.quote.assert_called_once_with("引用回复", at=None, timeout=3)
        chat.SendMsg.assert_not_called()

    def test_adapter_exposes_recall_and_quote_modules(self) -> None:
        adapter = WeChatAdapter(self._service(), WeChatSettings())
        self_message = mock.Mock(attr="self")
        self_message.select_option.return_value = {"status": "成功"}
        friend_message = mock.Mock()
        friend_message.quote.return_value = {"status": "成功"}

        recall = adapter.recall_message(self_message)
        quote = adapter.quote_message(friend_message, "回复")

        self.assertTrue(recall.success)
        self.assertTrue(quote.success)


if __name__ == "__main__":
    unittest.main()

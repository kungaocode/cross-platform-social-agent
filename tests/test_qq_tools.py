import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from social_bot.platforms.qq.qzone import QzonePublisher, match_publish_command
from social_bot.platforms.qq.tools import QQToolExecutor
from social_bot.platforms.qq.worker import QQWorker, build_settings
from social_bot.service import BotService
from social_bot.settings import AppSettings, RuntimeSettings, ScopeSettings, DeepSeekSettings, WeChatSettings


class QQToolsTests(unittest.TestCase):
    def setUp(self):
        self.runtime = RuntimeSettings(tool_calling_enabled=True, allow_weather_tool=True,
                                       allow_research_tool=True, allow_time_tool=True, allow_link_tool=True)

    def test_read_tools_never_expose_wechat_operations(self):
        weather, research = Mock(), Mock()
        weather.forecast.return_value = {"success": True, "provider": "amap"}
        research.search.return_value = {"success": True, "provider": "ddgs_duckduckgo", "results": []}
        executor = QQToolExecutor(self.runtime, "test", weather=weather, research=research)
        self.assertEqual({d["function"]["name"] for d in executor.definitions},
                         {"weather_forecast", "research_search", "time_now", "link_read"})
        self.assertTrue(executor.execute("weather_forecast", {"location": "上海", "days": 2})["success"])
        self.assertIn("query_time", weather.forecast.call_args.kwargs)
        self.assertEqual(executor.execute("research_search", {"query": "Python"})["provider"], "ddgs_duckduckgo")
        self.assertFalse(executor.execute("wechat_recall", {"confirm": True})["success"])
        executor.runtime = replace(self.runtime, allow_research_tool=False)
        self.assertFalse(executor.execute("research_search", {"query": "Python"})["success"])

    def test_link_reader_requires_url_in_current_message(self):
        links = Mock()
        links.read.return_value = SimpleNamespace()  # 不调用模拟成功分支。
        executor = QQToolExecutor(self.runtime, "读一下 https://example.com", links=links)
        self.assertFalse(executor.execute("link_read", {"url": "https://other.example.com"})["success"])
        links.read.assert_not_called()

    def test_qq_settings_default_to_independent_read_tools_and_validate_switches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "api_deepseek.txt").write_text("sk-test-only-placeholder", encoding="utf-8")
            (root / "base.json").write_text(json.dumps({"api_key": "test-only"}), encoding="utf-8")
            config = {"base_config": "base.json", "memory_dir": folder, "private_users": ["u"]}
            path = root / "qq.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            settings = build_settings(path, "123")
            self.assertTrue(settings.runtime.tool_calling_enabled)
            self.assertTrue(settings.runtime.allow_research_tool)
            self.assertTrue(settings.runtime.allow_weather_tool)
            for switch in ('allow_research_tool', 'document_reading_enabled'):
                config[switch] = "false"
                path.write_text(json.dumps(config), encoding="utf-8")
                with self.subTest(switch=switch), self.assertRaises(ValueError):
                    build_settings(path, "123")
                config.pop(switch)


class QzoneTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.transport = Mock(return_value={"status": "ok", "retcode": 0, "data": {"tid": "post1"}})
        self.config = dict(enabled=True, owner_openids=["owner"], base_url="http://127.0.0.1:3000", token_env="TEST_QZONE_TOKEN", ugc_right=4)
        self.publisher = QzonePublisher(self.db, self.config, transport=self.transport)
        self.env = patch.dict(os.environ, {"TEST_QZONE_TOKEN": "test-only"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.db.close()

    def publish(self, text="[QQ说说]\n复制：第一行\n第二行", key="event1", sender="owner", kind="c2c", **arguments):
        return self.publisher.publish(key, sender, kind, match_publish_command(text), {"text": "模型生成内容", "confirm": True, **arguments})

    def test_copy_publish_is_exact_and_duplicate_event_reuses_receipt(self):
        result = self.publish()
        self.assertTrue(result["success"])
        self.assertEqual(result["tid"], "post1")
        self.transport.assert_called_once_with({"content": "第一行\n第二行", "ugc_right": 4})
        self.assertTrue(self.publish()["cached"])
        self.transport.assert_called_once()

    def test_generated_content_and_natural_command(self):
        self.assertTrue(self.publish("[QQ空间]\n生成：写一句周末问候")["success"])
        self.assertEqual(self.transport.call_args.args[0]["content"], "模型生成内容")
        self.assertIsNotNone(match_publish_command("帮我发一条说说：周末愉快"))
        for text in ["不要发说说：test", "如何发说说", "帮我写一条说说草稿", "[QQ说说]\n复制："]:
            self.assertIsNone(match_publish_command(text))

    def test_owner_private_chat_confirm_and_enabled_are_required(self):
        self.assertFalse(self.publish(sender="stranger")["success"])
        self.assertFalse(self.publish(kind="group")["success"])
        self.assertFalse(self.publish(confirm=False)["success"])
        self.config["enabled"] = False
        self.assertFalse(self.publish()["success"])
        self.transport.assert_not_called()

    def test_unknown_response_or_timeout_is_persisted_and_never_retried(self):
        self.transport.side_effect = TimeoutError("secret-token")
        self.assertEqual(self.publish()["reason"], "qzone_result_unknown_no_retry")
        self.publisher = QzonePublisher(self.db, self.config, transport=self.transport)
        self.assertEqual(self.publish()["reason"], "qzone_result_unknown_no_retry")
        self.transport.assert_called_once()

    def test_missing_token_never_attempts_publish(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.publish()["reason"], "qzone_service_not_configured")
        self.transport.assert_not_called()

    def test_api_rejection_does_not_claim_success_or_retry(self):
        self.transport.return_value = {"status": "failed", "retcode": 1401, "message": "sensitive"}
        self.assertFalse(self.publish()["success"])
        self.assertEqual(self.publish()["reason"], "qzone_publish_failed_no_retry")
        self.transport.assert_called_once()

    def test_worker_routes_qq_publish_and_never_republishes_duplicate(self):
        class Client:
            def complete_with_tools(client, messages, definitions, execute, max_rounds, tool_choice="auto"):
                client.choice = tool_choice
                client.messages = messages
                client.receipt = execute("qq_publish_shuoshuo", {"text": "model rewrite", "confirm": True})
                return SimpleNamespace(content="发布成功", tool_calls=1, message_sent=False)

        with tempfile.TemporaryDirectory() as folder:
            runtime = RuntimeSettings(database_path=str(Path(folder) / "test.sqlite3"), tool_calling_enabled=True, cooldown_seconds=0)
            settings = AppSettings(api_key="unused", runtime=runtime, deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
                                   scope=ScopeSettings(private_users=frozenset({"owner"})))
            client = Client()
            service = BotService(settings, client)
            try:
                worker = QQWorker(service, "123", qzone_config=self.config)
                worker.qzone.transport = self.transport
                request = {"message": {"kind": "c2c", "chat_id": "owner", "sender_id": "owner", "message_id": "1", "text": "[QQ说说]\n复制：周末好"}}
                self.assertTrue(worker.handle(request)["accepted"])
                self.assertTrue(worker.handle(request)["accepted"])
                self.assertEqual(client.choice["function"]["name"], "qq_publish_shuoshuo")
                self.assertTrue(client.receipt["success"])
                self.assertTrue(any("本轮 QQ 工具" in m["content"] for m in client.messages if m["role"] == "system"))
                self.transport.assert_called_once_with({"content": "周末好", "ugc_right": 4})
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()

import base64
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from social_bot.platforms.common.chat import SocialEvent, SocialResponder, SocialTools, PollLoop, platform_settings
from social_bot.platforms.common.media import SocialMediaReader
from social_bot.service import BotService
from social_bot.settings import AppSettings, RuntimeSettings, ScopeSettings, DeepSeekSettings, WeChatSettings

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class Client:
    def __init__(self):
        self.requests = []
        self.receipt = None

    def complete_with_tools(self, messages, definitions, execute, max_rounds, tool_choice="auto"):
        self.requests.append((messages, definitions, tool_choice))
        if isinstance(tool_choice, dict):
            self.receipt = execute(tool_choice["function"]["name"], {"question": "图里有什么"})
        return SimpleNamespace(content="收到 😊", tool_calls=int(isinstance(tool_choice, dict)), message_sent=False)

    def complete(self, messages):
        self.requests.append((messages, [], "plain"))
        return "收到 😊"


class SocialChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        runtime = RuntimeSettings(database_path=str(Path(self.temp.name) / "bot.sqlite3"), tool_calling_enabled=True,
                                   allow_weather_tool=True, allow_research_tool=True, allow_time_tool=True, cooldown_seconds=0)
        self.settings = AppSettings(api_key="test", runtime=runtime, scope=ScopeSettings(private_users=frozenset({"u1"}), group_activation_enabled=False),
                                    deepseek=DeepSeekSettings(), wechat=WeChatSettings())
        self.client = Client()
        self.service = BotService(self.settings, self.client)
        self.reader = Mock()
        self.reader.read.return_value = {"success": True, "results": [{"kind": "image", "text": "橘猫"}]}
        self.responder = SocialResponder(self.service, "bilibili", "123", config={"comments_enabled": True}, reader=self.reader)
        self.send = Mock()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def event(self, mid="one", **changes):
        return replace(SocialEvent("bilibili", "123", "private", mid, "u1", "你好", "u1"), **changes)

    def test_plain_chat_can_use_emoji_and_shared_tools_without_write_tools(self):
        self.assertTrue(self.responder.handle(self.event(), self.send)["sent"])
        self.assertIn("😊", self.send.call_args.args[1])
        names = {d["function"]["name"] for d in self.client.requests[0][1]}
        self.assertEqual(names, {"time_now", "weather_forecast", "research_search"})
        self.assertTrue(any("只读聊天工具" in row["content"] for row in self.client.requests[0][0] if row["role"] == "system"))

    def test_bound_media_is_forced_once_without_signed_urls_in_prompt(self):
        event = self.event(text="这是什么", media=({"kind": "image", "url": "https://i.hdslb.com/image?signature=private"},))
        self.responder.handle(event, self.send)
        self.assertEqual(self.client.requests[0][2]["function"]["name"], "chat_read_media")
        self.reader.read.assert_called_once()
        self.assertNotIn("signature=private", str(self.client.requests))
        tools = SocialTools(self.settings.runtime, "test", reader=self.reader, media=event.media)
        tools.execute("chat_read_media", {"question": "读图"})
        tools.execute("chat_read_media", {"question": "再读"})
        self.assertEqual(self.reader.read.call_count, 2)

    def test_comment_scopes_are_verified_and_do_not_leak_private_or_other_threads(self):
        self.responder.handle(self.event(text="私信里的秘密"), self.send)
        first = self.event("comment1", channel="comment", sender="u2", thread="123:comment:1", mentioned=True, text="第一楼")
        second = replace(first, id="comment2", thread="123:comment:2", text="第二楼")
        self.assertTrue(self.responder.handle(first, self.send)["sent"])
        self.assertTrue(self.responder.handle(second, self.send)["sent"])
        last = str(self.client.requests[-1][0])
        self.assertNotIn("私信里的秘密", last)
        self.assertNotIn("第一楼", last)
        self.assertFalse(self.service.scope.check(dict(chat_type="group", message_id="forged", chat_id="123:comment:1", is_mentioned=True, text="test")).allowed)

    def test_unmentioned_self_other_account_and_unlisted_sender_never_call_model(self):
        for event in [self.event(sender="stranger"), self.event(sender="123"), self.event(account="456"),
                      self.event(channel="comment", mentioned=False)]:
            self.assertFalse(self.responder.handle(event, self.send)["accepted"])
        self.assertEqual(self.client.requests, [])
        self.send.assert_not_called()

    def test_unknown_send_is_persisted_and_not_retried(self):
        self.send.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.responder.handle(self.event(), self.send)
        self.assertEqual(self.responder.db.execute("SELECT state FROM social_deliveries").fetchone()[0], "unknown")
        self.assertFalse(self.responder.handle(self.event(), self.send)["accepted"])
        self.send.assert_called_once()

    def test_disabling_after_generation_prevents_send(self):
        self.responder.permitted = Mock(side_effect=[True, False])
        self.assertEqual(self.responder.handle(self.event(), self.send)["reason"], "social_disabled_before_send")
        self.send.assert_not_called()

    def test_first_scan_is_baseline_later_events_are_once_and_bounded(self):
        source = Mock(return_value=[self.event("old")])
        loop = PollLoop(self.responder, source, self.send, max_replies=1)
        loop.step(["u1"])
        self.assertFalse(self.client.requests)
        source.return_value = [self.event("old"), self.event("new1"), self.event("new2")]
        loop.step(["u1"])
        self.assertEqual(self.send.call_count, 1)
        loop.step(["u1"])
        self.assertEqual(self.send.call_count, 2)
        loop.step(["u1"])
        self.assertEqual(self.send.call_count, 2)
        PollLoop(self.responder, source, self.send).step(["u1"])
        self.assertEqual(self.send.call_count, 2)

    def test_disabled_media_tools_return_honest_reply_without_model(self):
        self.service.settings = replace(self.settings, runtime=replace(self.settings.runtime, tool_calling_enabled=False))
        self.responder.handle(self.event(media=({"kind": "image", "url": "unused"},)), self.send)
        self.assertIn("已关闭", self.send.call_args.args[1])
        self.assertFalse(self.client.requests)

    def test_comment_video_tool_uses_only_bound_source_and_reuses_evidence(self):
        tools = SocialTools(self.settings.runtime, "这个视频在说什么", reader=self.reader,
                            source_video="https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertIn("chat_read_context_video", {d["function"]["name"] for d in tools.definitions})
        tools.execute("chat_read_context_video", {"question": "概述", "url": "https://evil.com"})
        tools.execute("chat_read_context_video", {"question": "再次"})
        self.reader.read.assert_called_once()
        self.assertEqual(self.reader.read.call_args.args[0][0]["url"], "https://www.bilibili.com/video/BV1xx411c7mD")

    def test_platform_defaults_enable_shared_tools_and_validate_switches(self):
        settings = platform_settings(self.settings, "douyin", {"peers": ["朋友"]}, Path(self.temp.name) / "other.sqlite3")
        self.assertTrue(settings.runtime.allow_weather_tool)
        self.assertTrue(settings.runtime.allow_research_tool)
        self.assertTrue(settings.runtime.emoji_enabled)
        self.assertFalse(settings.runtime.emoji_stickers_enabled)
        with self.assertRaises(ValueError):
            platform_settings(self.settings, "douyin", {"image_reading_enabled": "true"}, "unused")

    def test_enabled_video_capability_is_described_even_during_a_plain_question(self):
        settings=platform_settings(self.settings,'bilibili',dict(enabled=True,video_social=dict(enabled=True)),'unused')
        self.assertIn('当前已启用聊天触发的后台刷视频能力',settings.deepseek.system_prompt)
        self.assertIn('video_browse_share',settings.deepseek.system_prompt)
        self.assertIn('video_browse_watch',settings.deepseek.system_prompt)
        self.assertIn('不监视评论',settings.deepseek.system_prompt)
        self.assertIn('本轮未展示启动工具不代表没有这项能力',settings.deepseek.system_prompt)
        disabled=platform_settings(self.settings,'bilibili',dict(enabled=True,video_social=dict(enabled=False)),'unused')
        self.assertIn('当前平台未启用后台刷视频',disabled.deepseek.system_prompt)


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SOCIAL_BOT_CACHE_DIR": str(Path(self.temp.name) / "cache")})
        self.env.start()
        self.settings = AppSettings(api_key="test", runtime=RuntimeSettings(), scope=ScopeSettings(), deepseek=DeepSeekSettings(), wechat=WeChatSettings())
        self.vision, self.video, self.online = Mock(return_value="橘猫"), Mock(), Mock()
        self.reader = SocialMediaReader(self.settings, "douyin", vision=self.vision, video=self.video, online=self.online)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_inline_and_dom_images_are_actually_given_to_vision(self):
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        def analyze(path, question):
            self.assertEqual(Path(path).read_bytes(), PNG)
            return "橘猫"
        self.vision.side_effect = analyze
        result = self.reader.read([{"kind": "image", "url": "dom-image:test"}], resolve_inline=lambda _: url)
        self.assertTrue(result["success"])
        self.assertEqual(result["results"][0]["text"], "橘猫")

    def test_foreign_private_hosts_and_redirects_are_refused(self):
        for url in ["http://127.0.0.1/secret", "https://evil.com/image", "file:///C:/secret"]:
            self.assertFalse(self.reader.read([{"kind": "image", "url": url}])["success"])
        with patch("social_bot.platforms.common.media.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            self.assertFalse(self.reader.read([{"kind": "image", "url": "https://p.douyinpic.com/a"}])["success"])
        self.vision.assert_not_called()

    def test_bad_image_and_missing_models_do_not_invent_evidence(self):
        url = "data:image/png;base64," + base64.b64encode(b"not an image").decode()
        self.assertFalse(self.reader.read([{"kind": "image", "url": url}])["success"])
        self.reader.image_enabled = False
        self.assertFalse(self.reader.read([{"kind": "image", "url": url}])["success"])
        self.assertFalse(self.reader.read(["bad item"])["success"])
        self.vision.assert_not_called()

    def test_image_vision_failure_retains_a_sanitized_specific_reason(self):
        url='data:image/png;base64,'+base64.b64encode(PNG).decode()
        self.vision.side_effect=RuntimeError('vision_network_error: private-url?token=secret')
        result=self.reader.read([dict(kind='image',url=url)])
        self.assertEqual(result['results'][0]['reason'],'vision_network_error')
        self.assertNotIn('secret',str(result))

    def test_platform_video_uses_existing_analyzer_and_preserves_partial_coverage(self):
        self.online.analyze.return_value = {"success": True, "text": "人物从左侧走入", "partial": True, "coverage_start_seconds": 0,
                                           "coverage_end_seconds": 180, "note": "仅前三分钟", "raw_private_url": "secret"}
        result = self.reader.read([{"kind": "video", "url": "https://www.douyin.com/video/123456"}])
        self.assertTrue(result["success"])
        self.assertTrue(result["results"][0]["partial"])
        self.assertNotIn("secret", str(result))
        self.online.analyze.side_effect = RuntimeError("credential-bearing URL")
        result = self.reader.read([{"kind": "video", "url": "https://www.douyin.com/video/123456"}])
        self.assertFalse(result["success"])
        self.assertNotIn("credential-bearing", str(result))


if __name__ == "__main__":
    unittest.main()

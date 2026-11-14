import io
import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

from social_bot.platforms.bilibili.social import BilibiliSocialClient, BilibiliSource, private_event, mention_event
from social_bot.platforms.bilibili.client import BilibiliError


class BilibiliSocialTests(unittest.TestCase):
    def test_private_messages_images_and_shared_video_normalize_and_filter_own(self):
        row = dict(msg_key=123, msg_seqno=2, sender_uid=456, receiver_id=100, receiver_type=1, msg_type=1, content=json.dumps({"content": "你好 😊"}))
        self.assertEqual(private_event(row, "100", "456").text, "你好 😊")
        image = private_event({**row, "msg_type": 2, "content": json.dumps({"url": "https://i.hdslb.com/a"})}, "100", "456")
        self.assertEqual(image.media[0]["kind"], "image")
        video = private_event({**row, "msg_type": 7, "content": json.dumps({"bvid": "BV1xx411c7mD"})}, "100", "456")
        self.assertEqual(video.media[0]["kind"], "video")
        self.assertIsNone(private_event({**row, "sender_uid": 100}, "100", "456"))
        self.assertIsNone(private_event({**row, "msg_type": 5}, "100", "456"))

    def notification(self):
        return dict(id=999, user={"mid": 456, "nickname": "朋友"}, item={"type": "reply", "business_id": 1,
                    "subject_id": 123, "root_id": 555, "source_id": 666, "target_id": 777, "source_content": "@助手 请问今天的天气"})

    def test_at_comment_uses_source_id_as_parent_and_preserves_root(self):
        event = mention_event(self.notification(), "100")
        self.assertTrue(event.mentioned)
        self.assertEqual(event.target, {"oid": "123", "type": 1, "root": "555", "parent": "666"})
        self.assertIsNone(mention_event({**self.notification(), "user": {"mid": 100}}, "100"))
        data = self.notification()
        data["item"]["type"] = "video"
        self.assertIsNone(mention_event(data, "100"))

    def test_comment_video_source_is_bound_without_tracking_query(self):
        row = self.notification()
        row["item"]["uri"] = "https://www.bilibili.com/video/BV1xx411c7mD?tracking=private"
        self.assertEqual(mention_event(row, "100").source_video, "https://www.bilibili.com/video/BV1xx411c7mD")

    def test_reply_confirms_original_comment_and_does_not_fall_back_to_top_level(self):
        client = BilibiliSocialClient("sess", "csrf")
        client.api = Mock(return_value={"replies": [{"rpid": 666, "root": 555}]})
        client._social = Mock(return_value={"rpid": 888})
        target = mention_event(self.notification(), "100").target
        client.reply_comment(target, "今天晴朗 😊")
        payload = client._social.call_args.kwargs["data"]
        self.assertEqual(payload["parent"], "666")
        self.assertEqual(payload["root"], "555")
        client.api.return_value = {"replies": []}
        client._social.reset_mock()
        with self.assertRaises(BilibiliError):
            client.reply_comment(target, "不应发送")
        client._social.assert_not_called()

    def test_write_request_contains_csrf_and_utf8_emoji_and_is_once(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps({"code": 0, "data": {"msg_key": 123}}).encode())
        client = BilibiliSocialClient("sess-test", "csrf-test", opener=opener)
        client._signed = Mock(side_effect=lambda params: params)
        client.send_private("100", "456", "收到 😊")
        request = opener.open.call_args.args[0]
        self.assertEqual(urlsplit(request.full_url).hostname, "api.vc.bilibili.com")
        form = parse_qs(request.data.decode())
        self.assertEqual(form["csrf"], ["csrf-test"])
        self.assertEqual(json.loads(form["msg[content]"][0])["content"], "收到 😊")
        self.assertIn("bili_jct=csrf-test", request.get_header("Cookie"))
        opener.open.assert_called_once()

    def test_network_failure_is_sanitized_and_never_retried(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError("secret-cookie")
        client = BilibiliSocialClient("sess-test", "csrf-test", opener=opener)
        client._signed = Mock(side_effect=lambda params: params)
        with self.assertRaisesRegex(BilibiliError, "NETWORK_ERROR"):
            client.send_private("100", "456", "你好")
        opener.open.assert_called_once()

    def test_missing_csrf_prevents_post(self):
        client = BilibiliSocialClient("sess-test", "")
        client.opener = Mock()
        with self.assertRaises(BilibiliError):
            client.send_private("100", "456", "你好")
        client.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()

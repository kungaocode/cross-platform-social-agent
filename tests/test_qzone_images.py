import base64
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from social_bot.platforms.qq.qzone import QzonePublisher, match_publish_command
from social_bot.platforms.qq.worker import QQWorker
from social_bot.service import BotService
from social_bot.settings import AppSettings, RuntimeSettings, ScopeSettings, DeepSeekSettings, WeChatSettings


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class QzoneImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"TEST_QZONE_TOKEN": "test-token", "SOCIAL_BOT_CACHE_DIR": str(Path(self.temp.name) / "cache")})
        self.env.start()
        self.db = sqlite3.connect(":memory:")
        self.config = dict(enabled=True, owner_openids=["owner"], base_url="http://127.0.0.1:3000", token_env="TEST_QZONE_TOKEN", ugc_right=4)
        self.transport = Mock(return_value={"status": "ok", "retcode": 0, "data": {"tid": "image-post"}})
        self.downloads = []

        def download(url, folder, **kwargs):
            self.assertTrue(kwargs["image"])
            self.downloads.append(url)
            path = Path(folder) / "image.png"
            path.write_bytes(PNG)
            return path

        self.downloader = Mock(side_effect=download)
        self.publisher = QzonePublisher(self.db, self.config, transport=self.transport, image_downloader=self.downloader)
        self.attachments = [{"content_type": "image/png", "url": f"https://gchat.qpic.cn/photo{i}?signature=test"} for i in range(3)]

    def tearDown(self):
        self.db.close()
        self.env.stop()
        self.temp.cleanup()

    def publish(self, **kwargs):
        return self.publisher.publish("event", "owner", "c2c", match_publish_command("[QQ说说]\n复制：周末好"),
                                      {"text": "不应替换原文", "confirm": True, "images": ["file:///untrusted.png"]},
                                      attachments=kwargs.pop("attachments", self.attachments), **kwargs)

    def test_all_images_preserve_order_and_use_cross_machine_base64(self):
        result = self.publish()
        self.assertTrue(result["success"])
        self.assertEqual(result["image_count"], 3)
        payload = self.transport.call_args.args[0]
        self.assertEqual(payload["content"], "周末好")
        self.assertEqual(self.downloads, [a["url"] for a in self.attachments])
        self.assertEqual([base64.b64decode(image.removeprefix("base64://")) for image in payload["images"]], [PNG] * 3)
        receipt = self.db.execute("SELECT receipt FROM qq_qzone_publications").fetchone()[0]
        self.assertNotIn("signature", receipt)
        self.assertNotIn("base64://", receipt)
        self.assertTrue(self.publish()["cached"])
        self.transport.assert_called_once()
        self.assertEqual(self.downloader.call_count, 3)

    def test_partial_download_failure_stops_entire_post_without_text_fallback(self):
        successful_download = self.downloader.side_effect
        def fail_second_image(url, folder, **kwargs):
            if "photo0" in url:
                return successful_download(url, folder, **kwargs)
            raise TimeoutError("sensitive signature")
        self.downloader.side_effect = fail_second_image
        result = self.publish()
        self.assertEqual(result["reason"], "qzone_image_prepare_failed")
        self.assertFalse(result["side_effect_performed"])
        self.assertNotIn("sensitive", json.dumps(result))
        self.assertEqual(self.publish(attachments=[])["reason"], "qzone_publish_failed_no_retry")
        self.transport.assert_not_called()

    def test_rejects_non_images_and_too_many_images_before_download(self):
        for attachments in [[{"content_type": "video/mp4", "url": "https://a.qq.com/video.mp4"}],
                            self.attachments + [{"content_type": "application/pdf"}], self.attachments * 4]:
            self.assertEqual(self.publish(attachments=attachments)["reason"], "qzone_images_only_max_9")
        self.downloader.assert_not_called()
        self.transport.assert_not_called()

    def test_unauthorized_and_unconfigured_requests_do_not_download(self):
        self.config["enabled"] = False
        self.assertFalse(self.publish()["success"])
        self.config["enabled"] = True
        self.config["base_url"] = ""
        self.assertEqual(self.publish()["reason"], "qzone_service_not_configured")
        self.downloader.assert_not_called()

    def test_size_limits_stop_publish_before_side_effect(self):
        self.publisher.MAX_TOTAL_IMAGE_BYTES = len(PNG) * 2
        self.assertEqual(self.publish()["reason"], "qzone_image_prepare_failed")
        self.transport.assert_not_called()

    def test_untrusted_attachment_url_is_rejected_by_actual_downloader(self):
        self.publisher = QzonePublisher(self.db, self.config, transport=self.transport)
        with patch("social_bot.platforms.qq.media.build_opener") as opener:
            result = self.publish(attachments=[{"content_type": "image/png", "url": "http://127.0.0.1/private"}])
        self.assertEqual(result["reason"], "qzone_image_prepare_failed")
        opener.assert_not_called()
        self.transport.assert_not_called()

    def test_unknown_image_post_is_not_reuploaded_or_republished(self):
        self.transport.side_effect = TimeoutError()
        self.assertEqual(self.publish()["reason"], "qzone_result_unknown_no_retry")
        self.assertEqual(self.publish()["reason"], "qzone_result_unknown_no_retry")
        self.transport.assert_called_once()
        self.assertEqual(self.downloader.call_count, 3)

    def test_real_http_transport_delivers_images_and_auth_to_local_test_server(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                body = handler.rfile.read(int(handler.headers["Content-Length"]))
                received.append((handler.path, handler.headers["Authorization"], json.loads(body)))
                data = json.dumps({"status": "ok", "retcode": 0, "data": {"tid": "local-test-post"}}).encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
        thread.start()
        try:
            self.config["base_url"] = f"http://127.0.0.1:{server.server_port}"
            self.publisher = QzonePublisher(self.db, self.config, image_downloader=self.downloader)
            self.assertEqual(self.publish()["tid"], "local-test-post")
            self.assertTrue(self.publish()["cached"])
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0][:2], ("/send_qzone_msg", "Bearer test-token"))
            self.assertEqual(len(received[0][2]["images"]), 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_worker_copy_and_generate_forward_images_without_vision_or_history_urls(self):
        class Client:
            def complete_with_tools(client, messages, definitions, execute, max_rounds, tool_choice="auto"):
                client.messages = messages
                client.result = execute("qq_publish_shuoshuo", {"text": "生成的周末问候", "confirm": True})
                return SimpleNamespace(content="发布完成", tool_calls=1, message_sent=False)

        runtime = RuntimeSettings(database_path=str(Path(self.temp.name) / "worker.sqlite3"), tool_calling_enabled=True, cooldown_seconds=0)
        settings = AppSettings(api_key="test-only", runtime=runtime, deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
                               scope=ScopeSettings(private_users=frozenset({"owner", "other"})))
        client = Client()
        service = BotService(settings, client)
        reader = Mock(image_enabled=False)
        try:
            worker = QQWorker(service, "123", media_reader=reader, qzone_config=self.config)
            worker.qzone.transport, worker.qzone.image_downloader = self.transport, self.downloader
            for index, mode in enumerate(["复制", "生成"]):
                request = {"message": dict(kind="c2c", chat_id="owner", sender_id="owner", message_id=str(index),
                                           text=f"[QQ说说]\n{mode}：周末愉快", attachments=self.attachments)}
                self.assertTrue(worker.handle(request)["accepted"])
                self.assertTrue(client.result["success"])
                self.assertEqual(client.result["image_count"], 3)
                self.assertNotIn("signature=test", str(client.messages))
                self.assertNotIn("base64://", str(client.messages))
                payload = self.transport.call_args.args[0]
                self.assertEqual(payload["content"], "周末愉快" if mode == "复制" else "生成的周末问候")
            self.assertEqual(worker.handle({"message": dict(kind="c2c", chat_id="other", sender_id="other", message_id="denied",
                             text="[QQ说说]\n复制：test", attachments=self.attachments)})["reason"], "qzone_unavailable")
            reader.read.assert_not_called()
            self.assertEqual(self.transport.call_count, 2)
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()

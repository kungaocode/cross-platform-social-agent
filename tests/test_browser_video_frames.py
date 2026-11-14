import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import Mock, patch

from social_bot.browser_video_frames import BrowserFrameAnalyzer
from social_bot.modules.video import VideoError
from social_bot.video_browser import VideoAgentTools
from social_bot.store import ConversationStore

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


class BrowserFrameTests(unittest.TestCase):
    def browser(self, times=(10, 12, 14)):
        page = Mock()
        page.title.return_value = "二创片段"
        page.get_by_text.return_value.filter.return_value.count.return_value = 0
        players = page.locator.return_value.filter.return_value
        players.count.return_value = 1
        video = players.nth.return_value
        video.bounding_box.return_value = dict(width=640, height=360)
        video.evaluate.side_effect = [dict(paused=False, time=t, ready=4, width=640,
                                          source="https://media.invalid/clip?private-signature", duration=100) for t in times]
        video.screenshot.side_effect = lambda path: Path(path).write_bytes(b"fixture-image")
        browser = SimpleNamespace(page=page, current=URL, platform="bilibili", observe=Mock(return_value=dict(blocked=False)))
        return browser

    def test_sparse_playing_frames_are_visual_only_and_do_not_export_media_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            @contextmanager
            def cache(*args):
                yield folder
            vision = Mock(return_value="可见一只戴帽子的猫。")
            vision.model = "fixture-vision"
            browser = self.browser()
            with patch("social_bot.browser_video_frames.cache_task", cache):
                result = BrowserFrameAnalyzer(vision).analyze(browser)
            self.assertEqual(result["evidence"], "browser_frames")
            self.assertFalse(result["has_audio"])
            self.assertTrue(result["partial"])
            self.assertEqual(result["sampled_frame_seconds"], [10, 12, 14])
            self.assertIn("没有音频", result["text"])
            self.assertNotIn("private-signature", str(result))
            self.assertEqual(vision.call_count, 3)

    def test_stalled_or_verification_pages_are_not_sent_to_the_model(self):
        vision = Mock()
        browser = self.browser(times=(10, 10, 10))
        with tempfile.TemporaryDirectory() as folder:
            @contextmanager
            def cache(*args):
                yield folder
            with patch("social_bot.browser_video_frames.cache_task", cache):
                with self.assertRaisesRegex(VideoError, "not_advancing"):
                    BrowserFrameAnalyzer(vision).analyze(browser)
        vision.assert_not_called()
        browser.observe.return_value = dict(blocked=True)
        with self.assertRaisesRegex(VideoError, "verification_required"):
            BrowserFrameAnalyzer(vision).analyze(browser)
        vision.assert_not_called()

    def test_download_error_can_use_visible_frames_and_persist_sourced_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder) / "videos.sqlite3")
            try:
                browser = self.browser()
                browser.max_videos = 1
                analyzer = Mock()
                analyzer.analyze.side_effect = VideoError("online_video_http_412")
                frames = Mock()
                frames.analyze.return_value = dict(success=True, evidence="browser_frames", has_audio=False,
                    text="仅观察两帧，没有音频。画面里有一只猫。", coverage_start_seconds=10, coverage_end_seconds=14, partial=True)
                tools = VideoAgentTools(browser, analyzer, store, frame_analyzer=frames)
                result = tools.execute("video_analyze", {})
                self.assertTrue(result["success"])
                self.assertFalse(result["has_audio"])
                self.assertEqual(result["download_reason"], "online_video_http_412")
                row = store.connection.execute("SELECT state,evidence FROM video_observations").fetchone()
                self.assertEqual(row[0], "done")
                self.assertIn("browser_frames", row[1])
                browser.max_videos = 2
                browser.current = "https://www.bilibili.com/video/BV1yy411c7mD"
                self.assertTrue(tools.execute("video_analyze", {})["success"])
                analyzer.analyze.assert_called_once()
                self.assertEqual(frames.analyze.call_count, 2)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

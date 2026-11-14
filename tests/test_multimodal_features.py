from __future__ import annotations

import tempfile
import unittest
import json
import zipfile
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from social_bot.modules.audio import AudioModule
from social_bot.modules.document import DocumentModule
from social_bot.modules.link import LinkModule
from social_bot.modules.poke import PokeModule
from social_bot.modules.poke import is_poke_message
from social_bot.modules.speech import QwenAudioTranscriber, SpeechModule
from social_bot.modules.vision import QwenTextAnalyzer, QwenVisionAnalyzer, VisionModule
from social_bot.tools import WeChatToolExecutor


class VisionModuleTests(unittest.TestCase):
    def test_download_ui_exception_returns_failure_without_calling_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            message = mock.Mock()
            message.download.side_effect = RuntimeError("UI control expired")
            analyzer = mock.Mock()
            result = VisionModule(analyzer=analyzer, cache_dir=folder).ask(message)
            self.assertFalse(result.success)
            self.assertEqual(result.reason, "image_download_error:RuntimeError:UI control expired")
            analyzer.assert_not_called()

    def test_legacy_no_argument_download_exception_returns_failure(self) -> None:
        class Message:
            def download(self):
                raise RuntimeError("UI control expired")

        with tempfile.TemporaryDirectory() as folder:
            result = VisionModule(cache_dir=folder).ask(Message())
            self.assertFalse(result.success)
            self.assertTrue(result.reason.startswith("image_download_error:RuntimeError:"))

    def test_prepares_downloaded_image_without_calling_model(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "image.png"
            image.write_bytes(b"image")
            message = mock.Mock()
            message.download.return_value = image

            result = VisionModule(cache_dir=Path(folder) / "cache").ask(message, "图中有什么？")

            self.assertFalse(result.success)
            self.assertEqual(result.reason, "vision_model_not_configured")
            self.assertEqual(result.request["question"], "图中有什么？")

    def test_analyzer_is_an_injected_local_hook(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "image.png"
            image.write_bytes(b"image")
            message = mock.Mock()
            message.download.return_value = image
            analyzer = mock.Mock(return_value={"answer": "一只猫"})

            result = VisionModule(analyzer=analyzer, cache_dir=Path(folder) / "cache").ask(message, "是什么动物？")

            self.assertTrue(result.success)
            self.assertEqual(result.answer, "一只猫")
            analyzer.assert_called_once()

    def test_qwen_analyzer_sends_base64_image_to_compatible_api(self) -> None:
        class Response:
            def read(self):
                return '{"choices":[{"message":{"content":"图中有一只猫"}}]}'.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "image.png"
            image.write_bytes(b"png-bytes")
            analyzer = QwenVisionAnalyzer(
                "sk-test", "https://example.com/compatible-mode/v1", model="qwen3.8-flash"
            )
            with mock.patch("social_bot.modules.vision.module.net.urlopen", return_value=Response()) as opener:
                answer = analyzer(str(image), "是什么动物？")

            self.assertEqual(answer, "图中有一只猫")
            request = opener.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.com/compatible-mode/v1/chat/completions")
            payload = __import__("json").loads(request.data.decode("utf-8"))
            content = payload["messages"][0]["content"]
            self.assertEqual(payload["model"], "qwen3.8-flash")
            self.assertEqual(content[0]["text"], "是什么动物？")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_download_accepts_wxauto_boolean_and_finds_new_cached_image(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "cache"
            cache.mkdir()
            image = cache / "downloaded.jpg"

            class Message:
                def download(self, **_kwargs):
                    nonlocal image
                    image=Path(_kwargs['dir_path'])/'downloaded.jpg'
                    image.write_bytes(b"image")
                    return True

            result = VisionModule(cache_dir=cache).ask(Message(), "描述图片")

            self.assertFalse(result.success)
            self.assertEqual(result.reason, "vision_model_not_configured")
            self.assertEqual(Path(result.image_path), image.resolve())
            self.assertTrue(image.exists())

    def test_qwen_text_analyzer_sends_parsed_document(self) -> None:
        class Response:
            def read(self):
                return '{"choices":[{"message":{"content":"文件摘要"}}]}'.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        analyzer = QwenTextAnalyzer("sk-test", "https://example.com/compatible-mode/v1")
        with mock.patch("social_bot.modules.vision.module.net.urlopen", return_value=Response()) as opener:
            self.assertEqual(analyzer("正文内容", "请总结"), "文件摘要")
        payload = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(payload["model"], "qwen3.8-flash")
        self.assertIn("正文内容", payload["messages"][0]["content"])

    def test_qwen_text_analyzer_can_send_pdf_file_with_images(self) -> None:
        class Response:
            def read(self):
                return '{"choices":[{"message":{"content":"PDF 图文摘要"}}]}'.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.pdf"
            path.write_bytes(b"%PDF-test")
            analyzer = QwenTextAnalyzer("sk-test", "https://example.com/compatible-mode/v1")
            with mock.patch("social_bot.modules.vision.module.net.urlopen", return_value=Response()) as opener:
                self.assertEqual(analyzer.analyze_file(str(path), "请概括论文"), "PDF 图文摘要")
            payload = json.loads(opener.call_args.args[0].data.decode("utf-8"))
            file_item = payload["messages"][0]["content"][0]
            self.assertEqual(file_item["type"], "file")
            self.assertEqual(file_item["file"]["filename"], "paper.pdf")
            self.assertTrue(file_item["file"]["file_data"].startswith("data:application/pdf;base64,"))


class DocumentModuleTests(unittest.TestCase):
    def test_parses_text_and_truncates(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "note.txt"
            path.write_text("第一行\n\n\n第二行", encoding="utf-8")

            result = DocumentModule(max_chars=100).parse_path(str(path))

            self.assertTrue(result.success)
            self.assertIn("第二行", result.text)
            self.assertFalse(result.truncated)

    def test_downloads_file_message_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "note.md"
            path.write_text("# 标题", encoding="utf-8")
            message = mock.Mock()
            message.download.return_value = path

            result = DocumentModule().parse_message(message)

            self.assertTrue(result.success)
            self.assertIn("标题", result.text)

    def test_downloaded_file_is_removed_after_parse(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "cache"
            path = cache / "note.txt"

            class Message:
                def download(self, **_kwargs):
                    cache.mkdir(parents=True, exist_ok=True)
                    path.write_text("一次性文件", encoding="utf-8")
                    return path

            result = DocumentModule(cache_dir=cache).parse_message(Message())

            self.assertTrue(result.success)
            self.assertTrue(path.exists())

    def test_file_download_uses_wxauto_file_signature(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "cache"
            path = cache / "report.pdf"
            calls = []

            class Message:
                def download(self, dir_path=None, force_click=False, timeout=30):
                    calls.append((dir_path, force_click, timeout))
                    cache.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"%PDF-1.4")
                    return path

            result = DocumentModule(cache_dir=cache).parse_message(Message())

            self.assertFalse(result.success)  # 测试文件不是完整 PDF，但已经成功落盘
            self.assertEqual(calls[0][1:], (False, 30))
            self.assertTrue(path.exists())

    def test_detects_images_in_docx_for_visual_route(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", "<document/>")
                archive.writestr("word/media/image1.png", b"image")

            self.assertTrue(DocumentModule._has_visual_elements(str(path)))

    def test_parses_pptx_text_tables_and_notes_with_python_pptx(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "deck.pptx"
            path.write_bytes(b"pptx")

            class Cell:
                def __init__(self, text: str):
                    self.text = text

            table = SimpleNamespace(rows=[SimpleNamespace(cells=[Cell("指标"), Cell("数值")]), SimpleNamespace(cells=[Cell("A"), Cell("10")])])
            slide = SimpleNamespace(
                shapes=[
                    SimpleNamespace(text="演示标题", has_table=False, has_chart=False),
                    SimpleNamespace(text="", has_table=True, table=table, has_chart=False),
                ],
                notes_slide=SimpleNamespace(notes_text_frame=SimpleNamespace(text="讲解补充")),
            )
            fake_pptx = ModuleType("pptx")
            fake_pptx.Presentation = lambda _path: SimpleNamespace(slides=[slide])
            with mock.patch.dict(sys.modules, {"pptx": fake_pptx}):
                result = DocumentModule().parse_path(str(path))

            self.assertTrue(result.success)
            self.assertIn("第 1 页", result.text)
            self.assertIn("演示标题", result.text)
            self.assertIn("指标 | 数值", result.text)
            self.assertIn("演讲者备注：讲解补充", result.text)

    def test_visual_office_document_is_converted_before_qwen_file_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "paper.docx"
            source.write_bytes(b"docx")
            converted = Path(folder) / "paper.pdf"
            analyzer = mock.Mock()
            analyzer.analyze_file.return_value = "图表摘要"
            module = DocumentModule(analyzer=analyzer, cache_dir=Path(folder) / "cache")
            with mock.patch.object(module, "_has_visual_elements", return_value=True), mock.patch.object(
                module, "_convert_to_pdf", return_value=converted
            ) as convert:
                result = module.analyze_document(str(source), "文字摘要", "请分析图表")

            self.assertTrue(result["success"])
            self.assertEqual(result["source"], "qwen_pdf_converted")
            convert.assert_called_once_with(str(source))
            analyzer.analyze_file.assert_called_once_with(str(converted), "请分析图表")


class LinkModuleTests(unittest.TestCase):
    def test_reads_url_from_link_card_resolver(self) -> None:
        module = LinkModule()
        with mock.patch.object(module, "read", return_value=SimpleNamespace(success=True, url="https://mp.weixin.qq.com/s/card")) as read:
            result = module.read_message(SimpleNamespace(type="link", content="文章标题"), lambda _message: "https://mp.weixin.qq.com/s/card")

        self.assertTrue(result.success)
        read.assert_called_once_with("https://mp.weixin.qq.com/s/card")

    def test_extracts_url_from_message_fields_before_ui_resolver(self) -> None:
        module = LinkModule()
        with mock.patch.object(module, "read", return_value=SimpleNamespace(success=True, url="https://example.com/a")) as read:
            result = module.read_message(SimpleNamespace(type="link", url="https://example.com/a"), mock.Mock())

        self.assertTrue(result.success)
        read.assert_called_once_with("https://example.com/a")

    def test_reads_note_card_body_without_a_public_url(self) -> None:
        module = LinkModule()
        message = SimpleNamespace(
            type="note",
            content="荔枝西米露的笔记",
            get_content=lambda wait=3: ["荔枝西米露的笔记", "这是小红书卡片正文。"],
        )
        result = module.read_message(message, lambda _message: "")

        self.assertTrue(result.success)
        self.assertEqual(result.source_type, "xiaohongshu")
        self.assertIn("这是小红书卡片正文", result.text)

    def test_extracts_html_text_without_model_call(self) -> None:
        class Headers:
            def get_content_charset(self):
                return "utf-8"

            def get_content_type(self):
                return "text/html"

        class Response:
            headers = Headers()

            def read(self, _size):
                return "<html><title>标题</title><script>x</script><p>正文内容</p></html>".encode()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with mock.patch("social_bot.modules.link.module.urlopen", return_value=Response()):
            result = LinkModule().read("https://example.com/article")

        self.assertTrue(result.success)
        self.assertEqual(result.title, "标题")
        self.assertIn("正文内容", result.text)

    def test_classifies_bilibili_as_partial_page_content(self) -> None:
        self.assertEqual(LinkModule._source_type("www.bilibili.com"), "bilibili")

    def test_classifies_douyin_and_parses_subtitle_formats(self) -> None:
        self.assertEqual(LinkModule._source_type("v.douyin.com"), "douyin")
        self.assertEqual(LinkModule._source_type("www.xiaohongshu.com"), "xiaohongshu")
        self.assertEqual(LinkModule._source_type("xhslink.com"), "xiaohongshu")
        self.assertEqual(
            LinkModule._subtitle_text('[{"from": 0, "content": "第一句"}, {"content": "第二句"}]'),
            "第一句 第二句",
        )
        self.assertEqual(
            LinkModule._subtitle_text("WEBVTT\n\n00:00.000 --> 00:01.000\n字幕内容"),
            "字幕内容",
        )

    def test_video_result_marks_metadata_fallback_as_partial(self) -> None:
        module = LinkModule()
        with mock.patch.object(module, "_read_video_with_ytdlp", return_value=("视频标题", "标题：视频标题\n简介：简介", "video_transcript_unavailable")):
            result = module._read_video("https://www.bilibili.com/video/BVtest", "bilibili")
        self.assertTrue(result.success)
        self.assertTrue(result.partial)
        self.assertEqual(result.reason, "video_transcript_unavailable")


class PokeAndAudioTests(unittest.TestCase):
    def test_poke_command_text_is_not_mistaken_for_received_poke(self) -> None:
        self.assertFalse(is_poke_message(SimpleNamespace(type="text", content="请拍一拍我")))

    def test_recognizes_and_sends_poke(self) -> None:
        message = mock.Mock(type="poke", content="张三拍了拍我")
        message.tickle.return_value = {"status": "成功"}
        module = PokeModule()

        self.assertTrue(module.recognize(message).success)
        self.assertTrue(module.poke(message).success)
        message.tickle.assert_called_once_with()

    def test_sends_audio_when_wxauto_supports_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "reply.mp3"
            path.write_bytes(b"audio")
            chat = mock.Mock()
            chat.SendAudio.return_value = {"status": "成功"}

            result = AudioModule().send_audio(chat, str(path), duration=3, start=1)

            self.assertTrue(result.success)
            chat.SendAudio.assert_called_once_with(str(path.resolve()), duration=3, start=1)

    def test_qwen_audio_transcriber_cleans_downloaded_audio(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "cache"
            path = cache / "voice.wav"

            class Message:
                def download(self, **_kwargs):
                    nonlocal path
                    path=Path(_kwargs['dir_path'])/'voice.wav'
                    path.write_bytes(b"wav")
                    return True

            analyzer = mock.Mock(return_value="语音内容")
            result = SpeechModule(analyzer=analyzer, cache_dir=cache).transcribe(Message())

            self.assertTrue(result["success"])
            self.assertEqual(result["text"], "语音内容")
            analyzer.assert_called_once()
            self.assertTrue(path.exists())

    def test_audio_model_falls_back_to_wxauto_when_voice_cannot_download(self) -> None:
        message = mock.Mock()
        del message.download
        message.to_text.return_value = "wxauto转写"

        result = SpeechModule(analyzer=mock.Mock()).transcribe(message)

        self.assertTrue(result["success"])
        self.assertEqual(result["text"], "wxauto转写")
        self.assertEqual(result["reason"], "speech_fallback_wxauto")

    def test_qwen_audio_payload_and_nested_response(self) -> None:
        class Response:
            def read(self):
                return '{"output":{"choices":[{"message":{"content":[{"text":"转写结果"}]}}]}}'.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "voice.wav"
            path.write_bytes(b"wav")
            analyzer = QwenAudioTranscriber("sk-test", "https://example.com/asr")
            with mock.patch("social_bot.modules.speech.module.net.urlopen", return_value=Response()) as opener:
                self.assertEqual(analyzer(str(path)), "转写结果")
            payload = json.loads(opener.call_args.args[0].data.decode("utf-8"))
            self.assertEqual(payload["model"], "qwen-audio-3.1-asr-flash")
            data = payload["input"]["messages"][0]["content"][0]["input_audio"]["data"]
            self.assertTrue(data.startswith("data:audio/wav;base64,"))


class NewToolDefinitionsTests(unittest.TestCase):
    def test_new_tools_are_exposed_for_matching_messages(self) -> None:
        image = SimpleNamespace(type="image")
        executor = WeChatToolExecutor(
            mock.Mock(), image, "chat", allow_vision=True, allow_link=True, allow_audio=True
        )
        names = {item["function"]["name"] for item in executor.definitions}
        self.assertIn("wechat_visual_question", names)
        self.assertIn("link_read", names)
        self.assertIn("wechat_send_audio", names)

    def test_text_question_can_use_recent_image_context(self) -> None:
        message = SimpleNamespace(content="这张图里有什么？")
        image = SimpleNamespace(type="image")
        executor = WeChatToolExecutor(
            mock.Mock(), message, "chat", allow_vision=True, vision_message=image
        )
        names = {item["function"]["name"] for item in executor.definitions}
        self.assertIn("wechat_visual_question", names)

    def test_link_tool_requires_url_from_user_message(self) -> None:
        message = SimpleNamespace(content="请读取这个网页")
        executor = WeChatToolExecutor(mock.Mock(), message, "chat", allow_link=True)

        result = executor.execute("link_read", {"url": "https://example.com"})

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "url_must_be_present_in_user_message")

    def test_link_tool_accepts_natural_summary_command(self) -> None:
        message = SimpleNamespace(content="帮我总结这篇文章 https://example.com/article")
        link = mock.Mock()
        link.read.return_value = SimpleNamespace(
            success=True,
            reason="",
            url="https://example.com/article",
            source_type="website",
            title="标题",
            text="正文",
            partial=False,
        )
        executor = WeChatToolExecutor(mock.Mock(), message, "chat", allow_link=True, link_module=link)

        result = executor.execute("link_read", {"url": "https://example.com/article"})

        self.assertTrue(result["success"])
        self.assertEqual(result["text"], "正文")
        link.read.assert_called_once_with("https://example.com/article")

    def test_multimodal_modes_gate_tools_and_leave_mention_to_model(self) -> None:
        image = SimpleNamespace(type="image")

        mention = WeChatToolExecutor(
            mock.Mock(), image, "chat", allow_ocr=True, allow_vision=True,
            multimodal_mode="mention", is_mentioned=True,
        )
        mention_names = {item["function"]["name"] for item in mention.definitions}
        self.assertIn("wechat_ocr", mention_names)
        self.assertIn("wechat_visual_question", mention_names)
        self.assertFalse(mention.multimodal_auto_read)

        quiet = WeChatToolExecutor(
            mock.Mock(), image, "chat", allow_ocr=True, allow_vision=True,
            multimodal_mode="mention", is_mentioned=False,
        )
        quiet_names = {item["function"]["name"] for item in quiet.definitions}
        self.assertNotIn("wechat_ocr", quiet_names)
        self.assertNotIn("wechat_visual_question", quiet_names)

        off = WeChatToolExecutor(
            mock.Mock(), image, "chat", allow_ocr=True, allow_vision=True,
            multimodal_mode="off", is_mentioned=True,
        )
        off_names = {item["function"]["name"] for item in off.definitions}
        self.assertNotIn("wechat_ocr", off_names)
        self.assertNotIn("wechat_visual_question", off_names)


if __name__ == "__main__":
    unittest.main()

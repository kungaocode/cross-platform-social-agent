from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from social_bot.modules.ocr import OCRModule
from social_bot.modules.research import ResearchModule
from social_bot.modules.speech import SpeechModule
from social_bot.modules.weather import WeatherModule
from social_bot.tools import WeChatToolExecutor


class StubWeather(WeatherModule):
    def __init__(self, responses):
        super().__init__(timeout_seconds=1)
        self.responses = iter(responses)

    def _get_json(self, url):
        return next(self.responses)


class ExtraModuleTests(unittest.TestCase):
    def test_weather_uses_ip_then_forecast(self) -> None:
        weather = StubWeather(
            [
                {"city": "测试市", "region": "测试省", "country_name": "中国", "latitude": 30, "longitude": 120, "timezone": "Asia/Shanghai"},
                {
                    "current": {
                        "time": "2026-10-11T12:00",
                        "temperature_2m": 22,
                        "apparent_temperature": 23,
                        "relative_humidity_2m": 65,
                        "weather_code": 1,
                        "wind_speed_10m": 8,
                    },
                    "daily": {
                        "time": ["2026-10-11"],
                        "weather_code": [3],
                        "temperature_2m_max": [25],
                        "temperature_2m_min": [17],
                        "precipitation_probability_max": [20],
                    },
                },
            ]
        )

        result = weather.forecast(location="", days=1)

        self.assertTrue(result["success"])
        self.assertIn("Open-Meteo", result["provider"])
        self.assertEqual(result["location"]["city"], "测试市")
        self.assertEqual(result["current"]["weather"], "基本晴")
        self.assertEqual(result["forecast"][0]["weather"], "阴")

    def test_research_normalizes_search_results(self) -> None:
        research = ResearchModule(searcher=lambda query, max_results: [{"title": "标题", "href": "https://example.com", "body": "摘要"}])

        result = research.search("测试", max_results=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["results"][0]["url"], "https://example.com")

    def test_ocr_and_speech_call_wxauto_methods(self) -> None:
        image = mock.Mock()
        image.ocr.return_value = "图片文字"
        voice = mock.Mock()
        voice.to_text.return_value = "语音文字"

        self.assertEqual(OCRModule().recognize(image)["text"], "图片文字")
        self.assertEqual(SpeechModule().transcribe(voice)["text"], "语音文字")
        image.ocr.assert_called_once_with(timeout=3)
        voice.to_text.assert_called_once_with()

    def test_ocr_extracts_text_from_wxauto_result_object(self) -> None:
        image = mock.Mock()
        image.ocr.return_value = SimpleNamespace(text="对象里的图片文字")

        result = OCRModule().recognize(image)

        self.assertEqual(result["text"], "对象里的图片文字")

    def test_ocr_does_not_report_failed_response_as_recognized_text(self) -> None:
        for response in (
            {"status": "失败", "message": "图片控件失效"},
            {"success": False, "text": "不是识别结果"},
            {"status": "失败", "text": "不是识别结果"},
        ):
            with self.subTest(response=response):
                message = mock.Mock()
                message.ocr.return_value = response
                result = OCRModule().recognize(message)
                self.assertEqual(result, {"success": False, "reason": "ocr_failed", "text": ""})

    def test_ocr_empty_and_unknown_results_are_not_text(self) -> None:
        for response in ({}, {"message": "failed"}, SimpleNamespace(text=""), False, None):
            with self.subTest(response=response):
                message = mock.Mock()
                message.ocr.return_value = response
                result = OCRModule().recognize(message)
                self.assertEqual(result, {"success": False, "reason": "ocr_empty", "text": ""})

    def test_ocr_preserves_supported_text_results(self) -> None:
        for response in ("图片文字", {"status": "成功", "text": "图片文字"},
                         {"content": "图片文字"}, {"result": {"text": "图片文字"}},
                         ["图片文字", ""], SimpleNamespace(text="图片文字")):
            with self.subTest(response=response):
                message = mock.Mock()
                message.ocr.return_value = response
                self.assertEqual(OCRModule().recognize(message)["text"], "图片文字")

    def test_tool_executor_exposes_media_tools_only_for_matching_message(self) -> None:
        adapter = mock.Mock()
        image = SimpleNamespace(type="image")
        executor = WeChatToolExecutor(
            adapter,
            image,
            "chat",
            allow_weather=True,
            allow_research=True,
            allow_ocr=True,
            allow_speech=True,
        )
        names = {item["function"]["name"] for item in executor.definitions}
        self.assertIn("weather_forecast", names)
        self.assertIn("research_search", names)
        self.assertIn("wechat_ocr", names)
        self.assertNotIn("wechat_transcribe", names)


if __name__ == "__main__":
    unittest.main()

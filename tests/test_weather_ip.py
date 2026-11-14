from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from social_bot.modules.weather import WeatherError, WeatherModule
from social_bot.settings import RuntimeSettings


class ScriptedWeather(WeatherModule):
    """按 URL 片段返回预设响应，用来断言请求顺序和兜底行为。"""

    def __init__(self, responses, **kwargs):
        self.responses = dict(responses)
        self.calls: list[str] = []
        super().__init__(**kwargs)

    def _get_json(self, url, headers=None):
        self.calls.append(url)
        for fragment, payload in self.responses.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise AssertionError(f"脚本没有为请求准备响应：{url}")


def weather_payloads(city: str = "深圳市", province: str = "广东", adcode: str = "440300"):
    """构造一组高德实况 + 预报响应，让城市和 adcode 在断言里保持一致。"""
    live = {
        "status": "1",
        "lives": [
            {
                "province": province,
                "city": city,
                "reporttime": "2026-10-11 18:00:00",
                "weather": "多云",
                "temperature": "24",
                "humidity": "55",
                "winddirection": "东",
                "windpower": "≤3",
            }
        ],
    }
    forecast = {
        "status": "1",
        "forecasts": [
            {
                "province": province,
                "city": city,
                "adcode": adcode,
                "reporttime": "2026-10-11 18:00:00",
                "casts": [
                    {
                        "date": "2026-10-11",
                        "week": "7",
                        "dayweather": "多云",
                        "nightweather": "晴",
                        "daytemp": "25",
                        "nighttemp": "18",
                        "daywind": "东",
                        "daypower": "≤3",
                    }
                ],
            }
        ],
    }
    return live, forecast


LIVE_PAYLOAD, FORECAST_PAYLOAD = weather_payloads()

IPWHOIS_PAYLOAD = {
    "success": True,
    "latitude": 22.5455,
    "longitude": 114.0683,
    "city": "深圳市",
    "region": "广东省",
    "country": "China",
    "timezone": {"id": "Asia/Shanghai"},
}

IPAPI_PAYLOAD = {
    "city": "深圳市",
    "region": "广东省",
    "country_name": "China",
    "latitude": 22.5455,
    "longitude": 114.0683,
    "timezone": "Asia/Shanghai",
}

ALL_IP_HOSTS = ("ipapi.co", "ipwho.is", "ipinfo.io", "ip-api.com")


class AmapIpLocationTests(unittest.TestCase):
    def test_amap_uses_its_own_ip_api_without_third_party_services(self) -> None:
        weather = ScriptedWeather(
            {
                "restapi.amap.com/v3/ip": {
                    "status": "1",
                    "province": "广东省",
                    "city": "深圳市",
                    "adcode": "440300",
                },
                "extensions=all": FORECAST_PAYLOAD,
                "extensions=base": LIVE_PAYLOAD,
            },
            provider="amap",
            timeout_seconds=1,
            amap_api_key="test-key",
        )

        result = weather.forecast(location="", days=1)

        self.assertFalse([url for url in weather.calls if "ipapi.co" in url])
        self.assertEqual(result["location"]["source"], "amap_ip")
        self.assertEqual(result["location"]["adcode"], "440300")
        self.assertEqual(result["location"]["city"], "深圳市")
        self.assertEqual(result["current"]["weather"], "多云")

    def test_amap_ip_failure_falls_back_to_third_party_then_regeo(self) -> None:
        live, forecast = weather_payloads(city="上海市", province="上海", adcode="310101")
        weather = ScriptedWeather(
            {
                "restapi.amap.com/v3/ip": WeatherError("高德天气返回错误：INVALID_USER_KEY"),
                "ipapi.co": WeatherError("天气服务 ipapi.co HTTP 403"),
                "ipwho.is": IPWHOIS_PAYLOAD,
                "geocode/regeo": {
                    "status": "1",
                    "regeocode": {
                        "formatted_address": "上海市黄浦区",
                        "addressComponent": {"province": "上海市", "city": [], "adcode": "310101"},
                    },
                },
                "extensions=all": forecast,
                "extensions=base": live,
            },
            provider="amap",
            timeout_seconds=1,
            amap_api_key="test-key",
        )

        result = weather.forecast(location="", days=1)

        self.assertEqual(result["location"]["source"], "public_ip_amap")
        self.assertEqual(result["location"]["adcode"], "310101")
        # 逆地理编码拿到的 adcode 必须真的用于查天气。
        self.assertTrue([url for url in weather.calls if "city=310101" in url])
        self.assertTrue([url for url in weather.calls if "geocode/regeo" in url])
        # 第三方服务给的是英文城市名，高德行政区接口解析不了，所以必须用经纬度换 adcode。
        self.assertFalse([url for url in weather.calls if "config/district" in url])

    def test_amap_ip_failure_with_overseas_ip_is_reported(self) -> None:
        weather = ScriptedWeather(
            {
                "restapi.amap.com/v3/ip": WeatherError("高德天气返回错误：INVALID_USER_KEY"),
                "ipapi.co": {
                    "city": "Tokyo",
                    "country_name": "Japan",
                    "latitude": 35.6,
                    "longitude": 139.7,
                    "timezone": "Asia/Tokyo",
                },
            },
            provider="amap",
            timeout_seconds=1,
            amap_api_key="test-key",
        )

        with self.assertRaisesRegex(WeatherError, "只支持中国城市"):
            weather.forecast(location="", days=1)

    def test_amap_rate_limit_error_is_retried(self) -> None:
        responses = [
            {"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"},
            {"status": "1", "lives": LIVE_PAYLOAD["lives"]},
        ]

        class RateLimitedWeather(WeatherModule):
            def _get_json(self, url, headers=None):
                return responses.pop(0)

        weather = RateLimitedWeather(provider="amap", timeout_seconds=1, amap_api_key="test-key")
        with mock.patch("social_bot.modules.weather.module.time.sleep") as sleeper:
            data = weather._amap_get("/v3/weather/weatherInfo", {"city": "440300"})

        self.assertEqual(data["status"], "1")
        self.assertEqual(sleeper.call_count, 1)
        self.assertEqual(responses, [])

    def test_amap_non_rate_limit_error_is_not_retried(self) -> None:
        responses = [{"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"}]

        class FailingWeather(WeatherModule):
            def _get_json(self, url, headers=None):
                return responses.pop(0)

        weather = FailingWeather(provider="amap", timeout_seconds=1, amap_api_key="test-key")
        with mock.patch("social_bot.modules.weather.module.time.sleep") as sleeper:
            with self.assertRaisesRegex(WeatherError, "INVALID_USER_KEY"):
                weather._amap_get("/v3/ip", {})

        self.assertEqual(sleeper.call_count, 0)

    def test_amap_daily_quota_error_is_not_treated_as_rate_limit(self) -> None:
        self.assertTrue(WeatherModule._is_amap_rate_limit("CUQPS_HAS_EXCEEDED_THE_LIMIT", "10021"))
        self.assertTrue(WeatherModule._is_amap_rate_limit("ACCESS_TOO_FREQUENT", "10004"))
        # 当日额度用完重试也没有意义，直接报错让用户处理。
        self.assertFalse(WeatherModule._is_amap_rate_limit("DAILY_QUERY_OVER_LIMIT", "10003"))
        self.assertFalse(WeatherModule._is_amap_rate_limit("INVALID_USER_KEY", "10001"))


class AmapCacheTests(unittest.TestCase):
    def test_amap_ip_result_is_cached_and_reused_when_offline(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "data" / "weather_ip_cache.json"
            online = ScriptedWeather(
                {
                    "restapi.amap.com/v3/ip": {
                        "status": "1",
                        "province": "广东省",
                        "city": "深圳市",
                        "adcode": "440300",
                    },
                    "extensions=all": FORECAST_PAYLOAD,
                    "extensions=base": LIVE_PAYLOAD,
                },
                provider="amap",
                timeout_seconds=1,
                amap_api_key="test-key",
                ip_cache_file=cache_path,
            )
            online.forecast(location="", days=1)
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["adcode"], "440300")
            self.assertEqual(saved["city"], "深圳市")

            offline = ScriptedWeather(
                {
                    "restapi.amap.com/v3/ip": WeatherError("天气服务 restapi.amap.com 网络请求失败"),
                    **{host: WeatherError(f"天气服务 {host} HTTP 403") for host in ALL_IP_HOSTS},
                    "extensions=all": FORECAST_PAYLOAD,
                    "extensions=base": LIVE_PAYLOAD,
                },
                provider="amap",
                timeout_seconds=1,
                amap_api_key="test-key",
                ip_cache_file=cache_path,
            )
            result = offline.forecast(location="", days=1)

        self.assertEqual(result["location"]["source"], "amap_ip")
        self.assertEqual(result["location"]["adcode"], "440300")
        self.assertEqual(result["location"]["city"], "深圳市")

    def test_offline_without_any_cache_reports_actionable_error(self) -> None:
        weather = ScriptedWeather(
            {
                "restapi.amap.com/v3/ip": WeatherError("天气服务 restapi.amap.com 网络请求失败"),
                **{host: WeatherError(f"天气服务 {host} HTTP 403") for host in ALL_IP_HOSTS},
            },
            provider="amap",
            timeout_seconds=1,
            amap_api_key="test-key",
        )

        with self.assertRaisesRegex(WeatherError, "weather_location"):
            weather.forecast(location="", days=1)


class IpFallbackTests(unittest.TestCase):
    def test_ip_lookup_falls_back_to_next_service(self) -> None:
        weather = ScriptedWeather(
            {
                "ipapi.co": WeatherError("天气服务 ipapi.co HTTP 403"),
                "ipwho.is": IPWHOIS_PAYLOAD,
            },
            provider="open_meteo",
            timeout_seconds=1,
        )

        result = weather._from_ip()

        self.assertEqual(result["city"], "深圳市")
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual(len(weather.calls), 2)

    def test_all_services_failed_uses_cached_location(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "data" / "weather_ip_cache.json"
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "cached_at": "2026-10-11T09:00:00+08:00",
                        "latitude": 22.5455,
                        "longitude": 114.0683,
                        "city": "深圳市",
                        "region": "广东省",
                        "country": "中国",
                        "timezone": "Asia/Shanghai",
                        "source": "public_ip",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            weather = ScriptedWeather(
                {host: WeatherError(f"天气服务 {host} HTTP 403") for host in ALL_IP_HOSTS},
                provider="open_meteo",
                timeout_seconds=1,
                ip_cache_file=cache_path,
            )

            result = weather._from_ip()

        self.assertEqual(result["city"], "深圳市")
        self.assertEqual(len(weather.calls), len(ALL_IP_HOSTS))

    def test_without_cache_every_failed_service_is_reported(self) -> None:
        weather = ScriptedWeather(
            {host: WeatherError(f"天气服务 {host} HTTP 403") for host in ALL_IP_HOSTS},
            provider="open_meteo",
            timeout_seconds=1,
        )

        with self.assertRaisesRegex(WeatherError, "ip-api.com"):
            weather._from_ip()

    def test_successful_lookup_is_persisted_for_next_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "data" / "weather_ip_cache.json"
            weather = ScriptedWeather(
                {"ipapi.co": IPAPI_PAYLOAD},
                provider="open_meteo",
                timeout_seconds=1,
                ip_cache_file=cache_path,
            )

            weather._from_ip()
            saved = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(saved["city"], "深圳市")
        self.assertEqual(saved["latitude"], 22.5455)
        self.assertIn("cached_at", saved)

    def test_corrupted_cache_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            cache_path = Path(folder) / "weather_ip_cache.json"
            cache_path.write_text("{ 不是 JSON", encoding="utf-8")
            weather = ScriptedWeather(
                {host: WeatherError(f"天气服务 {host} HTTP 403") for host in ALL_IP_HOSTS},
                provider="open_meteo",
                timeout_seconds=1,
                ip_cache_file=cache_path,
            )

            self.assertIsNone(weather._cached_ip_location)
            with self.assertRaises(WeatherError):
                weather._from_ip()


class WeatherCachePathTests(unittest.TestCase):
    def test_cache_file_lives_next_to_database(self) -> None:
        settings = RuntimeSettings(database_path="data/bot.sqlite3")

        cache_path = Path(settings.weather_cache_file)

        self.assertEqual(cache_path.name, "weather_ip_cache.json")
        self.assertEqual(cache_path.parent.name, "data")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import html
import http.client
import logging
import os
from pathlib import Path
import re
import time
from datetime import datetime
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ... import net

LOGGER = logging.getLogger(__name__)


class WeatherError(RuntimeError):
    pass


def _amap_text(value: Any) -> str:
    """高德接口可能用空数组表示“没有结果”，这里统一转成空字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    return ""


_WEATHER_CODES = {
    0: "晴",
    1: "基本晴",
    2: "部分多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴大冰雹",
}


class WeatherModule:
    """按配置查询天气。

    ``amap`` 使用高德地图天气 API，IP 定位优先走高德自己的 ``/v3/ip``；
    ``tianqi`` 使用天气网中国城市页面及其页面公开调用的预报数据；
    ``open_meteo`` 和 ``qweather`` 保留为可选兼容 provider。
    """

    # 公网 IP 定位的兜底顺序：ipapi.co 会限流甚至直接返回 403，所以准备多个备选服务。
    # 每项是 (主机名, 请求地址, 解析方法名)。
    _IP_LOOKUP_SOURCES: tuple[tuple[str, str, str], ...] = (
        ("ipapi.co", "https://ipapi.co/json/", "_parse_ipapi_co"),
        ("ipwho.is", "https://ipwho.is/", "_parse_ipwhois"),
        ("ipinfo.io", "https://ipinfo.io/json", "_parse_ipinfo"),
        (
            "ip-api.com",
            "http://ip-api.com/json/?fields=status,message,country,regionName,city,lat,lon,timezone",
            "_parse_ip_api",
        ),
    )

    # 免费 Key 的 QPS 很低，连续调用容易被限流；限流时按下面的间隔重试。
    _AMAP_RETRY_ATTEMPTS = 2
    _AMAP_RETRY_DELAYS = (1.0, 2.5)

    def __init__(
        self,
        timeout_seconds: int = 15,
        ip_cache_seconds: int = 600,
        provider: str = "open_meteo",
        qweather_api_host: str = "",
        qweather_api_key: str = "",
        amap_api_key: str = "",
        ip_cache_file: str | Path = "",
    ) -> None:
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.ip_cache_seconds = max(0, int(ip_cache_seconds))
        self.provider = str(provider or "open_meteo").strip().lower().replace("-", "_")
        if self.provider not in {"amap", "tianqi", "open_meteo", "qweather"}:
            raise ValueError("天气 provider 必须是 amap、tianqi、open_meteo 或 qweather")
        self.qweather_api_host = str(qweather_api_host or "").strip()
        self.qweather_api_key = str(qweather_api_key or "").strip()
        self.amap_api_key = str(amap_api_key or "").strip() or self._read_amap_api_key()
        self._ip_location: dict[str, Any] | None = None
        self._ip_location_at = 0.0
        self._amap_ip_target: dict[str, Any] | None = None
        self._amap_ip_target_at = 0.0
        self.ip_cache_file = Path(ip_cache_file) if str(ip_cache_file or "").strip() else None
        # 上一次成功定位的结果；所有在线 IP 定位都失败时用它兜底，避免整条流程停摆。
        cached_location = self._load_cached_location() or {}
        self._cached_ip_location = self._ip_location_from_cache(cached_location)
        self._cached_amap_target = self._amap_target_from_cache(cached_location)

    @staticmethod
    def _read_amap_api_key() -> str:
        """读取高德天气 Key；支持环境变量和项目根目录 weather_api 文件。"""
        for env_name in ("AMAP_WEATHER_API_KEY", "AMAP_API_KEY"):
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
        for filename in ("weather_api", "weather_api.txt"):
            private_path = Path('private/secrets') / filename
            path = private_path if private_path.is_file() else Path(filename)
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                value = line.strip()
                if value and not value.startswith("#"):
                    return value
        return ""

    def _get_json(self, url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            # QWeather 默认可能返回 gzip；urllib 不会在所有 Python 版本自动解压，
            # 主动请求未压缩响应，避免把压缩字节误当成 JSON。
            "Accept-Encoding": "identity",
            "User-Agent": "deepseek-social-bot/1.0",
        }
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            url,
            headers=request_headers,
            method="GET",
        )
        try:
            with net.urlopen(request, timeout=self.timeout_seconds) as response:
                try:
                    raw = response.read()
                except http.client.IncompleteRead:
                    # 天气网偶尔提前关闭 chunked 响应，重试一次可避免把临时传输问题
                    # 误报成天气接口格式错误。
                    with net.urlopen(request, timeout=self.timeout_seconds) as retry_response:
                        raw = retry_response.read()
                data = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise WeatherError(f"天气服务 {urllib.parse.urlparse(url).netloc} HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise WeatherError(
                    f"天气服务 {urllib.parse.urlparse(url).netloc} 请求超时（{self.timeout_seconds} 秒）"
                ) from exc
            raise WeatherError(
                f"天气服务 {urllib.parse.urlparse(url).netloc} 网络请求失败: {reason}{net.proxy_hint()}"
            ) from exc
        except TimeoutError as exc:
            raise WeatherError(
                f"天气服务 {urllib.parse.urlparse(url).netloc} 请求超时（{self.timeout_seconds} 秒）"
            ) from exc
        except http.client.IncompleteRead as exc:
            raise WeatherError(
                f"天气服务 {urllib.parse.urlparse(url).netloc} 返回数据不完整"
            ) from exc
        except json.JSONDecodeError as exc:
            raise WeatherError("天气服务返回了无法解析的 JSON") from exc
        if not isinstance(data, dict):
            raise WeatherError("天气服务返回格式异常")
        return data

    def _qweather_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.qweather_api_host:
            raise WeatherError("QWeather 未配置 weather_api_host")
        if not self.qweather_api_key:
            raise WeatherError("QWeather 未配置 API Key，请设置 QWEATHER_API_KEY")
        host = self.qweather_api_host.strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        query = urllib.parse.urlencode(params)
        data = self._get_json(
            f"{host}/{path.lstrip('/')}?{query}",
            headers={"X-QW-Api-Key": self.qweather_api_key},
        )
        if str(data.get("code", "200")) != "200":
            raise WeatherError(f"QWeather 返回错误码：{data.get('code', 'unknown')}")
        return data

    @staticmethod
    def _ip_location_result(
        *,
        latitude: Any,
        longitude: Any,
        city: Any = "",
        region: Any = "",
        country: Any = "",
        timezone: Any = "auto",
        source: str = "public_ip",
    ) -> dict[str, Any]:
        try:
            parsed_latitude = float(latitude)
            parsed_longitude = float(longitude)
        except (TypeError, ValueError) as exc:
            raise WeatherError("IP 定位结果缺少经纬度") from exc
        return {
            "latitude": parsed_latitude,
            "longitude": parsed_longitude,
            "city": str(city or "未知城市"),
            "region": str(region or ""),
            "country": str(country or ""),
            "timezone": str(timezone or "auto"),
            "source": source,
        }

    def _parse_ipapi_co(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("error"):
            raise WeatherError(str(payload.get("reason") or "定位失败"))
        return self._ip_location_result(
            latitude=payload.get("latitude"),
            longitude=payload.get("longitude"),
            city=payload.get("city"),
            region=payload.get("region"),
            country=payload.get("country_name") or payload.get("country"),
            timezone=payload.get("timezone"),
        )

    def _parse_ipwhois(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("success") is False:
            raise WeatherError(str(payload.get("message") or "定位失败"))
        timezone_value = payload.get("timezone")
        if isinstance(timezone_value, dict):
            timezone_value = timezone_value.get("id")
        return self._ip_location_result(
            latitude=payload.get("latitude"),
            longitude=payload.get("longitude"),
            city=payload.get("city"),
            region=payload.get("region"),
            country=payload.get("country"),
            timezone=timezone_value,
        )

    def _parse_ipinfo(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("bogon"):
            raise WeatherError("该服务把当前出口识别为内网地址")
        latitude, separator, longitude = str(payload.get("loc") or "").partition(",")
        if not separator:
            raise WeatherError("定位结果缺少经纬度")
        return self._ip_location_result(
            latitude=latitude,
            longitude=longitude,
            city=payload.get("city"),
            region=payload.get("region"),
            country=payload.get("country"),
            timezone=payload.get("timezone"),
        )

    def _parse_ip_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("status") or "").strip().lower() != "success":
            raise WeatherError(str(payload.get("message") or "定位失败"))
        return self._ip_location_result(
            latitude=payload.get("lat"),
            longitude=payload.get("lon"),
            city=payload.get("city"),
            region=payload.get("regionName"),
            country=payload.get("country"),
            timezone=payload.get("timezone"),
        )

    def _load_cached_location(self) -> dict[str, Any] | None:
        """读取上次成功定位的原始记录；文件损坏或缺失时返回 None。"""
        path = self.ip_cache_file
        if path is None:
            return None
        try:
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            LOGGER.warning("读取天气定位缓存失败：%s", exc)
            return None
        return data if isinstance(data, dict) else None

    def _ip_location_from_cache(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self._ip_location_result(
                latitude=payload.get("latitude"),
                longitude=payload.get("longitude"),
                city=payload.get("city"),
                region=payload.get("region"),
                country=payload.get("country"),
                timezone=payload.get("timezone"),
                source=str(payload.get("source") or "public_ip"),
            )
        except WeatherError:
            return None

    @staticmethod
    def _amap_target_from_cache(payload: dict[str, Any]) -> dict[str, Any] | None:
        adcode = _amap_text(payload.get("adcode"))
        city = _amap_text(payload.get("city"))
        if not adcode or not city:
            return None
        return {
            "adcode": adcode,
            "city": city,
            "region": _amap_text(payload.get("region")),
            "country": _amap_text(payload.get("country")) or "中国",
            "timezone": _amap_text(payload.get("timezone")) or "Asia/Shanghai",
            "source": str(payload.get("source") or "amap_ip"),
        }

    def _save_cached_location(self, payload: dict[str, Any]) -> None:
        """把这次成功定位写入缓存文件；只保留一份自洽的记录。"""
        path = self.ip_cache_file
        if path is None:
            return
        record = {key: value for key, value in payload.items() if value not in (None, "")}
        record["cached_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("写入天气定位缓存失败：%s", exc)

    def _from_ip(self) -> dict[str, Any]:
        """按顺序尝试多个公网 IP 定位服务；全部失败时退回上次成功的位置。"""
        now = time.monotonic()
        if self._ip_location and now - self._ip_location_at < self.ip_cache_seconds:
            return dict(self._ip_location)
        failures: list[str] = []
        for host, url, parser_name in self._IP_LOOKUP_SOURCES:
            try:
                payload = self._get_json(url)
                result = getattr(self, parser_name)(payload)
            except Exception as exc:  # noqa: BLE001 - 单个服务失败不应中断兜底链
                failures.append(f"{host}：{exc}")
                continue
            self._ip_location = result
            self._ip_location_at = now
            self._cached_ip_location = dict(result)
            self._save_cached_location(result)
            return dict(result)
        detail = "；".join(failures) or "没有可用的 IP 定位服务"
        if self._cached_ip_location:
            LOGGER.warning(
                "公网 IP 定位全部失败，改用上次成功的位置 %s（%s）",
                self._cached_ip_location.get("city"),
                detail,
            )
            return dict(self._cached_ip_location)
        raise WeatherError(f"公网 IP 定位失败（{detail}）")

    def _amap_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.amap_api_key:
            raise WeatherError("高德天气未配置 API Key，请在项目根目录 weather_api.txt 中放入 Key")
        query = dict(params)
        query.update({"key": self.amap_api_key, "output": "JSON"})
        url = f"https://restapi.amap.com{path}?{urllib.parse.urlencode(query)}"
        # 免费 Key 的 QPS 很低，连续调用容易被限流；限流时短暂等待后重试，
        # 避免把「调用过于频繁」当成天气服务故障，直接中断朋友圈发布。
        for attempt in range(self._AMAP_RETRY_ATTEMPTS + 1):
            data = self._get_json(url)
            if str(data.get("status")) == "1":
                return data
            info = str(data.get("info") or "")
            infocode = str(data.get("infocode") or "")
            if attempt < self._AMAP_RETRY_ATTEMPTS and self._is_amap_rate_limit(info, infocode):
                LOGGER.warning("高德接口限流（%s），%.1f 秒后重试", info or infocode, self._AMAP_RETRY_DELAYS[attempt])
                time.sleep(self._AMAP_RETRY_DELAYS[attempt])
                continue
            raise WeatherError(f"高德天气返回错误：{info or infocode or '未知错误'}")
        raise WeatherError("高德天气返回错误：重试后仍然失败")

    @staticmethod
    def _is_amap_rate_limit(info: str, infocode: str) -> bool:
        """区分「调用太快」和「Key 无效 / 当日额度用完」，只对前者重试。"""
        marker = f"{info} {infocode}".upper()
        return any(
            token in marker
            for token in ("QPS", "TOO_FREQUENT", "CUQPS", "过于频繁", "10019", "10020", "10021", "10022")
        )

    def _from_amap_place(self, place: str) -> dict[str, Any]:
        requested = str(place or "").strip()
        if not requested:
            raise WeatherError("高德天气查询需要中国城市名称")
        data = self._amap_get(
            "/v3/config/district",
            {"keywords": requested, "subdistrict": 0, "extensions": "base"},
        )
        districts = data.get("districts")
        if not isinstance(districts, list) or not districts:
            raise WeatherError(f"高德地图找不到中国城市：{requested}")
        target = districts[0]
        adcode = str(target.get("adcode") or "").strip()
        if not adcode:
            raise WeatherError(f"高德地图城市结果缺少 adcode：{requested}")
        return {
            "adcode": adcode,
            "city": str(target.get("name") or requested),
            "region": str(target.get("province") or ""),
            "country": "中国",
            "timezone": "Asia/Shanghai",
            "source": "amap_district",
        }

    def _amap_target_from_coordinates(self, latitude: Any, longitude: Any) -> dict[str, Any]:
        """用高德逆地理编码把经纬度换成 adcode，避免第三方服务只给英文城市名。"""
        try:
            location = f"{float(longitude):.6f},{float(latitude):.6f}"
        except (TypeError, ValueError) as exc:
            raise WeatherError("IP 定位结果缺少经纬度") from exc
        data = self._amap_get(
            "/v3/geocode/regeo",
            {"location": location, "extensions": "base", "radius": 1000},
        )
        regeocode = data.get("regeocode")
        component = regeocode.get("addressComponent") if isinstance(regeocode, dict) else None
        if not isinstance(component, dict):
            raise WeatherError("高德逆地理编码返回格式异常")
        adcode = _amap_text(component.get("adcode"))
        province = _amap_text(component.get("province"))
        city = _amap_text(component.get("city")) or province
        if not adcode:
            raise WeatherError("高德逆地理编码没有返回 adcode")
        return {
            "adcode": adcode,
            "city": city or "未知城市",
            "region": province,
            "country": "中国",
            "timezone": "Asia/Shanghai",
            "source": "amap_regeo",
        }

    def _amap_target_from_public_ip(self) -> dict[str, Any]:
        """公网 IP 兜底：用第三方服务定位到经纬度，再交给高德逆地理编码解析。"""
        try:
            ip_target = self._from_ip()
        except WeatherError as exc:
            raise WeatherError(
                "高德 IP 定位和公网 IP 定位都失败了；"
                "可以在 config.json 的 moments.weather_location 固定城市名称，避免依赖 IP 定位："
                f"{exc}"
            ) from exc
        country = str(ip_target.get("country") or "")
        if country not in {"中国", "China", "CN", "CHN"}:
            raise WeatherError("高德天气当前只支持中国城市，无法查询当前 IP 的海外所在地")
        try:
            target = self._amap_target_from_coordinates(
                ip_target.get("latitude"),
                ip_target.get("longitude"),
            )
        except WeatherError as exc:
            raise WeatherError(
                "高德 IP 定位不可用，公网 IP 定位到的位置也无法解析成中国城市；"
                "可以在 config.json 的 moments.weather_location 固定城市名称："
                f"{exc}"
            ) from exc
        target["source"] = "public_ip_amap"
        self._cached_amap_target = dict(target)
        # 这份记录的经纬度和 adcode 来自同一次定位，彼此自洽。
        self._save_cached_location(
            {
                "latitude": ip_target.get("latitude"),
                "longitude": ip_target.get("longitude"),
                "city": target["city"],
                "region": target["region"],
                "adcode": target["adcode"],
                "country": target["country"],
                "timezone": target["timezone"],
                "source": "public_ip_amap",
            }
        )
        return target

    def _amap_target_with_cache_fallback(self, cause: str) -> dict[str, Any]:
        """高德 IP 接口不可用时的兜底：第三方 IP + 逆地理编码，再不行就用上次成功的定位。"""
        try:
            return self._amap_target_from_public_ip()
        except WeatherError as exc:
            if not self._cached_amap_target:
                raise
            LOGGER.warning(
                "在线 IP 定位都失败了（%s：%s），改用上次成功的位置 %s",
                cause,
                exc,
                self._cached_amap_target.get("city"),
            )
            return dict(self._cached_amap_target)

    def _from_amap_ip(self) -> dict[str, Any]:
        """用高德自己的 IP 定位接口解析当前出口城市。

        高德 ``/v3/ip`` 只依赖已经配置好的高德 Key，不再依赖 ipapi.co 这类第三方服务；
        ``/v3/ip`` 返回的 adcode 已经是城市级编码，直接用它查询天气，避免多打一次接口。
        """
        now = time.monotonic()
        if self._amap_ip_target and now - self._amap_ip_target_at < self.ip_cache_seconds:
            return dict(self._amap_ip_target)
        try:
            data = self._amap_get("/v3/ip", {})
        except WeatherError as exc:
            LOGGER.warning("高德 IP 定位接口不可用，改用公网 IP 服务：%s", exc)
            target = self._amap_target_with_cache_fallback("高德 IP 定位接口不可用")
            self._amap_ip_target = dict(target)
            self._amap_ip_target_at = now
            return target
        province = _amap_text(data.get("province"))
        city = _amap_text(data.get("city"))
        adcode = _amap_text(data.get("adcode"))
        name = city or province
        if adcode and name:
            target = {
                "adcode": adcode,
                "city": name,
                "region": province,
                "country": "中国",
                "timezone": "Asia/Shanghai",
                "source": "amap_ip",
            }
        elif name:
            # 极少数情况下 /v3/ip 只给城市名，此时再走行政区解析补 adcode。
            try:
                target = self._from_amap_place(name)
            except WeatherError as exc:
                raise WeatherError(f"高德 IP 定位到「{name}」，但行政区解析失败：{exc}") from exc
            target["source"] = "amap_ip"
        else:
            LOGGER.warning("高德 IP 定位没有返回城市（可能是海外出口或国内定位不到），改用公网 IP 服务")
            target = self._amap_target_with_cache_fallback("高德 IP 定位没有返回城市")
        self._amap_ip_target = dict(target)
        self._amap_ip_target_at = now
        self._cached_amap_target = dict(target)
        self._save_cached_location(target)
        return target

    def _amap_forecast(self, target: dict[str, Any], days: int) -> dict[str, Any]:
        live_data = self._amap_get(
            "/v3/weather/weatherInfo",
            {"city": target["adcode"], "extensions": "base"},
        )
        data = self._amap_get(
            "/v3/weather/weatherInfo",
            {"city": target["adcode"], "extensions": "all"},
        )
        lives = live_data.get("lives") or []
        forecasts = data.get("forecasts") or []
        live = lives[0] if isinstance(lives, list) and lives and isinstance(lives[0], dict) else {}
        forecast_root = forecasts[0] if isinstance(forecasts, list) and forecasts and isinstance(forecasts[0], dict) else {}
        casts = forecast_root.get("casts") or []
        current = {
            "time": live.get("reporttime"),
            "temperature_c": self._number(live.get("temperature")),
            "apparent_temperature_c": None,
            "humidity_percent": self._number(live.get("humidity")),
            "wind_speed_kmh": None,
            "weather": live.get("weather") or (casts[0].get("dayweather") if casts else "未知天气"),
            "wind": f"{live.get('winddirection', '')}{live.get('windpower', '')}".strip(),
            "temperature_min_c": self._number(casts[0].get("nighttemp")) if casts else None,
            "temperature_max_c": self._number(casts[0].get("daytemp")) if casts else None,
            "weather_code": None,
        }
        result_forecast: list[dict[str, Any]] = []
        for cast in casts[:days]:
            if not isinstance(cast, dict):
                continue
            result_forecast.append(
                {
                    "date": cast.get("date"),
                    "week": cast.get("week"),
                    "weather": cast.get("dayweather") or cast.get("nightweather") or "未知天气",
                    "day_weather": cast.get("dayweather"),
                    "night_weather": cast.get("nightweather"),
                    "temperature_max_c": self._number(cast.get("daytemp")),
                    "temperature_min_c": self._number(cast.get("nighttemp")),
                    "precipitation_probability_percent": None,
                    "wind": f"{cast.get('daywind', '')}{cast.get('daypower', '')}".strip(),
                }
            )
        if not result_forecast:
            raise WeatherError("高德天气没有返回可用的预报条目")
        return {
            "success": True,
            "provider": "高德地图天气 API（weatherInfo；中国城市）",
            "location": {
                "city": forecast_root.get("city") or target["city"],
                "region": forecast_root.get("province") or target["region"],
                "country": "中国",
                "timezone": target["timezone"],
                "adcode": forecast_root.get("adcode") or target["adcode"],
                "source": target["source"],
            },
            "current": current,
            "forecast": result_forecast,
            "reporttime": forecast_root.get("reporttime") or live.get("reporttime"),
        }

    def _get_html(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) deepseek-social-bot/1.0",
            },
            method="GET",
        )
        try:
            with net.urlopen(request, timeout=self.timeout_seconds) as response:
                try:
                    raw = response.read()
                except http.client.IncompleteRead:
                    with net.urlopen(request, timeout=self.timeout_seconds) as retry_response:
                        raw = retry_response.read()
        except urllib.error.HTTPError as exc:
            raise WeatherError(f"天气服务 {urllib.parse.urlparse(url).netloc} HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise WeatherError(
                    f"天气服务 {urllib.parse.urlparse(url).netloc} 请求超时（{self.timeout_seconds} 秒）"
                ) from exc
            raise WeatherError(
                f"天气服务 {urllib.parse.urlparse(url).netloc} 网络请求失败: {reason}{net.proxy_hint()}"
            ) from exc
        except TimeoutError as exc:
            raise WeatherError(
                f"天气服务 {urllib.parse.urlparse(url).netloc} 请求超时（{self.timeout_seconds} 秒）"
            ) from exc
        except http.client.IncompleteRead as exc:
            raise WeatherError(
                f"天气服务 {urllib.parse.urlparse(url).netloc} 返回数据不完整"
            ) from exc
        for encoding in ("utf-8", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _normalise_china_city(value: str) -> str:
        city = str(value or "").strip()
        city = re.sub(r"^(中国|中华人民共和国)[省市]?(?=[\u4e00-\u9fff])", "", city)
        city = re.sub(r"(特别行政区|自治州|地区|盟|市)$", "", city)
        return city.strip()

    def _tianqi_city_map(self) -> dict[str, str]:
        """从天气网全国城市页取得中文城市名到页面 slug 的映射。"""
        page = self._get_html("https://www.tianqi.com/chinacity.html")
        mapping: dict[str, str] = {}
        # 该页面的城市链接均是 /pinyin/，标题包含“城市天气预报”。
        pattern = re.compile(
            r'<a\s+[^>]*href=["\']/([^"\'/]+?)/["\'][^>]*title=["\']([^"\']*?天气预报)["\'][^>]*>(.*?)</a>',
            re.I | re.S,
        )
        for match in pattern.finditer(page):
            slug = match.group(1).strip().lower()
            name = re.sub(r"<[^>]+>", "", match.group(3))
            name = html.unescape(name).strip()
            name = re.sub(r"天气预报.*$", "", name).strip()
            if slug and name and re.search(r"[\u4e00-\u9fff]", name):
                mapping.setdefault(self._normalise_china_city(name), slug)
        return mapping

    def _from_tianqi_place(self, place: str) -> dict[str, Any]:
        requested = str(place or "").strip()
        if not requested:
            raise WeatherError("天气网查询需要中国城市名称")
        # 只接受全国城市页中出现的中国城市，避免把英文地点误当成海外城市。
        mapping = self._tianqi_city_map()
        normalised = self._normalise_china_city(requested)
        slug = mapping.get(normalised)
        if not slug and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", requested):
            candidate = requested.lower().strip("/")
            if candidate in set(mapping.values()):
                slug = candidate
        if not slug:
            raise WeatherError(f"天气网目前只支持中国城市，找不到中国城市：{requested}")
        page_url = f"https://www.tianqi.com/{urllib.parse.quote(slug)}/7/"
        page = self._get_html(page_url)
        id_match = re.search(r"var\s+prov\s*=\s*['\"](\d+)['\"]", page)
        city_match = re.search(r"var\s+city_name\s*=\s*['\"]([^'\"]+)['\"]", page)
        pinyin_match = re.search(r"var\s+pinyin\s*=\s*['\"]([^'\"]+)['\"]", page)
        if not id_match:
            raise WeatherError(f"天气网城市页面缺少城市编号：{requested}")
        return {
            "city_id": id_match.group(1),
            "city": city_match.group(1) if city_match else normalised,
            "region": "中国",
            "country": "中国",
            "timezone": "Asia/Shanghai",
            "source": "tianqi_city_page",
            "slug": pinyin_match.group(1) if pinyin_match else slug,
            "page": page,
        }

    @staticmethod
    def _tianqi_current(page: str, target: dict[str, Any]) -> dict[str, Any]:
        current_text_match = re.search(r'<div\s+class=["\']weaone_ba["\'][^>]*>(.*?)</div>', page, re.I | re.S)
        current_text = html.unescape(re.sub(r"<[^>]+>", "", current_text_match.group(1))).strip() if current_text_match else ""
        temp_match = re.search(r"当前温度\s*([+-]?\d+(?:\.\d+)?)℃", current_text)
        weather_match = re.search(r"今日天气：.*?，([^,，]+)[,，]", current_text)
        range_match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*[~～]\s*([+-]?\d+(?:\.\d+)?)℃", current_text)
        wind_match = re.search(r"，([^,，]+风[^,，]*)，当前温度", current_text)
        return {
            "time": None,
            "temperature_c": float(temp_match.group(1)) if temp_match else None,
            "apparent_temperature_c": None,
            "humidity_percent": None,
            "wind_speed_kmh": None,
            "wind": wind_match.group(1) if wind_match else None,
            "weather": weather_match.group(1).strip() if weather_match else "未知天气",
            "temperature_min_c": float(range_match.group(1)) if range_match else None,
            "temperature_max_c": float(range_match.group(2)) if range_match else None,
            "weather_code": None,
            "source_page": f"https://www.tianqi.com/{target['slug']}/",
        }

    def _tianqi_forecast(self, target: dict[str, Any], days: int) -> dict[str, Any]:
        data = self._get_json(
            f"https://www.tianqi.com/tianqi/tianqidata/{target['city_id']}"
        )
        rows = data.get("data")
        if data.get("code") not in (None, 200, "200") or not isinstance(rows, list) or not rows:
            raise WeatherError("天气网返回了异常的预报数据")
        forecast: list[dict[str, Any]] = []
        for row in rows[:days]:
            if not isinstance(row, dict):
                continue
            year = row.get("y") or time.localtime().tm_year
            month = str(row.get("m") or "").zfill(2)
            day = str(row.get("d") or "").zfill(2)
            date = f"{year}-{month}-{day}" if month.strip("0") and day.strip("0") else str(row.get("date") or "")
            probability = (row.get("precipitation") or {}).get("probability")
            try:
                probability = float(probability) if probability not in (None, "") else None
            except (TypeError, ValueError):
                probability = None
            forecast.append(
                {
                    "date": date,
                    "weather_code": row.get("img_num"),
                    "weather": row.get("weather") or "未知天气",
                    "temperature_max_c": self._number(row.get("htemp")),
                    "temperature_min_c": self._number(row.get("ltemp")),
                    "precipitation_probability_percent": probability,
                    "wind": f"{row.get('WD', '')}{row.get('WS', '')}".strip(),
                    "humidity_percent": self._number(str(row.get("SD", "")).rstrip("%")),
                }
            )
        if not forecast:
            raise WeatherError("天气网没有返回可用的预报条目")
        current = self._tianqi_current(target.get("page", ""), target)
        return {
            "success": True,
            "provider": "天气网 tianqi.com（页面数据；页面标注数据来源中国气象局）",
            "location": {
                "city": target["city"],
                "region": target["region"],
                "country": target["country"],
                "timezone": target["timezone"],
                "source": target["source"],
                "city_page": f"https://www.tianqi.com/{target['slug']}/",
            },
            "current": current,
            "forecast": forecast,
        }

    @staticmethod
    def _number(value: Any) -> float | int | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return int(number) if number.is_integer() else number

    def _from_place(self, place: str) -> dict[str, Any]:
        query = urllib.parse.urlencode(
            {"name": place, "count": 1, "language": "zh", "format": "json"}
        )
        data = self._get_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}")
        results = data.get("results")
        if not isinstance(results, list) or not results:
            raise WeatherError(f"找不到地点：{place}")
        target = results[0]
        try:
            result = {
                "latitude": float(target["latitude"]),
                "longitude": float(target["longitude"]),
                "city": str(target.get("name") or place),
                "region": str(target.get("admin1") or ""),
                "country": str(target.get("country") or ""),
                "timezone": str(target.get("timezone") or "auto"),
                "source": "place_search",
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError("地点解析结果缺少经纬度") from exc
        return result

    def _from_qweather_place(self, place: str) -> dict[str, Any]:
        data = self._qweather_get(
            "/geo/v2/city/lookup",
            {"location": place, "number": 1, "lang": "zh"},
        )
        locations = data.get("location")
        if not isinstance(locations, list) or not locations:
            raise WeatherError(f"找不到地点：{place}")
        target = locations[0]
        try:
            return {
                "latitude": float(target["lat"]),
                "longitude": float(target["lon"]),
                "city": str(target.get("name") or place),
                "region": str(target.get("adm1") or target.get("adm2") or ""),
                "country": str(target.get("country") or ""),
                "timezone": str(target.get("tz") or "auto"),
                "location_id": str(target.get("id") or ""),
                "source": "qweather_geo",
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise WeatherError("QWeather 地点结果缺少经纬度") from exc

    def _qweather_forecast(self, target: dict[str, Any], days: int) -> dict[str, Any]:
        lat = target["latitude"]
        lon = target["longitude"]
        current_data = self._qweather_get(
            f"/weather/v1/current/{lat:.2f}/{lon:.2f}",
            {"localTime": "true", "lang": "zh"},
        )
        daily_data = self._qweather_get(
            f"/weather/v1/daily/{lat:.2f}/{lon:.2f}",
            {"days": days, "localTime": "true", "lang": "zh"},
        )
        current = current_data.get("condition") or {}
        temperature = current_data.get("temperature") or {}
        feels_like = current_data.get("feelsLike") or {}
        wind = current_data.get("wind") or {}
        wind_speed = wind.get("speed") or {}
        current_view = {
            "time": current_data.get("obsTime") or current_data.get("updateTime"),
            "temperature_c": temperature.get("value"),
            "apparent_temperature_c": feels_like.get("value"),
            "humidity_percent": round(float(current_data.get("humidity", 0)) * 100, 1),
            "wind_speed_kmh": round(float(wind_speed.get("value", 0)) * 3.6, 1),
            "weather_code": current.get("code"),
            "weather": current.get("text") or "未知天气",
        }
        forecast: list[dict[str, Any]] = []
        for day in (daily_data.get("days") or [])[:days]:
            if not isinstance(day, dict):
                continue
            daytime = day.get("daytime") or {}
            condition = daytime.get("condition") or day.get("condition") or {}
            maximum = day.get("temperatureMax") or {}
            minimum = day.get("temperatureMin") or {}
            precipitation = daytime.get("precipitation") or day.get("precipitation") or {}
            probability = precipitation.get("probability")
            if probability is not None:
                probability = round(float(probability) * 100, 1)
            forecast.append(
                {
                    "date": str(day.get("forecastStartTime") or "")[:10],
                    "weather_code": condition.get("code"),
                    "weather": condition.get("text") or "未知天气",
                    "temperature_max_c": maximum.get("value"),
                    "temperature_min_c": minimum.get("value"),
                    "precipitation_probability_percent": probability,
                }
            )
        return {
            "success": True,
            "provider": "QWeather 和风天气（GeoAPI + 实时天气 + 每日预报）",
            "location": {
                "city": target["city"],
                "region": target["region"],
                "country": target["country"],
                "timezone": target["timezone"],
                "latitude": target["latitude"],
                "longitude": target["longitude"],
                "source": target["source"],
            },
            "current": current_view,
            "forecast": forecast,
        }

    def forecast(
        self,
        location: str = "",
        days: int = 3,
        use_ip: bool = True,
        query_time: str = "",
    ) -> dict[str, Any]:
        days = max(1, min(7, int(days)))
        location = str(location or "").strip()
        query_time = str(query_time or datetime.now().astimezone().isoformat(timespec="seconds"))
        if self.provider == "amap":
            if location:
                target = self._from_amap_place(location)
            elif use_ip:
                # 优先用高德自己的 IP 定位，第三方 IP 服务（ipapi.co 等）只作兜底。
                target = self._from_amap_ip()
            else:
                raise WeatherError("没有提供地点，且已关闭 IP 定位")
            result = self._amap_forecast(target, days)
            result["query_time"] = query_time
            return result
        if self.provider == "tianqi":
            if location:
                target = self._from_tianqi_place(location)
            elif use_ip:
                ip_target = self._from_ip()
                country = str(ip_target.get("country") or "")
                if country not in {"中国", "China", "CN", "CHN"}:
                    raise WeatherError("天气网当前只支持中国城市，无法查询当前 IP 的海外所在地")
                target = self._from_tianqi_place(str(ip_target.get("city") or ""))
                target["source"] = "public_ip_tianqi"
            else:
                raise WeatherError("没有提供地点，且已关闭 IP 定位")
            result = self._tianqi_forecast(target, days)
            result["query_time"] = query_time
            return result
        if self.provider == "qweather":
            if location:
                target = self._from_qweather_place(location)
            elif use_ip:
                # 公网 IP 只负责把运行机器定位到经纬度；实际天气全部来自 QWeather。
                target = self._from_ip()
                target["source"] = "public_ip_qweather"
            else:
                raise WeatherError("没有提供地点，且已关闭 IP 定位")
            result = self._qweather_forecast(target, days)
            result["query_time"] = query_time
            return result

        if location:
            target = self._from_place(location)
        elif use_ip:
            target = self._from_ip()
        else:
            raise WeatherError("没有提供地点，且已关闭 IP 定位")

        query = urllib.parse.urlencode(
            {
                "latitude": target["latitude"],
                "longitude": target["longitude"],
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": days,
                "timezone": "auto",
            }
        )
        data = self._get_json(f"https://api.open-meteo.com/v1/forecast?{query}")
        current = data.get("current")
        daily = data.get("daily")
        if not isinstance(current, dict) or not isinstance(daily, dict):
            raise WeatherError("天气预报结果格式异常")

        current_code = current.get("weather_code")
        current_view = {
            "time": current.get("time"),
            "temperature_c": current.get("temperature_2m"),
            "apparent_temperature_c": current.get("apparent_temperature"),
            "humidity_percent": current.get("relative_humidity_2m"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "weather_code": current_code,
            "weather": _WEATHER_CODES.get(current_code, "未知天气"),
        }
        dates = daily.get("time") or []
        codes = daily.get("weather_code") or []
        maximums = daily.get("temperature_2m_max") or []
        minimums = daily.get("temperature_2m_min") or []
        rain = daily.get("precipitation_probability_max") or []
        forecast: list[dict[str, Any]] = []
        for index, date in enumerate(dates):
            code = codes[index] if index < len(codes) else None
            forecast.append(
                {
                    "date": date,
                    "weather_code": code,
                    "weather": _WEATHER_CODES.get(code, "未知天气"),
                    "temperature_max_c": maximums[index] if index < len(maximums) else None,
                    "temperature_min_c": minimums[index] if index < len(minimums) else None,
                    "precipitation_probability_percent": rain[index] if index < len(rain) else None,
                }
            )
        return {
            "success": True,
            "query_time": query_time,
            "provider": (
                "ipapi.co（公网 IP 定位）+ Open-Meteo（天气预报）"
                if target["source"] == "public_ip"
                else "Open-Meteo（地点解析和天气预报）"
            ),
            "location": {
                "city": target["city"],
                "region": target["region"],
                "country": target["country"],
                "timezone": target["timezone"],
                "latitude": target["latitude"],
                "longitude": target["longitude"],
                "source": target["source"],
            },
            "current": current_view,
            "forecast": forecast,
        }

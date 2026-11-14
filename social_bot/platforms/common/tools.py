"""跨平台只读聊天工具；不包含任何社交写操作。"""
from dataclasses import asdict

from ...modules.weather import WeatherModule
from ...modules.research import ResearchModule
from ...modules.time import TimeModule
from ...modules.link import LinkModule


def tool(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}}}


class ReadOnlyChatTools:
    multimodal_mode = "off"
    multimodal_auto_read = False
    preferred_tool = None

    def __init__(self, runtime, text, *, weather=None, research=None, time_module=None, links=None):
        self.runtime, self.text = runtime, text
        self.weather = weather or WeatherModule(timeout_seconds=runtime.weather_timeout_seconds,
                                               provider=runtime.weather_provider, ip_cache_file=runtime.weather_cache_file)
        self.research = research or ResearchModule.from_settings(runtime)
        self.time = time_module or TimeModule()
        self.links = links or LinkModule()

    @property
    def definitions(self):
        definitions = []
        if self.runtime.allow_weather_tool:
            definitions.append(tool("weather_forecast", "查询实时天气与预报。location 为城市，留空按运行机器 IP 定位，并非聊天用户位置；只报告工具实际返回的 provider。",
                {"location": {"type": "string"}, "days": {"type": "integer", "minimum": 1, "maximum": 7}}, ["location", "days"]))
        if self.runtime.allow_research_tool:
            definitions.append(tool("research_search", "联网检索，多个搜索后端自动回退，返回实际 provider 与来源链接；全部失败必须说明。",
                {"query": {"type": "string", "maxLength": 500}, "max_results": {"type": "integer", "minimum": 1, "maximum": 8}}, ["query", "max_results"]))
        if self.runtime.allow_time_tool:
            definitions.append(tool("time_now", "查询上海时区当前时间和日期。", {}, []))
        if self.runtime.allow_link_tool:
            definitions.append(tool("link_read", "读取当前用户明确提供的网页链接；不访问模型自行编造的链接。",
                {"url": {"type": "string"}}, ["url"]))
        return definitions

    def execute(self, name, arguments):
        if name not in {item["function"]["name"] for item in self.definitions}:
            return {"success": False, "reason": "chat_tool_unavailable"}
        try:
            if name == "time_now":
                return self.time.now()
            if name == "weather_forecast":
                location = arguments.get("location", "")
                if not isinstance(location, str):
                    raise ValueError("location")
                now = self.time.now()
                return {**self.weather.forecast(location=location, days=max(1, min(7, int(arguments.get("days", 3)))),
                        use_ip=self.runtime.weather_ip_location, query_time=now["iso"]), "time_context": now}
            if name == "research_search":
                return self.research.search(arguments.get("query", ""), arguments.get("max_results", 5))
            if name == "link_read":
                url = arguments.get("url", "")
                if not isinstance(url, str) or url != LinkModule.extract_url(self.text):
                    return {"success": False, "reason": "current_message_url_required"}
                return asdict(self.links.read(url))
        except Exception as exc:
            return {"success": False, "reason": "chat_tool_error", "error_type": type(exc).__name__}

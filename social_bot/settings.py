from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DeepSeekSettings:
    base_url: str = "https://api.deepseek.com/chat/completions"
    model: str = "deepseek-chat"
    system_prompt: str = (
        "你是一个简洁、友好的聊天助手。可以自然加入 1 到 2 个 emoji，"
        "也可以使用 [微笑]、[鼓掌]、[太阳] 等微信原生表情标记，但不要每句话都加。"
    )
    timeout_seconds: int = 30
    max_tokens: int = 800
    collaboration_enabled: bool = False
    task_model: str = "deepseek-flash"
    task_timeout_seconds: int = 90
    task_max_tokens: int = 2400
    direct_simple_chat: bool = False


@dataclass(frozen=True)
class MultimodalSettings:
    """图片等多模态内容使用的 OpenAI 兼容模型配置。"""

    api_key: str = ""
    api_file: str = "api_qwen.txt"
    base_url: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
    model: str = "qwen3.8-flash"
    timeout_seconds: int = 45
    max_tokens: int = 600


@dataclass(frozen=True)
class AudioSettings:
    """Qwen ASR 接口配置。"""

    api_key: str = ""
    api_file: str = "api_audio.txt"
    base_url: str = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    model: str = "qwen-audio-3.1-asr-flash"
    timeout_seconds: int = 45
    sample_rate: int = 16000


@dataclass(frozen=True)
class VideoSettings:
    api_key: str = ""
    api_file: str = "api_audio.txt"
    base_url: str = ""
    model: str = "qwen3.8-omni-flash"
    timeout_seconds: int = 90


@dataclass(frozen=True)
class ScopeSettings:
    enabled: bool = True
    private_users: frozenset[str] = field(default_factory=frozenset)
    groups: frozenset[str] = field(default_factory=frozenset)
    group_require_mention: bool = True
    group_keywords: tuple[str, ...] = ()
    max_input_chars: int = 4000
    group_activation_enabled: bool = True
    group_activate_keyword: str = "转人机"
    group_activate_reply: str = "大肥鱼收到"
    group_deactivate_keyword: str = "转人工"
    group_deactivate_reply: str = "已转人工，机器人已停止回复。"


@dataclass(frozen=True)
class RuntimeSettings:
    database_path: str = "data/bot.sqlite3"
    agent_memory_enabled: bool = False
    agent_memory_path: str = ""
    agent_memory_retry_seconds: int = 30
    memory_account_id: str = "default"
    wechat_group_memory_enabled: bool = False
    max_context_messages: int = 12
    cooldown_seconds: int = 8
    # 微信回调只负责投递；实际消息处理最多使用 4 个并发工作线程。
    message_workers: int = 4
    # 先关闭，确认普通文字回复稳定后再打开模型工具调用。
    tool_calling_enabled: bool = False
    # 撤回是破坏性操作，默认不向模型暴露。
    allow_recall_tool: bool = False
    max_tool_rounds: int = 3
    # 工具能力的细分开关；只有 tool_calling_enabled=true 时才会生效。
    allow_weather_tool: bool = False
    allow_research_tool: bool = False
    research_backends: tuple[str, ...] = ("ddgs_duckduckgo", "baidu_api", "bocha_api")
    research_timeout_seconds: int = 6
    research_cooldown_seconds: int = 60
    research_bocha_key_env: str = "BOCHA_API_KEY"
    research_baidu_key_env: str = "BAIDU_SEARCH_API_KEY"
    research_credentials_file: str = ""
    research_domestic_direct: bool = True
    research_duckduckgo_mode: str = 'http'
    research_duckduckgo_timeout_seconds: int = 15
    research_duckduckgo_browser_channel: str = 'msedge'
    research_duckduckgo_profile: str = ''
    # 多模态读取策略：global=所有消息，mention=仅被 @ 的消息，off=关闭读取。
    multimodal_mode: str = "mention"
    # 工具默认全部关闭；接入并验证接口后再逐项打开。
    allow_ocr_tool: bool = False
    allow_speech_tool: bool = False
    allow_time_tool: bool = False
    weather_ip_location: bool = True
    weather_timeout_seconds: int = 15
    # 高德天气当前覆盖中国城市；海外地点后续再接入其他 provider。
    weather_provider: str = "amap"
    # 朋友圈写操作默认关闭，需要单独打开。
    allow_moments_publish: bool = False
    allow_moments_comment: bool = False
    # 聊天媒体发送和群管理属于外部写操作，默认关闭，需明确打开。
    allow_media_tool: bool = False
    allow_group_tools: bool = False
    # 视觉模型、文件解析、链接读取和语音发送均有独立开关。
    allow_vision_tool: bool = False
    allow_poke_tool: bool = False
    allow_document_tool: bool = False
    allow_link_tool: bool = False
    allow_audio_tool: bool = False
    allow_video_tool: bool = False
    # 聊天和引用回复是否允许模型自然加入 emoji/微信原生表情标记。
    emoji_enabled: bool = True
    emoji_max_count: int = 2
    emoji_stickers_enabled: bool = True

    @property
    def weather_cache_file(self) -> str:
        """天气 IP 定位缓存文件，和数据库放在同一个 data 目录下。"""
        try:
            return str(Path(self.database_path).with_name("weather_ip_cache.json"))
        except (OSError, ValueError):
            return "data/weather_ip_cache.json"


@dataclass(frozen=True)
class WeChatSettings:
    enabled: bool = False
    targets: tuple[str, ...] = ()
    bot_name: str = ""
    # text：普通发送；quote：引用触发回复的消息。
    reply_mode: str = "text"


@dataclass(frozen=True)
class MomentsSettings:
    """独立朋友圈运行模式的配置。"""

    enabled: bool = False
    duration_minutes: int = 120
    # false = 单次模式：发完首条（和开篇评论）就退出，不等待、不轮询评论、不发结束文案。
    wait_for_comments: bool = True
    template: str = (
        "开头是‘早上好，这里是大肥鱼’；轻松和大家打招呼，提到第一次见面、请多关照；"
        "播报今日天气（城市、天气、温度、穿衣/带伞提醒）；加一句‘如果大家想要和我互动，"
        "可以在评论区和我交流哦’；最后送上关爱祝福。全文100字以内，亲切自然，用1-2个emoji表达"
        "情绪或小心思，不要为了凑数量添加。\n"
        "参考成品：早上好，这里是大肥鱼🐟\n第一次见面，请多关照～\n"
        "今日【城市】【天气】，【温度】，出门记得【带伞/添衣】。\n"
        "如果大家想要和我互动，可以在评论区和我交流哦～\n"
        "愿大家平安顺遂，吃好睡好，天天好心情☀️"
    )
    # 配置文件中的 template_file 非空时，优先从该 UTF-8 文本文件读取 template。
    template_file: str = ""
    closing_text: str = "肥鱼累了不回消息了，睡觉去咯"
    media_files: tuple[str, ...] = ()
    privacy_config: dict[str, Any] = field(default_factory=dict)
    poll_interval_seconds: int = 20
    weather_enabled: bool = True
    weather_location: str = ""
    # comment：用官方 Comment() 在自己朋友圈下发一条普通评论（默认，最稳）；
    # reply：点击原评论后点对点回复（依赖 wxauto 暴露评论控件，真机上未必可用）。
    comment_reply_mode: str = "comment"
    # api：用 wxautox4 的 Moment.Comment()；ui：自己点「评论」按钮在界面上发。
    # 实测部分客户端上 Comment() 会返回成功但什么都没发出去，这时改用 ui。
    comment_via: str = "api"
    # 发布首条后立刻在自己评论区发的说明（走官方 Comment()），留空表示不发。
    # opening_comment_text 是固定文案；opening_comment_template 是让模型写文案的提示词。
    # 两者都配置时以固定文案为准。
    opening_comment_text: str = ""
    opening_comment_template: str = ""
    max_content_chars: int = 100
    emoji_enabled: bool = True
    emoji_max_count: int = 2
    emoji_stickers_enabled: bool = True
    comment_reply_template: str = (
        "你在自己刚发布的朋友圈下收到了一条评论：\n"
        "{comment}\n"
        "请生成一句自然、简短、友好的回复，只返回回复正文，不要解释，不要使用 Markdown。"
    )


@dataclass(frozen=True)
class AppSettings:
    api_key: str
    deepseek: DeepSeekSettings
    scope: ScopeSettings
    runtime: RuntimeSettings
    wechat: WeChatSettings
    multimodal: MultimodalSettings = field(default_factory=MultimodalSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    moments: MomentsSettings = field(default_factory=MomentsSettings)
    video: VideoSettings = field(default_factory=VideoSettings)


def _as_string_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError("范围配置必须是数组")
    return frozenset(str(item).strip() for item in value if str(item).strip())


def _read_api_key(api_file: Path) -> str:
    if not api_file.exists():
        raise FileNotFoundError(f"找不到 API 密钥文件：{api_file}")
    for line in api_file.read_text(encoding="utf-8").splitlines():
        key = line.strip()
        if key and not key.startswith("#"):
            return key
    raise ValueError(f"API 密钥文件为空：{api_file}")


def _read_multimodal_credentials(api_file: Path) -> tuple[str, str]:
    """从 Token Plan 文本文件中读取 key 和兼容 API 地址。"""
    if not api_file.exists():
        return "", ""
    text = api_file.read_text(encoding="utf-8", errors="replace")
    import re

    keys = re.findall(r"sk-[A-Za-z0-9._-]+", text)
    urls = re.findall(r"https://[^\s\r\n]+", text)
    key = max(keys, key=len) if keys else ""
    base_url = urls[0].rstrip("`'\"") if urls else ""
    if base_url.endswith("/compatible-mode/v1"):
        base_url += "/chat/completions"
    return key, base_url


def _read_audio_credentials(api_file: Path) -> tuple[str, str]:
    """读取 ASR key 和 DashScope 多模态生成地址。"""
    if not api_file.exists():
        return "", ""
    text = api_file.read_text(encoding="utf-8", errors="replace")
    import re

    keys = re.findall(r"sk-[A-Za-z0-9._-]+", text)
    urls = [url.rstrip("`'\"") for url in re.findall(r"https?://[^\s\r\n]+", text)]
    endpoint = next((url for url in urls if "/api/v1/services/" in url), "")
    if not endpoint:
        compatible = next((url for url in urls if "/compatible-mode/v1" in url), "")
        if compatible:
            endpoint = compatible.split("/compatible-mode/v1", 1)[0] + "/api/v1/services/aigc/multimodal-generation/generation"
    return (max(keys, key=len) if keys else ""), endpoint


def load_settings(config_path: str | Path = "config.json") -> AppSettings:
    path = Path(config_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    base_dir = path.parent
    api_file = Path(raw.get("api_file", "api_deepseek.txt"))
    if not api_file.is_absolute():
        api_file = base_dir / api_file

    model_raw = raw.get("deepseek", {})
    scope_raw = raw.get("scope", {})
    runtime_raw = raw.get("runtime", {})
    wechat_raw = raw.get("wechat", {})
    moments_raw = raw.get("moments", {})
    multimodal_raw = raw.get("multimodal", {})
    audio_raw = raw.get("audio", {})
    video_raw = raw.get("video", {})

    deepseek = DeepSeekSettings(
        base_url=str(model_raw.get("base_url", DeepSeekSettings.base_url)),
        model=str(model_raw.get("model", DeepSeekSettings.model)),
        system_prompt=str(model_raw.get("system_prompt", DeepSeekSettings.system_prompt)),
        timeout_seconds=max(1, int(model_raw.get("timeout_seconds", 30))),
        max_tokens=max(1, int(model_raw.get("max_tokens", 800))),
        collaboration_enabled=bool(model_raw.get('collaboration_enabled', False)),
        task_model=str(model_raw.get('task_model', 'deepseek-flash')),
        task_timeout_seconds=max(1, int(model_raw.get('task_timeout_seconds', 90))),
        task_max_tokens=max(1, int(model_raw.get('task_max_tokens', 2400))),
        direct_simple_chat=bool(model_raw.get('direct_simple_chat', False)),
    )
    multimodal_api_file = Path(multimodal_raw.get("api_file", "api_qwen.txt"))
    if not multimodal_api_file.is_absolute():
        multimodal_api_file = base_dir / multimodal_api_file
    multimodal_key, discovered_base_url = _read_multimodal_credentials(multimodal_api_file)
    multimodal = MultimodalSettings(
        api_key=str(multimodal_raw.get("api_key", multimodal_key)),
        api_file=str(multimodal_raw.get("api_file", "api_qwen.txt")),
        base_url=str(multimodal_raw.get("base_url", discovered_base_url or MultimodalSettings.base_url)),
        model=str(multimodal_raw.get("model", MultimodalSettings.model)),
        timeout_seconds=max(1, int(multimodal_raw.get("timeout_seconds", 45))),
        max_tokens=max(1, int(multimodal_raw.get("max_tokens", 600))),
    )
    audio_api_file = Path(audio_raw.get("api_file", "api_audio.txt"))
    if not audio_api_file.is_absolute():
        audio_api_file = base_dir / audio_api_file
    audio_key, discovered_audio_url = _read_audio_credentials(audio_api_file)
    audio = AudioSettings(
        api_key=str(audio_raw.get("api_key", audio_key)),
        api_file=str(audio_raw.get("api_file", "api_audio.txt")),
        base_url=str(audio_raw.get("base_url", discovered_audio_url or AudioSettings.base_url)),
        model=str(audio_raw.get("model", AudioSettings.model)),
        timeout_seconds=max(1, int(audio_raw.get("timeout_seconds", 45))),
        sample_rate=max(8000, int(audio_raw.get("sample_rate", 16000))),
    )
    scope = ScopeSettings(
        enabled=bool(scope_raw.get("enabled", True)),
        private_users=_as_string_set(scope_raw.get("private_users", [])),
        groups=_as_string_set(scope_raw.get("groups", [])),
        group_require_mention=bool(scope_raw.get("group_require_mention", True)),
        group_keywords=tuple(str(x).strip() for x in scope_raw.get("group_keywords", []) if str(x).strip()),
        max_input_chars=max(1, int(scope_raw.get("max_input_chars", 4000))),
        group_activation_enabled=bool(scope_raw.get("group_activation_enabled", True)),
        group_activate_keyword=str(scope_raw.get("group_activate_keyword", "转人机")).strip(),
        group_activate_reply=str(scope_raw.get("group_activate_reply", "大肥鱼收到")),
        group_deactivate_keyword=str(scope_raw.get("group_deactivate_keyword", "转人工")).strip(),
        group_deactivate_reply=str(scope_raw.get("group_deactivate_reply", "已转人工，机器人已停止回复。")),
    )
    weather_provider = str(runtime_raw.get("weather_provider", "amap")).strip().lower().replace("-", "_")
    if weather_provider not in {"amap", "tianqi", "open_meteo", "qweather"}:
        raise ValueError("runtime.weather_provider 必须是 amap、tianqi、open_meteo 或 qweather")
    multimodal_mode = str(runtime_raw.get("multimodal_mode", "mention")).strip().lower()
    multimodal_mode = {
        "global": "global",
        "all": "global",
        "全局": "global",
        "mention": "mention",
        "at": "mention",
        "@": "mention",
        "引用": "mention",
        "off": "off",
        "disabled": "off",
        "关闭": "off",
    }.get(multimodal_mode, multimodal_mode)
    if multimodal_mode not in {"global", "mention", "off"}:
        raise ValueError("runtime.multimodal_mode 必须是 global、mention 或 off")
    video_file = Path(video_raw.get('api_file', audio_raw.get('api_file', 'api_audio.txt')))
    if not video_file.is_absolute():
        video_file = base_dir / video_file
    video_key, video_url = _read_audio_credentials(video_file)
    video = VideoSettings(
        api_key=str(video_raw.get('api_key', video_key or audio.api_key)),
        api_file=str(video_raw.get('api_file', audio_raw.get('api_file', 'api_audio.txt'))),
        base_url=str(video_raw.get('base_url', video_url or audio.base_url)),
        model=str(video_raw.get('model', 'qwen3.8-omni-flash')),
        timeout_seconds=max(1, min(120, int(video_raw.get('timeout_seconds', 90)))),
    )
    from .modules.research.module import DEFAULT_BACKENDS, BACKENDS
    research_backends = runtime_raw.get("research_backends", list(DEFAULT_BACKENDS))
    if not isinstance(research_backends, list) or not research_backends or any(not isinstance(item, str) or item not in BACKENDS for item in research_backends):
        raise ValueError("runtime.research_backends 必须是支持的搜索后端名称数组，且不能为空")
    domestic_direct = runtime_raw.get('research_domestic_direct', True)
    if not isinstance(domestic_direct, bool):
        raise ValueError('runtime.research_domestic_direct 必须是布尔值')
    duck_mode = runtime_raw.get('research_duckduckgo_mode', 'http')
    duck_channel = runtime_raw.get('research_duckduckgo_browser_channel', 'msedge')
    if duck_mode not in {'http', 'browser'} or duck_channel not in {'msedge', 'chrome'}:
        raise ValueError('DuckDuckGo mode 须为 http/browser，浏览器须为 msedge/chrome')
    duck_profile = str(runtime_raw.get('research_duckduckgo_profile', '')).strip()
    if not duck_profile:
        duck_profile = str(Path(runtime_raw.get('database_path', 'data/bot.sqlite3')).parent/'duckduckgo-browser-profile')
    duck_candidate = Path(duck_profile).expanduser()
    duck_profile = str((base_dir/duck_candidate).resolve() if not duck_candidate.is_absolute() else duck_candidate.resolve())
    search_credentials = str(runtime_raw.get('research_credentials_file', 'search_credentials.json')).strip()
    if search_credentials:
        candidate = Path(search_credentials).expanduser()
        search_credentials = str((base_dir/candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
    agent_enabled = runtime_raw.get('agent_memory_enabled', False)
    if not isinstance(agent_enabled, bool):
        raise ValueError('runtime.agent_memory_enabled 必须是布尔值')
    agent_path = str(runtime_raw.get('agent_memory_path', '')).strip()
    if agent_enabled and not agent_path:
        raise ValueError('启用共享记忆需填写 runtime.agent_memory_path')
    if agent_path:
        candidate = Path(agent_path).expanduser()
        agent_path = str((base_dir/candidate).resolve() if not candidate.is_absolute() else candidate.resolve())
    agent_retry = runtime_raw.get('agent_memory_retry_seconds', 30)
    if type(agent_retry) is not int or not 0 <= agent_retry <= 3600:
        raise ValueError('runtime.agent_memory_retry_seconds 必须是 0～3600 的整数，0 表示关闭定时补同步')
    runtime = RuntimeSettings(
        database_path=str(runtime_raw.get("database_path", "data/bot.sqlite3")),
        agent_memory_enabled=agent_enabled,
        agent_memory_path=agent_path,
        agent_memory_retry_seconds=agent_retry,
        memory_account_id=str(runtime_raw.get('memory_account_id','default')),
        wechat_group_memory_enabled=bool(runtime_raw.get("wechat_group_memory_enabled", False)),
        max_context_messages=max(1, int(runtime_raw.get("max_context_messages", 12))),
        cooldown_seconds=max(0, int(runtime_raw.get("cooldown_seconds", 8))),
        message_workers=max(1, min(4, int(runtime_raw.get("message_workers", 4)))),
        tool_calling_enabled=bool(runtime_raw.get("tool_calling_enabled", False)),
        allow_recall_tool=bool(runtime_raw.get("allow_recall_tool", False)),
        max_tool_rounds=max(1, min(8, int(runtime_raw.get("max_tool_rounds", 3)))),
        allow_weather_tool=bool(runtime_raw.get("allow_weather_tool", False)),
        allow_research_tool=bool(runtime_raw.get("allow_research_tool", False)),
        research_backends=tuple(dict.fromkeys(research_backends)),
        research_timeout_seconds=max(1, min(30, int(runtime_raw.get("research_timeout_seconds", 6)))),
        research_cooldown_seconds=max(0, min(3600, int(runtime_raw.get("research_cooldown_seconds", 60)))),
        research_bocha_key_env=str(runtime_raw.get('research_bocha_key_env', 'BOCHA_API_KEY')).strip(),
        research_baidu_key_env=str(runtime_raw.get('research_baidu_key_env', 'BAIDU_SEARCH_API_KEY')).strip(),
        research_credentials_file=search_credentials,
        research_domestic_direct=domestic_direct,
        research_duckduckgo_mode=duck_mode,
        research_duckduckgo_timeout_seconds=max(1, min(15, int(runtime_raw.get('research_duckduckgo_timeout_seconds', 15)))),
        research_duckduckgo_browser_channel=duck_channel,
        research_duckduckgo_profile=duck_profile,
        multimodal_mode=multimodal_mode,
        allow_ocr_tool=bool(runtime_raw.get("allow_ocr_tool", False)),
        allow_speech_tool=bool(runtime_raw.get("allow_speech_tool", False)),
        allow_time_tool=bool(runtime_raw.get("allow_time_tool", False)),
        weather_ip_location=bool(runtime_raw.get("weather_ip_location", True)),
        weather_timeout_seconds=max(3, min(60, int(runtime_raw.get("weather_timeout_seconds", 15)))),
        weather_provider=weather_provider,
        allow_moments_publish=bool(runtime_raw.get("allow_moments_publish", False)),
        allow_moments_comment=bool(runtime_raw.get("allow_moments_comment", False)),
        allow_media_tool=bool(
            runtime_raw.get("allow_media_tool", runtime_raw.get("allow_send_media_tool", False))
        ),
        allow_group_tools=bool(
            runtime_raw.get("allow_group_tools", runtime_raw.get("allow_group_management_tool", False))
        ),
        allow_vision_tool=bool(runtime_raw.get("allow_vision_tool", False)),
        allow_poke_tool=bool(runtime_raw.get("allow_poke_tool", False)),
        allow_document_tool=bool(runtime_raw.get("allow_document_tool", False)),
        allow_link_tool=bool(runtime_raw.get("allow_link_tool", False)),
        allow_audio_tool=bool(runtime_raw.get("allow_audio_tool", False)),
        allow_video_tool=bool(runtime_raw.get("allow_video_tool", False)),
        emoji_enabled=bool(runtime_raw.get("emoji_enabled", True)),
        emoji_max_count=max(0, min(5, int(runtime_raw.get("emoji_max_count", 2)))),
        emoji_stickers_enabled=bool(runtime_raw.get("emoji_stickers_enabled", True)),
    )
    targets = wechat_raw.get("targets", [])
    if not isinstance(targets, list):
        raise ValueError("微信监听目标必须是数组")
    reply_mode = str(wechat_raw.get("reply_mode", "text")).strip().lower()
    if reply_mode not in {"text", "quote"}:
        raise ValueError("微信回复模式必须是 text 或 quote")
    wechat = WeChatSettings(
        enabled=bool(wechat_raw.get("enabled", False)),
        targets=tuple(str(x).strip() for x in targets if str(x).strip()),
        bot_name=str(wechat_raw.get("bot_name", "")).strip(),
        reply_mode=reply_mode,
    )
    media_files = moments_raw.get("media_files", [])
    if not isinstance(media_files, list):
        raise ValueError("朋友圈媒体文件必须是数组")
    privacy_config = moments_raw.get("privacy_config", {})
    if not isinstance(privacy_config, dict):
        raise ValueError("朋友圈隐私配置必须是对象")
    comment_reply_mode = str(moments_raw.get("comment_reply_mode", "comment")).strip().lower()
    if comment_reply_mode not in {"comment", "reply"}:
        raise ValueError("moments.comment_reply_mode 必须是 comment 或 reply")
    comment_via = str(moments_raw.get("comment_via", "api")).strip().lower()
    if comment_via not in {"api", "ui"}:
        raise ValueError("moments.comment_via 必须是 api 或 ui")
    template_file = str(moments_raw.get("template_file", "")).strip()
    template_text = str(moments_raw.get("template", MomentsSettings.template)).strip()
    if template_file:
        template_path = Path(template_file).expanduser()
        if not template_path.is_absolute():
            template_path = base_dir / template_path
        if not template_path.is_file():
            raise ValueError(f"朋友圈模板文件不存在：{template_path}")
        try:
            template_text = template_path.read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise ValueError(f"朋友圈模板文件读取失败：{template_path}: {exc}") from exc
    moments = MomentsSettings(
        enabled=bool(moments_raw.get("enabled", False)),
        wait_for_comments=bool(moments_raw.get("wait_for_comments", True)),
        duration_minutes=max(1, min(24 * 60, int(moments_raw.get("duration_minutes", 120)))),
        template=template_text,
        template_file=template_file,
        closing_text=str(moments_raw.get("closing_text", MomentsSettings.closing_text)).strip(),
        media_files=tuple(str(item).strip() for item in media_files if str(item).strip()),
        privacy_config=dict(privacy_config),
        poll_interval_seconds=max(5, min(300, int(moments_raw.get("poll_interval_seconds", 20)))),
        weather_enabled=bool(moments_raw.get("weather_enabled", True)),
        weather_location=str(moments_raw.get("weather_location", "")).strip(),
        comment_reply_mode=comment_reply_mode,
        comment_via=comment_via,
        opening_comment_text=str(moments_raw.get("opening_comment_text", "")).strip(),
        opening_comment_template=str(moments_raw.get("opening_comment_template", "")).strip(),
        max_content_chars=max(20, min(2000, int(moments_raw.get("max_content_chars", 100)))),
        emoji_enabled=bool(moments_raw.get("emoji_enabled", True)),
        emoji_max_count=max(0, min(5, int(moments_raw.get("emoji_max_count", 2)))),
        emoji_stickers_enabled=bool(moments_raw.get("emoji_stickers_enabled", True)),
        comment_reply_template=str(
            moments_raw.get("comment_reply_template", MomentsSettings.comment_reply_template)
        ).strip(),
    )
    if not moments.template:
        raise ValueError("朋友圈模板不能为空")
    if not moments.closing_text:
        raise ValueError("朋友圈结束文案不能为空")
    if not moments.comment_reply_template:
        raise ValueError("朋友圈评论回复模板不能为空")
    return AppSettings(
        api_key=_read_api_key(api_file),
        deepseek=deepseek,
        scope=scope,
        runtime=runtime,
        wechat=wechat,
        multimodal=multimodal,
        audio=audio,
        moments=moments,
        video=video,
    )

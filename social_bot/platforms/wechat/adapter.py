from __future__ import annotations

import collections
import concurrent.futures
import logging
import threading
import time
from typing import Any, Iterable

from ...modules.quote import QuoteModule, QuoteOutcome
from ...modules.recall import RecallModule, RecallOutcome
from ...modules.text_reply import TextReplyModule
from ...modules.ocr import OCRModule
from ...modules.research import ResearchModule
from ...modules.speech import SpeechModule
from ...modules.time import TimeModule
from ...modules.weather import WeatherModule
from ...modules.moments import MomentsModule
from ...modules.media import MediaModule
from ...modules.group import GroupModule, GroupOutcome
from ...modules.vision import VisionModule
from ...modules.poke import PokeModule
from ...modules.document import DocumentModule
from ...modules.link import LinkModule
from ...modules.audio import AudioModule
from ...modules.video import VideoModule, OmniVideoAnalyzer
from ...dispatch import ChatOutputQueue
from ...message_types import canonical_message_type, infer_message_type
from ...service import BotService
from ...settings import WeChatSettings
from .tools import WeChatToolExecutor

LOGGER = logging.getLogger(__name__)

# wxauto4（免费版）与 wxautox4（Plus 版）导出同一套 API，但支持范围不同：
# 免费版 wxauto4 41.1.7 最高支持微信客户端 4.1.8.107，Plus 版跟随最新客户端。
# 因此这里优先使用 Plus 版；只装了免费版时自动回退。
CLIENT_PACKAGES = ("wxautox4", "wxauto4")

# wxauto4 把一条消息拆成两个属性：
#   attr：来源，system / self / friend / other
#   type：内容类型，text / image / quote / time / ...
SOURCE_ATTRS = ("attr", "msg_attr", "from_type")
CONTENT_TYPES = ("type", "msg_type")

# 系统消息（含时间消息）一律不处理。
_IGNORED_SOURCES = {"system"}
# 非消息内容类型，同样跳过。
_IGNORED_TYPES = {"system", "sys", "notice", "out", "recall", "time"}
# wxauto 3.9 的老语义里，「自己发的」标记出现在 type 字段；单独识别以便统一走 self 逻辑。
_LEGACY_SOURCE_TYPES = {"self"}

# 群聊标志值；chat.chat_type 的取值是 friend / group / service / official。
_GROUP_VALUES = {"group", "groupchat", "chatroom"}

# wxauto 打开独立聊天窗口时会偶发 (1400, 'MoveWindow', '无效的窗口句柄')——
# 实测属于瞬时竞态，重试即可；重试仍失败也不该拖垮其它目标。
LISTEN_ATTEMPTS = 3
LISTEN_RETRY_DELAY = 1.0


def _first_attr(obj: Any, names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _is_failed_response(result: Any) -> bool:
    """判断 wxauto 的 WxResponse 是否表示失败。

    WxResponse 是 dict 子类，status 取值为 成功/失败/错误。
    注意 Plus 版的 AddListenChat 成功时同样返回 WxResponse，所以不能只看类型，
    必须看 status，否则会把成功误判成失败。
    """
    return isinstance(result, dict) and result.get("status") not in (None, "成功")


def _load_wechat_class() -> Any:
    """按优先级导入 WeChat 类：Plus 版优先，免费版兜底。"""
    problems: list[str] = []
    for package in CLIENT_PACKAGES:
        try:
            module = __import__(package, fromlist=["WeChat"])
        except ImportError as exc:
            problems.append(f"{package} 不可用（{exc}）")
            continue
        wechat_class = getattr(module, "WeChat", None)
        if wechat_class is None:
            problems.append(f"{package} 未导出 WeChat")
            continue
        LOGGER.info("使用 %s 作为微信客户端适配包", package)
        return wechat_class
    raise RuntimeError("未安装 wxauto4 或 wxautox4，请先安装其中一个兼容包：" + "；".join(problems))


def _try_add_listen_chat(client: Any, target: str, callback: Any) -> str | None:
    """给一个目标挂监听，失败自动重试。

    成功返回 None，失败返回原因字符串。这里只返回原因而不抛异常，是为了让单个目标
    失败不要拖垮其它目标——4 个群里 1 个抽风，其余 3 个应该继续正常工作。
    """
    reason = ""
    for attempt in range(1, LISTEN_ATTEMPTS + 1):
        try:
            result = client.AddListenChat(target, callback)
        except Exception as exc:  # noqa: BLE001 - UI 自动化瞬时失败，值得重试
            reason = f"{type(exc).__name__}: {exc}"
        else:
            # 免费版成功时返回 Chat 对象，Plus 版成功时返回 status=成功 的 WxResponse；
            # 两者失败时都返回 status=失败/错误 的 WxResponse。
            if not _is_failed_response(result):
                return None
            reason = str(result.get("message") or result)
        if attempt < LISTEN_ATTEMPTS:
            LOGGER.warning(
                "监听会话「%s」第 %s 次失败（%s），%.1f 秒后重试",
                target, attempt, reason, LISTEN_RETRY_DELAY,
            )
            time.sleep(LISTEN_RETRY_DELAY)
    return reason


# wxauto 为每个监听对象打开的「独立聊天窗口」会继承微信主窗口的尺寸。
# 主窗口被收进托盘或尺寸过小时，独立窗口会小到渲染不出消息气泡，于是 wxauto
# 读不到别人的消息发送者、把消息整体丢掉；而自己发的消息靠右侧、不需要读发送者，
# 所以照常通过——现象是「群友 @ 机器人毫无反应，但自己发控制词却正常」，极具迷惑性。
MAIN_WINDOW_CLASS = "Qt51514QWindowIcon"
MAIN_WINDOW_TITLES = ("微信", "Weixin")
MIN_MAIN_WINDOW_SIZE = (1200, 800)
# 每个会话的独立聊天窗口也要够大，否则同样渲染不出消息。
MIN_CHAT_WINDOW_SIZE = (1200, 900)


def _weixin_qt_windows(win32gui: Any) -> list[int]:
    """列出 Weixin.exe 的所有 Qt 顶层窗口句柄。"""
    handles: list[int] = []

    def collect(hwnd: Any, _: Any) -> bool:
        try:
            if win32gui.GetClassName(hwnd) == MAIN_WINDOW_CLASS:
                handles.append(hwnd)
        except Exception:  # noqa: BLE001
            pass
        return True

    win32gui.EnumWindows(collect, None)
    return handles


def ensure_chat_windows_ready(targets: Iterable[str]) -> None:
    """确保每个被监听会话的独立聊天窗口未最小化、且尺寸够大。

    wxauto 会复用上次遗留的独立窗口（日志里的「获取到已存在的子窗口」）。这类窗口
    如果尺寸过小或被最小化，微信就不会在里面渲染消息气泡，wxauto 便读不到**别人**
    消息的发送者，把整条消息丢掉——表现为「某个群或某个人永远不回复」，而自己的消息
    因为靠右侧、不需要读发送者，照常通过。

    只处理标题命中 targets 的窗口，不碰用户自己打开的其它聊天窗口。
    """
    wanted = {str(t).strip() for t in targets if str(t).strip()}
    if not wanted:
        return
    try:
        import win32con
        import win32gui
    except ImportError:  # pragma: no cover - pywin32 随 wxauto 一起安装
        return

    try:
        handles = _weixin_qt_windows(win32gui)
    except Exception:  # noqa: BLE001
        LOGGER.debug("枚举聊天窗口失败，跳过", exc_info=True)
        return

    min_width, min_height = MIN_CHAT_WINDOW_SIZE
    for hwnd in handles:
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:  # noqa: BLE001
            continue
        if title not in wanted:
            continue
        try:
            if win32gui.IsIconic(hwnd):
                LOGGER.warning("会话「%s」的独立窗口处于最小化状态，正在还原", title)
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top
            if width >= min_width and height >= min_height:
                continue
            LOGGER.warning(
                "会话「%s」的独立窗口仅 %sx%s，过小会导致读不到别人的消息，正在调整为 %sx%s",
                title, width, height, min_width, min_height,
            )
            win32gui.MoveWindow(hwnd, max(left, 40), max(top, 40), min_width, min_height, True)
        except Exception:  # noqa: BLE001
            LOGGER.debug("调整会话窗口失败 title=%s", title, exc_info=True)


def ensure_main_window_ready() -> None:
    """启动前让微信窗口处于可被 UI Automation 正确读取的状态。

    有两种情况会让 wxauto 读不到消息、把别人的消息静默丢弃（自己的消息不受影响）：
      1. 窗口被**最小化**——最小化窗口不渲染任何控件。注意最小化窗口的 GetWindowRect
         会呈现 -32000 偏移、算出来恒为 160x28 的占位值，很容易被误读成「窗口太小」。
      2. 主窗口尺寸过小——wxauto 为监听对象打开的独立聊天窗口会继承主窗口尺寸。

    因此这里先把所有被最小化的微信窗口还原（含 wxauto 上次遗留的聊天独立窗口，
    它们的标题是会话名而不是「微信」，光看主窗口会漏掉），再保证主窗口尺寸够大。

    尽力而为：任何一步失败都只记日志，不阻断启动。
    """
    try:
        import win32con
        import win32gui
    except ImportError:  # pragma: no cover - pywin32 随 wxauto 一起安装
        LOGGER.debug("未安装 pywin32，跳过微信窗口状态检查")
        return

    candidates: list[int] = []
    try:
        candidates = _weixin_qt_windows(win32gui)
    except Exception:  # noqa: BLE001
        LOGGER.debug("枚举微信窗口失败，跳过窗口状态检查", exc_info=True)
        return

    # 1) 还原所有被最小化的窗口
    for hwnd in candidates:
        try:
            if not win32gui.IsIconic(hwnd):
                continue
            title = win32gui.GetWindowText(hwnd) or "无标题"
            LOGGER.warning("微信窗口「%s」处于最小化状态，正在还原；最小化会导致监听读不到消息", title)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:  # noqa: BLE001
            LOGGER.debug("还原微信窗口失败，跳过", exc_info=True)

    # 2) 确保主窗口尺寸够大
    target = 0
    for hwnd in candidates:
        try:
            if win32gui.GetWindowText(hwnd) in MAIN_WINDOW_TITLES:
                target = hwnd
                break
        except Exception:  # noqa: BLE001
            continue
    if not target:
        LOGGER.debug("未找到微信主窗口，跳过尺寸检查")
        return

    try:
        left, top, right, bottom = win32gui.GetWindowRect(target)
        width, height = right - left, bottom - top
        visible = bool(win32gui.IsWindowVisible(target))
        min_width, min_height = MIN_MAIN_WINDOW_SIZE
        if visible and width >= min_width and height >= min_height:
            return
        LOGGER.warning(
            "微信主窗口当前 %sx%s（visible=%s），过小会导致监听窗口渲染不出消息、"
            "别人的消息被静默丢弃。正在调整为至少 %sx%s",
            width, height, visible, min_width, min_height,
        )
        win32gui.ShowWindow(target, win32con.SW_SHOWNORMAL)
        win32gui.MoveWindow(
            target, max(left, 40), max(top, 40), max(width, min_width), max(height, min_height), True
        )
    except Exception:  # noqa: BLE001
        LOGGER.debug("调整微信主窗口失败，跳过", exc_info=True)


def normalize_wechat_message(
    msg: Any, chat: Any, bot_name: str = "", accept_self_if: Any = None
) -> dict | None:
    """把 wxauto4/wxautox4 的消息对象转换成统一消息格式。

    「是不是自己发的」看 msg.attr，「是不是群聊」看 chat.chat_type。
    早期 wxauto 3.9 把来源放在 msg.type 里，这里一并兼容。

    accept_self_if 是可选的判定函数 (content) -> bool：
      - 默认 None：一律忽略自己发的消息。这是防止机器人回复自己造成刷屏的关键。
      - 传入后：只有返回 True 的自己消息才保留，用于「主人用控制词操作机器人」。
    """
    source = _first_attr(msg, SOURCE_ATTRS).lower()
    msg_type = infer_message_type(msg) or canonical_message_type(_first_attr(msg, CONTENT_TYPES))
    # 兼容旧语义：来源值可能出现在 type 字段里
    if not source and msg_type in _LEGACY_SOURCE_TYPES:
        source = msg_type
    if source in _IGNORED_SOURCES or msg_type in _IGNORED_TYPES:
        return None

    content = _first_attr(msg, ("content", "text"))
    if not content and msg_type == "image":
        content = "[图片消息]"
    elif not content and msg_type == "voice":
        content = "[语音消息]"
    elif not content and msg_type == "file":
        content = "[文件消息]"
    elif not content and msg_type == 'video':
        content = '[视频消息]'
    elif not content and msg_type == "link":
        content = "[链接卡片消息]"
    elif not content and msg_type == "note":
        content = "[文章卡片消息]"
    elif not content and msg_type == "poke":
        content = "[拍一拍消息]"
    if not content:
        return None

    if source == "self" and not (accept_self_if and accept_self_if(content)):
        return None

    chat_name = _first_attr(chat, ("who", "nickname", "name"), default=str(chat).strip())
    if not chat_name:
        return None

    chat_kind = _first_attr(chat, ("chat_type", "type")).lower()
    is_group = bool({chat_kind, msg_type, source} & _GROUP_VALUES)
    chat_type = "group" if is_group else "private"

    if is_group:
        # 群里必须区分具体是谁发的
        sender = _first_attr(msg, ("sender", "sender_name", "from_user"), default=chat_name)
    else:
        # 私聊里能发言的只有对方（自己发的消息已被上面的 self 过滤掉），
        # 直接用会话名更可靠：部分版本里 msg.sender 会是空值或 'self'/'friend' 这类占位值，
        # 那会让 private_users 白名单填对方名字也永远对不上。
        sender = chat_name
    message_id = _first_attr(msg, ("id", "msg_id", "hash", "message_id"))
    if not message_id:
        message_id = f"{chat_name}:{msg_type or source}:{content}"

    explicit_mention = getattr(msg, "is_mentioned", None)
    if explicit_mention is None:
        if bot_name:
            # wxauto 不提供 is_mentioned，只能从文本判断。必须匹配「@机器人名」——
            # 微信里 @ 会渲染成「@鲲」(后跟一个 U+2005)，所以这个判断是可靠的。
            # 不能用「消息里有 @ 就算」，否则群里别人互相 @ 也会被当成在叫机器人。
            is_mentioned = f"@{bot_name}" in content or bot_name in content
        else:
            is_mentioned = "@" in content
    else:
        is_mentioned = bool(explicit_mention)

    return {
        "platform": "wechat",
        "chat_type": chat_type,
        "chat_id": chat_name,
        "sender_id": sender,
        "sender_name": sender,
        "text": content,
        "message_id": message_id,
        "is_mentioned": is_mentioned,
        "message_type": msg_type or source or "text",
    }


class WeChatAdapter:
    """wxauto4/wxautox4 的适配层和消息模块组合器。"""

    def __init__(self, service: BotService, settings: WeChatSettings) -> None:
        self.service = service
        self.settings = settings
        self.client: Any = None
        runtime = getattr(getattr(service, "settings", None), "runtime", None)
        raw_workers = getattr(runtime, "message_workers", 4)
        try:
            message_workers = max(1, min(4, int(raw_workers)))
        except (TypeError, ValueError):
            message_workers = 4
        self._message_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=message_workers,
            thread_name_prefix="wechat-message",
        )
        self._executor_closed = False
        self._dispatch_lock = threading.RLock()
        self._inflight_message_keys: set[str] = set()
        self._recent_message_keys: set[str] = set()
        self._recent_message_order: collections.deque[str] = collections.deque(maxlen=4096)
        self._message_state_lock = threading.RLock()
        self._output_queue = ChatOutputQueue(self._chat_key)
        # 启动时挂监听失败的目标 (名称, 原因)，供上层提示用户。
        self.failed_targets: tuple[tuple[str, str], ...] = ()
        # 记录本会话已发出的回复原文，用于认出并跳过机器人自己的消息（防自回环）。
        self._sent_replies: collections.deque[str] = collections.deque(maxlen=100)
        # 只保存最近一条自己消息，给受控撤回工具使用；不把任意他人消息交给撤回接口。
        self._last_self_messages: dict[str, Any] = {}
        self._last_images: dict[str, Any] = {}
        self._message_chats: dict[int, Any] = {}
        self.recall_module = RecallModule()
        self.quote_module = QuoteModule()
        # 这些模块在适配器生命周期内复用，天气模块可缓存 IP 定位结果。
        raw_weather_timeout = getattr(runtime, "weather_timeout_seconds", 15)
        try:
            weather_timeout = int(raw_weather_timeout) if isinstance(raw_weather_timeout, (int, float, str)) else 15
        except (TypeError, ValueError):
            weather_timeout = 15
        raw_weather_provider = getattr(runtime, "weather_provider", "amap")
        weather_provider = (
            raw_weather_provider.strip().lower().replace("-", "_")
            if isinstance(raw_weather_provider, str) and raw_weather_provider.strip()
            else "open_meteo"
        )
        raw_weather_cache_file = getattr(runtime, "weather_cache_file", "")
        weather_cache_file = raw_weather_cache_file if isinstance(raw_weather_cache_file, str) else ""
        self.weather_module = WeatherModule(
            timeout_seconds=weather_timeout,
            provider=weather_provider,
            ip_cache_file=weather_cache_file,
        )
        LOGGER.info("天气模块 provider=%s", weather_provider)
        self.research_module = ResearchModule.from_settings(runtime)
        self.ocr_module = OCRModule()
        multimodal_settings = getattr(getattr(service, "settings", None), "multimodal", None)
        audio_settings = getattr(getattr(service, "settings", None), "audio", None)
        self.speech_module = SpeechModule.from_settings(audio_settings)
        self.media_module = MediaModule()
        self.group_module = GroupModule()
        from .announcement import GroupAnnouncementModule
        from pathlib import Path
        announcement_db=getattr(runtime,'database_path','data/bot.sqlite3')
        if not isinstance(announcement_db,(str,Path)):
            announcement_db='data/bot.sqlite3'
        self.announcement_module = GroupAnnouncementModule(
            task_client=getattr(service,'client',None),
            backup_dir=Path(announcement_db).parent/'announcement-backups')
        self.vision_module = VisionModule.from_settings(multimodal_settings)
        self.poke_module = PokeModule()
        from ...deepseek import DeepSeekClient
        task_client=service.client if isinstance(getattr(service,'client',None),DeepSeekClient) else None
        self.document_module = DocumentModule.from_settings(multimodal_settings,task_client=task_client)
        self.link_module = LinkModule()
        self.audio_module = AudioModule()
        self.video_module = VideoModule(OmniVideoAnalyzer.from_settings(getattr(service, 'settings', None)))
        self.time_module = TimeModule()
        # 朋友圈窗口按需打开；getter 延迟到真正调用时，避免启动监听时改变微信界面。
        self.moments_module = MomentsModule(lambda: self.client)
        self.text_reply_module = TextReplyModule(
            service=service,
            bot_name=settings.bot_name,
            normalize=normalize_wechat_message,
            accept_self_if=self._accept_self_message,
            sent_replies=self._sent_replies,
            send_reply=self._send_reply,
            tool_executor_factory=self._build_tool_executor,
        )

    @staticmethod
    def _chat_key(chat: Any) -> str:
        return _first_attr(chat, ("who", "nickname", "name"), default=str(chat).strip())

    def _remember_message(self, message: Any, chat: Any) -> None:
        """登记当前回调消息；只把来源为 self 的消息作为撤回候选。"""
        source = _first_attr(message, SOURCE_ATTRS).lower()
        msg_type = _first_attr(message, CONTENT_TYPES).lower()
        if not source and msg_type in _LEGACY_SOURCE_TYPES:
            source = msg_type
        content = _first_attr(message, ("content", "text"))
        chat_key = self._chat_key(chat)
        with self._message_state_lock:
            self._message_chats[id(message)] = chat
            if len(self._message_chats) > 4096:
                self._message_chats.pop(next(iter(self._message_chats)))
            if infer_message_type(message) == "image" and source != "self":
                self._last_images[chat_key] = message
            # 过滤掉用户手动发送的 self 消息，只允许撤回本适配器刚发出的回复。
            if source == "self" and content in self._sent_replies:
                self._last_self_messages[self._chat_key(chat)] = message

    def last_self_message(self, chat: Any) -> Any | None:
        """返回某会话最近收到的自己消息，供撤回工具做安全目标解析。"""
        with self._message_state_lock:
            return self._last_self_messages.get(self._chat_key(chat))

    def forget_last_self_message(self, chat: Any, message: Any) -> None:
        """撤回成功后清掉目标，避免下一次误用同一条消息对象。"""
        key = self._chat_key(chat)
        with self._message_state_lock:
            if self._last_self_messages.get(key) is message:
                self._last_self_messages.pop(key, None)

    def last_image_message(self, chat: Any) -> Any | None:
        """返回当前会话最近收到的图片，供下一条文字问题继续视觉问答。"""
        with self._message_state_lock:
            return self._last_images.get(self._chat_key(chat))

    def _build_tool_executor(self, message: Any, chat: Any) -> WeChatToolExecutor:
        runtime = getattr(getattr(self.service, "settings", None), "runtime", None)
        default_new_tools = runtime is not None
        normalized = normalize_wechat_message(
            message,
            chat,
            self.settings.bot_name,
            accept_self_if=self._accept_self_message,
        )
        return WeChatToolExecutor(
            self,
            message,
            chat,
            allow_recall=bool(getattr(runtime, "allow_recall_tool", False)),
            allow_weather=bool(getattr(runtime, "allow_weather_tool", default_new_tools)),
            allow_research=bool(getattr(runtime, "allow_research_tool", default_new_tools)),
            allow_ocr=bool(getattr(runtime, "allow_ocr_tool", default_new_tools)),
            allow_speech=bool(getattr(runtime, "allow_speech_tool", default_new_tools)),
            allow_time=bool(getattr(runtime, "allow_time_tool", default_new_tools)),
            allow_moments_publish=bool(getattr(runtime, "allow_moments_publish", False)),
            allow_moments_comment=bool(getattr(runtime, "allow_moments_comment", False)),
            allow_media=bool(getattr(runtime, "allow_media_tool", False)),
            allow_group_tools=bool(getattr(runtime, "allow_group_tools", False)),
            allow_vision=bool(getattr(runtime, "allow_vision_tool", False)),
            allow_poke=bool(getattr(runtime, "allow_poke_tool", False)),
            allow_document=bool(getattr(runtime, "allow_document_tool", False)),
            allow_link=bool(getattr(runtime, "allow_link_tool", False)),
            allow_audio=bool(getattr(runtime, "allow_audio_tool", False)),
            allow_video=bool(getattr(runtime, 'allow_video_tool', False)),
            multimodal_mode=str(getattr(runtime, "multimodal_mode", "global")),
            is_mentioned=bool(normalized and normalized.get("is_mentioned")),
            weather_ip_location=bool(getattr(runtime, "weather_ip_location", True)),
            weather_module=self.weather_module,
            research_module=self.research_module,
            ocr_module=self.ocr_module,
            speech_module=self.speech_module,
            media_module=self.media_module,
            group_module=self.group_module,
            announcement_module=self.announcement_module,
            vision_module=self.vision_module,
            poke_module=self.poke_module,
            document_module=self.document_module,
            link_module=self.link_module,
            audio_module=self.audio_module,
            video_module=self.video_module,
            send_image=self.send_image,
            send_sticker=self.send_sticker,
            send_audio=self.send_audio,
            time_module=self.time_module,
            moments_module=self.moments_module,
            # 图片回调必须绑定自身；线程池处理期间可能已收到更新的图片。
            vision_message=(message if infer_message_type(message) == "image"
                            else self.last_image_message(chat) or message),
        )

    def _control_keywords(self) -> set[str]:
        """取配置里的群聊控制词。用 isinstance 防御，便于测试替换 service。"""
        scope = getattr(getattr(self.service, "settings", None), "scope", None)
        keywords: set[str] = set()
        for name in ("group_activate_keyword", "group_deactivate_keyword"):
            value = getattr(scope, name, None)
            if isinstance(value, str) and value.strip():
                keywords.add(value.strip())
        return keywords

    def _accept_self_message(self, text: str) -> bool:
        """判断「主人自己发的消息」要不要处理，默认策略是一律忽略。

        只有同时通过下面两道闸才会放行——少任何一道都可能变成无限刷屏：
          1) 精确等于控制词，而不是包含控制词。因为机器人自己的回复文案里就可能
             含控制词（例如「已转人工，大肥鱼已停止回复。」含「转人工」），
             用包含匹配会立刻自回环。
          2) 不等于我们刚发出去的回复原文，兜住「回复文案被配成正好等于控制词」的情况。
        """
        stripped = str(text).strip()
        if not stripped:
            return False
        if stripped in self._sent_replies:
            return False
        return stripped in self._control_keywords()

    def _send_reply(self, message: Any, chat: Any, reply: str) -> Any:
        """根据配置选择普通发送或引用发送。"""
        if getattr(self.settings, "reply_mode", "text") == "quote":
            outcome = self.quote_message(message, reply, chat=chat)
            if outcome.success:
                return outcome.response
            LOGGER.error("微信引用回复失败：%s", outcome.reason)
            return {"status": "失败", "message": outcome.reason}
        return self._output_queue.enqueue(
            chat,
            lambda: chat.SendMsg(reply),
            label="reply",
        )

    @staticmethod
    def _clipboard_text() -> str:
        try:
            from wxauto4.utils.win32 import ReadClipboardData

            value = ReadClipboardData()
            return value if isinstance(value, str) else str(value or "")
        except Exception:  # noqa: BLE001 - 剪贴板可能被其它进程占用
            try:
                import win32clipboard

                win32clipboard.OpenClipboard()
                try:
                    value = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
                return value if isinstance(value, str) else str(value or "")
            except Exception:
                return ""

    @staticmethod
    def _set_clipboard_text(value: str) -> bool:
        try:
            from wxauto4.utils.win32 import SetClipboardText

            return bool(SetClipboardText(value))
        except Exception:  # noqa: BLE001
            try:
                import win32clipboard

                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardText(value, win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
                return True
            except Exception:
                return False

    @staticmethod
    def _open_window_snapshot() -> set[int]:
        try:
            from wxauto4.utils.win32 import GetAllWindows

            return {int(hwnd) for hwnd, _class_name, _title in GetAllWindows()}
        except Exception:
            return set()

    @classmethod
    def _copy_link_from_uia_window(cls, hwnd: int) -> str:
        """在新打开的微信页面中寻找“复制链接/更多”控件。"""
        try:
            import wxauto4.uia as uia

            root = uia.ControlFromHandle(hwnd)
        except Exception:
            return ""

        nodes: list[Any] = []
        seen: set[int] = set()

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 8 or len(nodes) >= 800:
                return
            marker = id(node)
            if marker in seen:
                return
            seen.add(marker)
            nodes.append(node)
            try:
                children = node.GetChildren() or []
            except Exception:
                children = []
            for child in children:
                walk(child, depth + 1)

        walk(root)

        def node_text(node: Any) -> str:
            values: list[str] = []
            try:
                values.append(str(getattr(node, "Name", "") or ""))
            except Exception:
                pass
            try:
                values.append(str(node.GetWindowText() or ""))
            except Exception:
                pass
            return " ".join(value.strip() for value in values if value.strip())

        # 先从页面控件文本中直接找 URL，部分 WebView 会暴露地址栏/超链接文本。
        for node in nodes:
            url = LinkModule.extract_url(node_text(node))
            if url:
                return url

        def click_named(names: tuple[str, ...]) -> str:
            for node in nodes:
                text = node_text(node).strip()
                if not text or not any(name.lower() in text.lower() for name in names):
                    continue
                try:
                    click = getattr(node, "Click", None)
                    if callable(click):
                        click()
                    else:
                        node.click()
                    time.sleep(0.25)
                except Exception:
                    continue
                value = LinkModule.extract_url(cls._clipboard_text())
                if value:
                    return value
            return ""

        copied = click_named(("复制链接", "复制网址", "copy link", "copy url"))
        if copied:
            return copied
        # 微信页面通常先点左上角“更多”，再在弹出的菜单中显示复制链接。
        click_named(("更多", "more", "⋯", "..."))
        try:
            root_after = uia.ControlFromHandle(hwnd)
        except Exception:
            root_after = root
        nodes = []
        seen.clear()
        walk(root_after)
        return click_named(("复制链接", "复制网址", "copy link", "copy url"))

    @classmethod
    def _right_click_title_and_copy(cls, hwnd: int) -> str:
        """右键文章标题区域，再从弹出菜单选择“复制链接”。"""
        try:
            import win32gui
            import wxauto4.uia as uia

            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width = max(1, right - left)
            height = max(1, bottom - top)
        except Exception:
            return ""

        # 微信内置文章页的标题通常位于页面上方约 10% 到 25% 的区域；
        # 多个横向点位兼容标题左对齐、居中和窄窗口布局。
        y_values = sorted({
            top + max(90, min(260, int(height * ratio)))
            for ratio in (0.12, 0.17, 0.23)
        })
        x_values = sorted({
            left + max(20, min(width - 20, int(width * ratio)))
            for ratio in (0.35, 0.5, 0.65)
        })
        for y in y_values:
            for x in x_values:
                try:
                    control = uia.ControlFromPoint(x, y)
                    right_click = getattr(control, "RightClick", None) or getattr(control, "right_click", None)
                    if not callable(right_click):
                        continue
                    previous = cls._clipboard_text()
                    marker = "__social_bot_title_link_marker__"
                    marker_set = cls._set_clipboard_text(marker)
                    right_click()
                    time.sleep(0.3)
                    foreground = int(uia.GetForegroundWindow())
                    candidate = cls._copy_link_from_uia_window(foreground)
                    if marker_set:
                        cls._set_clipboard_text(previous)
                    if candidate:
                        return candidate
                except Exception:
                    continue
        return ""

    def _resolve_link_from_message_ui(self, message: Any) -> str:
        """先打开文章卡片，再从新页面或消息菜单复制真实 URL。"""
        direct = LinkModule.message_url(message)
        if direct:
            return direct

        # 公众号/小红书卡片通常需要先点击进入内置页面；先记录窗口集合，
        # 点击后只检查新出现的页面，避免把聊天窗口自身误当成文章页面。
        before_windows = self._open_window_snapshot()
        click = getattr(message, "click", None)
        if callable(click):
            previous = self._clipboard_text()
            marker = "__social_bot_open_link_marker__"
            marker_set = self._set_clipboard_text(marker)
            try:
                click()
                time.sleep(0.8)
            except Exception:
                LOGGER.debug("打开链接卡片失败", exc_info=True)
            if marker_set:
                candidate = LinkModule.extract_url(self._clipboard_text())
                self._set_clipboard_text(previous)
                if candidate:
                    return candidate
                for hwnd in self._open_window_snapshot() - before_windows:
                    candidate = self._copy_link_from_uia_window(hwnd)
                    if candidate:
                        return candidate
                    candidate = self._right_click_title_and_copy(hwnd)
                    if candidate:
                        return candidate

                # 微信可能复用已有的 Chrome_WidgetWin_0 文章窗口，不会产生新句柄；
                # 此时尝试当前前台窗口，但只接受微信内置页面的窗口类。
                try:
                    import wxauto4.uia as uia
                    from wxauto4.utils.win32 import GetAllWindows

                    foreground = int(uia.GetForegroundWindow())
                    article_windows = {
                        int(hwnd)
                        for hwnd, class_name, title in GetAllWindows()
                        if str(class_name) == "Chrome_WidgetWin_0" and str(title) in {"微信", "Weixin"}
                    }
                    if foreground in article_windows:
                        candidate = self._right_click_title_and_copy(foreground)
                        if candidate:
                            return candidate
                except Exception:
                    pass

        # 页面没有暴露可操作控件时，回退到 wxauto 消息自己的右键菜单。
        select_option = getattr(message, "select_option", None)
        if callable(select_option):
            previous = self._clipboard_text()
            marker = "__social_bot_link_clipboard_marker__"
            marker_set = False
            for option in ("复制链接", "复制网址", "Copy link", "Copy Link"):
                marker_set = self._set_clipboard_text(marker)
                try:
                    try:
                        select_option(option, timeout=3)
                    except TypeError:
                        select_option(option)
                except Exception:
                    continue
                candidate = LinkModule.extract_url(self._clipboard_text())
                if candidate:
                    if marker_set:
                        self._set_clipboard_text(previous)
                    return candidate
                if not marker_set:
                    # 无法设置标记时仍继续尝试其它菜单文本，但不把旧剪贴板误判为链接。
                    continue
            if marker_set:
                self._set_clipboard_text(previous)
        return ""

    def resolve_link_from_message(self, message: Any, chat: Any | None = None) -> str:
        """从链接/文章卡片消息获取真实 URL，并与全局 UI 队列串行。"""
        with self._message_state_lock:
            remembered_chat = self._message_chats.get(id(message))
        target = chat or remembered_chat or getattr(message, "chat", None)
        if target is None:
            return self._resolve_link_from_message_ui(message)
        return self._output_queue.enqueue(
            target,
            lambda: self._resolve_link_from_message_ui(message),
            label="link_card",
        )

    def recall_message(self, message: Any, timeout: int = 2) -> RecallOutcome:
        """撤回一条自己发送的消息。"""
        return self.recall_module.recall(message, timeout=timeout)

    def quote_message(
        self,
        message: Any,
        text: str,
        at: str | list[str] | None = None,
        timeout: int = 3,
        chat: Any | None = None,
    ) -> QuoteOutcome:
        """引用指定消息发送文字。"""
        with self._message_state_lock:
            remembered_chat = self._message_chats.get(id(message))
        target = chat or remembered_chat or getattr(message, "chat", None)
        if target is None:
            # 保持旧的直接调用接口兼容；实际微信工具调用会显式传入当前聊天窗。
            return self.quote_module.quote(message, text, at=at, timeout=timeout)
        return self._output_queue.enqueue(
            target,
            lambda: self.quote_module.quote(message, text, at=at, timeout=timeout),
            label="quote",
        )

    def publish_moment(
        self,
        text: str,
        media_files: Iterable[str] | None = None,
        privacy_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发表朋友圈，并记录这次发布的目标供后续评论使用。"""
        return self.moments_module.publish(text, media_files, privacy_config)

    def comment_last_moment(self, text: str) -> dict[str, Any]:
        """在本程序最近成功发布且已追踪到的朋友圈下评论。"""
        return self.moments_module.comment_last(text)

    def send_image(self, chat: Any, path: str):
        """向当前监听会话发送一张图片或表情包。"""
        return self._output_queue.enqueue(chat, lambda: self.media_module.send_image(chat, path), label="image")

    def send_sticker(self, chat: Any, path: str):
        return self._output_queue.enqueue(chat, lambda: self.media_module.send_sticker(chat, path), label="sticker")

    def send_audio(self, chat: Any, path: str, duration: int | None = None, start: int = 0):
        return self._output_queue.enqueue(
            chat,
            lambda: self.audio_module.send_audio(chat, path, duration=duration, start=start),
            label="audio",
        )

    def set_vision_analyzer(self, analyzer: Any) -> None:
        """注入外部多模态模型函数；本项目不负责调用具体模型 API。"""
        self.vision_module.analyzer = analyzer

    def set_group_announcement(self, chat: Any, text: str) -> GroupOutcome:
        from ...modules.group import is_group_chat
        if not is_group_chat(chat):
            return GroupOutcome(False,'group_chat_required')
        result=self.announcement_module.update(chat,operation='replace',text=text)
        return GroupOutcome(bool(result.get('success')),result.get('reason',''),result)

    def set_group_my_nickname(self, chat: Any, nickname: str) -> GroupOutcome:
        return self.group_module.set_my_nickname(chat, nickname)

    def set_group_remark(self, chat: Any, remark: str) -> GroupOutcome:
        return self.group_module.set_remark(chat, remark)

    def _dispatch_key(self, msg: Any, chat: Any) -> str:
        chat_key = self._chat_key(chat)
        message_id = _first_attr(msg, ("id", "msg_id", "hash", "message_id"))
        if message_id:
            return f"{chat_key}\x1f{message_id}"
        # 没有稳定消息 ID 的旧版 wxauto 至少保证同一个对象不会重复提交；
        # BotService 仍会用规范化消息内容做持久化去重兜底。
        return f"{chat_key}\x1fobject:{id(msg)}"

    def _claim_message(self, key: str) -> bool:
        with self._dispatch_lock:
            if self._executor_closed or key in self._inflight_message_keys or key in self._recent_message_keys:
                return False
            self._inflight_message_keys.add(key)
            if len(self._recent_message_order) == self._recent_message_order.maxlen:
                expired = self._recent_message_order.popleft()
                self._recent_message_keys.discard(expired)
            self._recent_message_order.append(key)
            self._recent_message_keys.add(key)
            return True

    def _process_message(self, msg: Any, chat: Any, key: str) -> None:
        outcome = None
        try:
            outcome = self.text_reply_module.handle(msg, chat)
            LOGGER.debug(
                "微信消息处理完成 chat=%s handled=%s sent=%s reason=%s",
                self._chat_key(chat), outcome.handled, outcome.sent, outcome.reason,
            )
        except Exception:  # noqa: BLE001 - 工作线程不能影响其它消息
            LOGGER.exception("微信消息工作线程异常 chat=%s", self._chat_key(chat))
        finally:
            from ...store import ConversationStore
            store=getattr(self.service,'store',None)
            if isinstance(store,ConversationStore):
                store.complete_incoming(key,'waiting_reply' if getattr(outcome,'reason','') in ('model_error','service_error') else 'done')
            with self._dispatch_lock:
                self._inflight_message_keys.discard(key)
                # 模型/业务异常允许同一消息在 wxauto 重投后重试；成功处理的消息
                # 保留在有界 recent 集合中，避免控制词或无消息 ID 的回调重复执行。
                if outcome is None or getattr(outcome, "reason", "") in {"service_error", "model_error"}:
                    self._recent_message_keys.discard(key)

    def _on_message(self, msg: Any, chat: Any) -> None:
        """wxauto 回调只做登记和投递，模型、网络和文件处理在线程池中执行。"""
        self._remember_message(msg, chat)
        key = self._dispatch_key(msg, chat)
        if not self._claim_message(key):
            LOGGER.debug("跳过重复微信消息 chat=%s key=%s", self._chat_key(chat), key)
            return
        from ...store import ConversationStore
        if isinstance(getattr(self.service,'store',None),ConversationStore):
            normalized=normalize_wechat_message(msg,chat,self.settings.bot_name,accept_self_if=self._accept_self_message)
            if normalized:self.service.stage_incoming(normalized,key)
        # 未启动客户端时保留同步行为，方便直接调用适配器的控制测试；真实监听
        # 一旦 start() 设置 client，回调立即返回，不再阻塞 wxauto 的监听线程。
        if self.client is None:
            self._process_message(msg, chat, key)
            return
        try:
            self._message_executor.submit(self._process_message, msg, chat, key)
        except RuntimeError:
            with self._dispatch_lock:
                self._inflight_message_keys.discard(key)
            LOGGER.warning("消息线程池已关闭，丢弃消息 chat=%s", self._chat_key(chat))

    @staticmethod
    def _create_client(wechat_class: Any) -> Any:
        try:
            # 免费版 wxauto4 支持 ads=False，避免启动时输出宣传信息。
            return wechat_class(ads=False)
        except TypeError:
            # 个别版本不接受 ads 参数，退回默认构造。
            return wechat_class()

    def start(self) -> Any:
        if not self.settings.targets:
            raise ValueError("wechat.targets 为空，请填写要监听的微信联系人或群聊名称")
        targets = tuple(self.settings.targets)
        # 注意执行时机：主窗口在建监听之前修（wxauto 的独立窗口尺寸继承自主窗口）；
        # 但**不能**在建监听之前去动各会话的独立窗口——那些窗口会被 wxauto 复用，
        # 提前挪动会让它手里的窗口句柄失效，随后它自己 MoveWindow 会报
        # (1400, 'MoveWindow', '无效的窗口句柄') 直接启动失败。
        ensure_main_window_ready()
        wechat_class = _load_wechat_class()
        try:
            self.client = self._create_client(wechat_class)
        except Exception as exc:  # noqa: BLE001 - 保留原始异常并给出可执行提示
            LOGGER.exception("初始化微信客户端失败")
            raise RuntimeError(
                f"无法连接微信客户端（{type(exc).__name__}: {exc}）。请依次排查："
                "1) 桌面微信已启动并完成登录，主窗口没有被关闭到托盘；"
                "2) 微信客户端版本在当前 wxauto 包的支持范围内——免费版 wxauto4 41.1.7 "
                "最高支持 4.1.8.107，微信 4.1.9 及以上需要 wxautox4（Plus 版）；"
                "3) 若已安装 Plus 版，确认已执行 wxautox4 auth activate 完成激活。"
            ) from exc

        from ...store import ConversationStore
        if isinstance(getattr(self.service,'store',None),ConversationStore):
            recovered=self.service.store.recover_incoming()
            if recovered:LOGGER.warning("已恢复入库 %s 条中断消息，未自动重发历史回复",recovered)
        listening: list[str] = []
        failed: list[tuple[str, str]] = []
        for target in targets:
            reason = _try_add_listen_chat(self.client, target, self._on_message)
            if reason is None:
                listening.append(target)
                continue
            failed.append((target, reason))
            LOGGER.error(
                "监听会话「%s」失败（已重试 %s 次）：%s。"
                "请确认该名称与微信聊天列表中显示的名称完全一致。",
                target, LISTEN_ATTEMPTS, reason,
            )
        self.failed_targets = tuple(failed)

        if not listening:
            raise RuntimeError(
                "所有监听目标都没能挂上：" + "；".join(f"{t}（{r}）" for t, r in failed)
            )
        if failed:
            LOGGER.warning(
                "有 %s 个目标未能监听，其余 %s 个已正常运行：%s",
                len(failed), len(listening), "、".join(t for t, _ in failed),
            )
        # 此时 wxauto 已绑定完毕，再修各会话独立窗口的尺寸/最小化状态才是安全的。
        ensure_chat_windows_ready(listening)
        return self.client

    def close(self) -> None:
        """停止接收新任务，并等待已经提交的消息处理完成。"""
        with self._dispatch_lock:
            if self._executor_closed:
                return
            self._executor_closed = True
        self._message_executor.shutdown(wait=True, cancel_futures=False)
        self._output_queue.close()

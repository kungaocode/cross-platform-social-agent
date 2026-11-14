import builtins
import contextlib
import sys
import threading
import types
import unittest
from unittest import mock

from social_bot import wechat as wechat_module
from social_bot.settings import ScopeSettings, WeChatSettings
from social_bot.wechat import WeChatAdapter, _load_wechat_class, normalize_wechat_message


class FakeMessage:
    """贴近 wxauto4 的真实字段：attr 是来源，type 是内容类型。"""

    def __init__(
        self,
        attr: str = "friend",
        type: str = "text",
        content: str = "你好",
        sender: str = "用户甲",
        id: str = "msg-1",
    ) -> None:
        self.attr = attr
        self.type = type
        self.content = content
        self.sender = sender
        self.id = id


class FakeGroupChat:
    who = "测试群"
    chat_type = "group"


class FakeFriendChat:
    who = "张三"
    chat_type = "friend"


class LegacyFakeMessage:
    """wxauto 3.9 老语义：只有 type，且可能是 self/group。"""

    type = "group"
    content = "@大肥鱼 你好"
    sender = "用户甲"
    id = 123


class LegacyFakeChat:
    who = "测试群"


class WeChatNormalizeTests(unittest.TestCase):
    def test_group_message_normalization(self) -> None:
        result = normalize_wechat_message(FakeMessage(content="@大肥鱼 你好"), FakeGroupChat(), "大肥鱼")
        self.assertEqual(result["platform"], "wechat")
        self.assertEqual(result["chat_type"], "group")
        self.assertEqual(result["chat_id"], "测试群")
        self.assertEqual(result["sender_id"], "用户甲")
        self.assertTrue(result["is_mentioned"])

    def test_private_chat_is_not_group(self) -> None:
        """群聊判定必须看 chat.chat_type，不能看 msg.type。"""
        result = normalize_wechat_message(FakeMessage(), FakeFriendChat(), "大肥鱼")
        self.assertEqual(result["chat_type"], "private")

    def test_private_chat_sender_is_contact_name(self) -> None:
        """私聊白名单要能填「对方显示名」；msg.sender 是占位值时不能把 sender_id 带歪。"""
        for placeholder in ("self", "friend", ""):
            with self.subTest(placeholder=placeholder):
                result = normalize_wechat_message(
                    FakeMessage(attr="friend", sender=placeholder), FakeFriendChat(), "大肥鱼"
                )
                self.assertEqual(result["sender_id"], "张三")
                self.assertEqual(result["chat_id"], "张三")

    def test_group_sender_keeps_individual_name(self) -> None:
        """群聊里必须保留具体发言人的名字，不能被会话名覆盖。"""
        result = normalize_wechat_message(FakeMessage(sender="用户甲"), FakeGroupChat(), "大肥鱼")
        self.assertEqual(result["sender_id"], "用户甲")
        self.assertEqual(result["chat_id"], "测试群")

    def test_own_message_is_ignored_by_attr(self) -> None:
        """回归：attr 才是来源。漏掉这条会让机器人回复自己的回复，无限刷屏。"""
        own = FakeMessage(attr="self", content="你好", sender="我")
        self.assertIsNone(normalize_wechat_message(own, FakeGroupChat(), "大肥鱼"))

    def test_own_reply_that_contains_bot_name_is_still_ignored(self) -> None:
        """回归：机器人回复里含 bot_name，曾经会被误判成“被@”而触发自我回复。"""
        own_reply = FakeMessage(attr="self", content="大肥鱼收到")
        self.assertIsNone(normalize_wechat_message(own_reply, FakeGroupChat(), "大肥鱼"))

    def test_system_and_time_messages_are_ignored(self) -> None:
        self.assertIsNone(normalize_wechat_message(FakeMessage(attr="system"), FakeGroupChat(), "大肥鱼"))
        self.assertIsNone(
            normalize_wechat_message(
                FakeMessage(attr="system", type="time", content="2026年10月10日"), FakeGroupChat()
            )
        )

    def test_group_message_without_mention(self) -> None:
        result = normalize_wechat_message(FakeMessage(content="今天天气不错"), FakeGroupChat(), "大肥鱼")
        self.assertFalse(result["is_mentioned"])

    def test_mention_of_someone_else_is_not_bot_mention(self) -> None:
        """回归：群里别人互相 @ 不能被当成在叫机器人。"""
        result = normalize_wechat_message(FakeMessage(content="@韩一萌 你看这个"), FakeGroupChat(), "鲲")
        self.assertFalse(result["is_mentioned"])

    def test_bot_mention_matches_account_name(self) -> None:
        """微信里 @ 会渲染成「@鲲」并跟一个 U+2005。"""
        result = normalize_wechat_message(FakeMessage(content="@鲲\u2005你好"), FakeGroupChat(), "鲲")
        self.assertTrue(result["is_mentioned"])

    def test_without_bot_name_any_at_counts(self) -> None:
        result = normalize_wechat_message(FakeMessage(content="@谁 你好"), FakeGroupChat(), "")
        self.assertTrue(result["is_mentioned"])

    def test_explicit_mention_attribute_wins(self) -> None:
        msg = FakeMessage(content="在吗")
        msg.is_mentioned = True
        self.assertTrue(normalize_wechat_message(msg, FakeGroupChat(), "大肥鱼")["is_mentioned"])

    def test_empty_content_is_ignored(self) -> None:
        self.assertIsNone(normalize_wechat_message(FakeMessage(content="   "), FakeGroupChat()))

    def test_image_and_voice_messages_keep_placeholder_text(self) -> None:
        image = normalize_wechat_message(FakeMessage(type="image", content=""), FakeFriendChat())
        voice = normalize_wechat_message(FakeMessage(type="voice", content=""), FakeFriendChat())
        self.assertEqual((image["message_type"], image["text"]), ("image", "[图片消息]"))
        self.assertEqual((voice["message_type"], voice["text"]), ("voice", "[语音消息]"))

    def test_link_card_keeps_placeholder_text_for_card_reader(self) -> None:
        link = normalize_wechat_message(FakeMessage(type="link", content=""), FakeFriendChat())
        self.assertEqual((link["message_type"], link["text"]), ("link", "[链接卡片消息]"))

    def test_image_alias_is_normalized_for_ocr(self) -> None:
        image = normalize_wechat_message(FakeMessage(type="picture", content=""), FakeFriendChat())

        self.assertEqual((image["message_type"], image["text"]), ("image", "[图片消息]"))

    def test_legacy_wxauto_type_semantics_still_work(self) -> None:
        result = normalize_wechat_message(LegacyFakeMessage(), LegacyFakeChat(), "大肥鱼")
        self.assertEqual(result["chat_type"], "group")
        self.assertTrue(result["is_mentioned"])

    def test_legacy_self_message_is_ignored(self) -> None:
        message = LegacyFakeMessage()
        message.type = "self"
        self.assertIsNone(normalize_wechat_message(message, LegacyFakeChat()))


class ClientPackagePreferenceTests(unittest.TestCase):
    """回归：曾经先导入免费版，导致装了 Plus 版也用不上。"""

    @staticmethod
    def _module(name: str, wechat: object) -> types.ModuleType:
        module = types.ModuleType(name)
        module.WeChat = wechat  # type: ignore[attr-defined]
        return module

    @classmethod
    def _patcher(cls, available: dict[str, types.ModuleType]):
        """只接管 wxauto 相关的 import，其余交回真实导入，避免干扰测试框架。"""
        real_import = builtins.__import__

        def _import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in available:
                return available[name]
            if name in {"wxauto4", "wxautox4"}:
                raise ImportError(f"No module named {name!r}")
            return real_import(name, globals, locals, fromlist, level)

        return mock.patch("builtins.__import__", side_effect=_import)

    def test_plus_package_is_preferred_when_both_installed(self) -> None:
        plus = self._module("wxautox4", object())
        free = self._module("wxauto4", object())
        with self._patcher({"wxautox4": plus, "wxauto4": free}):
            self.assertIs(_load_wechat_class(), plus.WeChat)

    def test_falls_back_to_free_package(self) -> None:
        free = self._module("wxauto4", object())
        with self._patcher({"wxauto4": free}):
            self.assertIs(_load_wechat_class(), free.WeChat)

    def test_raises_when_installed_package_exports_nothing(self) -> None:
        empty = types.ModuleType("wxauto4")
        with self._patcher({"wxauto4": empty}):
            with self.assertRaises(RuntimeError):
                _load_wechat_class()

    def test_raises_when_nothing_installed(self) -> None:
        with self._patcher({}):
            with self.assertRaises(RuntimeError):
                _load_wechat_class()


class AdapterStartTests(unittest.TestCase):
    """回归：Plus 版 AddListenChat 成功时也返回 WxResponse，不能误判成失败。"""

    class FakeWxResponse(dict):
        @property
        def is_success(self) -> bool:
            return self.get("status") == "成功"

    @classmethod
    def _response(cls, status: str, message: str | None = None) -> "AdapterStartTests.FakeWxResponse":
        return cls.FakeWxResponse(status=status, message=message, data=None)

    def _adapter(self, targets: tuple[str, ...] = ("测试群",)) -> WeChatAdapter:
        settings = WeChatSettings(enabled=True, targets=targets, bot_name="大肥鱼")
        return WeChatAdapter(mock.Mock(), settings)

    @staticmethod
    @contextlib.contextmanager
    def _start_context(wechat_class):
        """拦住真实系统副作用：不导入真实适配包，也不去改动真实的微信窗口。"""
        with (
            mock.patch.object(wechat_module, "ensure_main_window_ready"),
            mock.patch.object(wechat_module, "ensure_chat_windows_ready"),
            mock.patch.object(wechat_module, "LISTEN_RETRY_DELAY", 0),
            mock.patch.object(wechat_module, "_load_wechat_class", return_value=wechat_class),
        ):
            yield

    def _run_start(self, client: object, targets: tuple[str, ...] = ("测试群",)) -> WeChatAdapter:
        """跑一次 start() 并返回 adapter，用于检查 failed_targets。"""
        adapter = self._adapter(targets)
        with self._start_context(mock.Mock(return_value=client)):
            adapter.start()
        return adapter

    def _start(self, client: object, targets: tuple[str, ...] = ("测试群",)) -> object:
        adapter = self._adapter(targets)
        with self._start_context(mock.Mock(return_value=client)):
            return adapter.start()

    def test_plus_success_response_is_not_treated_as_failure(self) -> None:
        client = mock.Mock()
        client.AddListenChat.return_value = self._response("成功")
        self.assertIs(self._start(client), client)

    def test_chat_object_return_is_accepted(self) -> None:
        """免费版成功时返回 Chat 对象，同样不能误判。"""
        client = mock.Mock()
        client.AddListenChat.return_value = object()
        self.assertIs(self._start(client), client)

    def test_failed_response_raises_with_message(self) -> None:
        client = mock.Mock()
        client.AddListenChat.return_value = self._response("失败", "未找到会话")
        with self.assertRaises(RuntimeError) as ctx:
            self._start(client)
        self.assertIn("未找到会话", str(ctx.exception))

    def test_transient_failure_is_retried(self) -> None:
        """回归：wxauto 打开独立窗口偶发 (1400, 'MoveWindow', '无效的窗口句柄')，重试应成功。"""
        client = mock.Mock()
        client.AddListenChat.side_effect = [
            OSError(1400, "MoveWindow", "无效的窗口句柄。"),
            self._response("成功"),
        ]
        adapter = self._run_start(client)
        self.assertEqual(client.AddListenChat.call_count, 2)
        self.assertEqual(adapter.failed_targets, ())

    def test_one_bad_target_does_not_stop_the_others(self) -> None:
        """回归：几个目标里 1 个抽风，其余必须照常运行，而不是整体中止。"""
        client = mock.Mock()

        def add(name, callback):
            if name == "坏目标":
                raise OSError(1400, "MoveWindow", "无效的窗口句柄。")
            return self._response("成功")

        client.AddListenChat.side_effect = add
        adapter = self._run_start(client, targets=("群A", "坏目标", "群B"))
        self.assertEqual([name for name, _ in adapter.failed_targets], ["坏目标"])
        self.assertIn("MoveWindow", adapter.failed_targets[0][1])

    def test_all_targets_failing_raises(self) -> None:
        client = mock.Mock()
        client.AddListenChat.return_value = self._response("失败", "没有这个会话")
        with self.assertRaises(RuntimeError) as ctx:
            self._run_start(client, targets=("群A", "群B"))
        self.assertIn("所有监听目标都没能挂上", str(ctx.exception))

    def test_ads_kwarg_fallback_on_type_error(self) -> None:
        """个别版本不接受 ads 参数，应退回默认构造而不是直接失败。"""
        client = mock.Mock()
        client.AddListenChat.return_value = self._response("成功")
        wechat_class = mock.Mock(side_effect=[TypeError("unexpected keyword argument 'ads'"), client])
        adapter = self._adapter()
        with self._start_context(wechat_class):
            self.assertIs(adapter.start(), client)
        self.assertEqual(wechat_class.call_count, 2)

    def test_empty_targets_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._adapter(targets=()).start()


class SelfMessageControlTests(unittest.TestCase):
    """主人自己发的消息默认忽略，但控制词要能用，且绝不能自回环。"""

    class StubService:
        def __init__(self, reply: str) -> None:
            self.settings = types.SimpleNamespace(
                scope=ScopeSettings(
                    group_activate_keyword="转肥鱼",
                    group_deactivate_keyword="转人工",
                )
            )
            self.reply = reply
            self.handled: list[dict] = []

        def handle(self, message: dict) -> dict:
            self.handled.append(message)
            return {"accepted": True, "reply": self.reply}

    def _adapter(self, reply: str = "大肥鱼收到") -> tuple[WeChatAdapter, "SelfMessageControlTests.StubService"]:
        service = self.StubService(reply)
        adapter = WeChatAdapter(
            service,  # type: ignore[arg-type]
            WeChatSettings(enabled=True, targets=("测试群",), bot_name="大肥鱼"),
        )
        return adapter, service

    def test_own_plain_message_is_ignored(self) -> None:
        adapter, service = self._adapter()
        adapter._on_message(FakeMessage(attr="self", content="大家好"), mock.Mock())
        self.assertEqual(service.handled, [])

    def test_image_processing_uses_current_image_after_newer_callback(self) -> None:
        adapter, _ = self._adapter()
        self.addCleanup(adapter.close)
        chat = FakeFriendChat()
        first = FakeMessage(type="image", content="图片", id="image-A")
        second = FakeMessage(type="image", content="图片", id="image-B")
        adapter._remember_message(first, chat)
        adapter._remember_message(second, chat)
        executor = adapter._build_tool_executor(first, chat)
        executor.allow_vision = True
        adapter.vision_module.ask = mock.Mock(return_value=types.SimpleNamespace(
            success=True, reason="", answer="A", image_path="A.png", request=None))
        self.assertTrue(executor.execute("wechat_visual_question", {"question": "描述图片"})["success"])
        adapter.vision_module.ask.assert_called_once_with(first, "描述图片")

    def test_text_followup_can_still_use_last_image_from_same_chat(self) -> None:
        adapter, _ = self._adapter()
        self.addCleanup(adapter.close)
        chat = FakeFriendChat()
        image = FakeMessage(type="image", content="图片")
        adapter._remember_message(image, chat)
        executor = adapter._build_tool_executor(FakeMessage(content="这是什么？"), chat)
        self.assertIs(executor.vision_message, image)
        other_chat = FakeGroupChat()
        text = FakeMessage(content="这是什么？")
        self.assertIs(adapter._build_tool_executor(text, other_chat).vision_message, text)

    def test_own_mention_is_still_ignored(self) -> None:
        """@提问仍然只响应别人，避免机器人回应自己的输出。"""
        adapter, service = self._adapter()
        adapter._on_message(FakeMessage(attr="self", content="@大肥鱼 你好"), mock.Mock())
        self.assertEqual(service.handled, [])

    def test_own_exact_control_keyword_is_accepted(self) -> None:
        adapter, service = self._adapter()
        chat = mock.Mock()
        adapter._on_message(FakeMessage(attr="self", content="转肥鱼"), chat)
        self.assertEqual(len(service.handled), 1)

    def test_own_message_merely_containing_keyword_is_rejected(self) -> None:
        """包含控制词不算，必须精确相等；否则回复文案里的控制词会立刻自回环。"""
        adapter, service = self._adapter()
        adapter._on_message(
            FakeMessage(attr="self", content="已转人工，大肥鱼已停止回复。"), mock.Mock()
        )
        self.assertEqual(service.handled, [])

    def test_own_reply_echo_does_not_loop(self) -> None:
        """最关键的一条：回复文案含控制词时，回显回来必须被跳过。"""
        adapter, service = self._adapter(reply="已转人工，大肥鱼已停止回复。")
        chat = mock.Mock()
        adapter._on_message(FakeMessage(attr="self", content="转人工"), chat)
        self.assertEqual(len(service.handled), 1)
        # 模拟上面那条回复作为 self 消息被监听回调收到
        adapter._on_message(FakeMessage(attr="self", content="已转人工，大肥鱼已停止回复。"), chat)
        self.assertEqual(len(service.handled), 1)

    def test_sent_reply_equal_to_keyword_is_skipped(self) -> None:
        """兜底：回复文案被配成正好等于控制词时也不能回环。"""
        adapter, service = self._adapter(reply="转人工")
        chat = mock.Mock()
        adapter._on_message(FakeMessage(attr="self", content="转人工"), chat)
        self.assertEqual(len(service.handled), 1)
        adapter._on_message(FakeMessage(attr="self", content="转人工"), chat)
        self.assertEqual(len(service.handled), 1)

    def test_other_people_control_keyword_still_works(self) -> None:
        """别人发的控制词不受影响，仍然按包含匹配（可以夹在句子里）。"""
        adapter, service = self._adapter()
        chat = mock.Mock()
        adapter._on_message(FakeMessage(attr="friend", content="麻烦转肥鱼谢谢"), chat)
        self.assertEqual(len(service.handled), 1)

    def test_normalize_accepts_self_when_predicate_allows(self) -> None:
        result = normalize_wechat_message(
            FakeMessage(attr="self", content="转肥鱼"),
            FakeGroupChat(),
            "大肥鱼",
            accept_self_if=lambda text: text == "转肥鱼",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["chat_type"], "group")

    def test_service_exception_does_not_escape_into_listener(self) -> None:
        """回归：业务异常不能窜进 wxauto 的监听线程，否则会丢消息。"""

        class BoomService:
            def __init__(self) -> None:
                self.settings = types.SimpleNamespace(scope=ScopeSettings())

            def handle(self, message: dict) -> dict:
                raise RuntimeError("模拟业务异常")

        adapter = WeChatAdapter(
            BoomService(),  # type: ignore[arg-type]
            WeChatSettings(enabled=True, targets=("测试群",), bot_name="大肥鱼"),
        )
        chat = mock.Mock()
        adapter._on_message(FakeMessage(), chat)  # 不应抛出
        chat.SendMsg.assert_not_called()


class MessageDispatchTests(unittest.TestCase):
    def test_link_card_copies_url_from_wxauto_menu(self) -> None:
        adapter = WeChatAdapter(mock.Mock(), WeChatSettings(enabled=True, targets=("测试群",)))
        message = FakeMessage(type="link", content="文章标题")
        message.select_option = mock.Mock(return_value={"status": "成功"})
        with (
            mock.patch.object(adapter, "_clipboard_text", side_effect=["old", "https://mp.weixin.qq.com/s/card"]),
            mock.patch.object(adapter, "_set_clipboard_text", return_value=True),
        ):
            self.assertEqual(adapter._resolve_link_from_message_ui(message), "https://mp.weixin.qq.com/s/card")
        message.select_option.assert_called_once_with("复制链接", timeout=3)

    def test_callback_dispatches_messages_in_parallel_and_deduplicates(self) -> None:
        class ParallelService:
            def __init__(self) -> None:
                self.settings = types.SimpleNamespace(scope=ScopeSettings())
                self.barrier = threading.Barrier(2, timeout=3)
                self.handled: list[str] = []
                self.lock = threading.Lock()

            def handle(self, message: dict) -> dict:
                with self.lock:
                    self.handled.append(message["message_id"])
                self.barrier.wait()
                return {"accepted": True, "reply": "收到"}

        service = ParallelService()
        adapter = WeChatAdapter(
            service,  # type: ignore[arg-type]
            WeChatSettings(enabled=True, targets=("群A", "群B"), bot_name="大肥鱼"),
        )
        adapter.client = object()
        chat_a = type("Chat", (), {"who": "群A", "chat_type": "group", "SendMsg": lambda self, text: None})()
        chat_b = type("Chat", (), {"who": "群B", "chat_type": "group", "SendMsg": lambda self, text: None})()
        adapter._on_message(FakeMessage(id="a"), chat_a)
        adapter._on_message(FakeMessage(id="b"), chat_b)
        adapter._on_message(FakeMessage(id="a"), chat_a)
        adapter.close()

        self.assertEqual(sorted(service.handled), ["a", "b"])


def _fake_win32(windows):
    calls = {"move": [], "show": []}
    gui = types.ModuleType("win32gui")
    gui.GetClassName = lambda hwnd: wechat_module.MAIN_WINDOW_CLASS
    gui.GetWindowText = lambda hwnd: windows[hwnd]["title"]
    gui.GetWindowRect = lambda hwnd: windows[hwnd]["rect"]
    gui.IsWindowVisible = lambda hwnd: windows[hwnd]["visible"]
    gui.IsIconic = lambda hwnd: windows[hwnd]["iconic"]
    gui.MoveWindow = lambda *args: calls["move"].append(args)
    gui.ShowWindow = lambda hwnd, flag: calls["show"].append((hwnd, flag))

    def enum_windows(callback, extra):
        for hwnd in windows:
            callback(hwnd, extra)

    gui.EnumWindows = enum_windows
    con = types.ModuleType("win32con")
    con.SW_SHOWNORMAL = 1
    con.SW_RESTORE = 9
    return gui, con, calls


def _build_windows(*specs):
    windows = {}
    for i, spec in enumerate(specs):
        spec.setdefault("visible", True)
        spec.setdefault("iconic", False)
        spec.setdefault("rect", (0, 0, 1600, 1000))
        windows[100 + i] = spec
    return windows


class MainWindowGuardTests(unittest.TestCase):
    """回归：窗口被最小化或主窗口过小，都会让监听读不到消息、静默丢弃别人的消息。"""

    def _run(self, *specs):
        gui, con, calls = _fake_win32(_build_windows(*specs))
        with mock.patch.dict(sys.modules, {"win32gui": gui, "win32con": con}):
            wechat_module.ensure_main_window_ready()
        return con, calls

    def test_small_main_window_is_enlarged(self) -> None:
        _, calls = self._run({"title": "微信", "rect": (0, 0, 160, 28)})
        self.assertEqual(len(calls["move"]), 1)
        args = calls["move"][0]
        self.assertGreaterEqual(args[3], wechat_module.MIN_MAIN_WINDOW_SIZE[0])
        self.assertGreaterEqual(args[4], wechat_module.MIN_MAIN_WINDOW_SIZE[1])

    def test_minimized_chat_subwindow_is_restored(self) -> None:
        """最小化窗口的 rect 恒为 160x28 占位值，标题是会话名，容易被漏掉。"""
        con, calls = self._run(
            {"title": "微信", "rect": (0, 0, 1600, 1000)},
            {"title": "某个群", "rect": (-32000, -32000, -31840, -31972), "iconic": True},
        )
        self.assertIn((101, con.SW_RESTORE), calls["show"])
        self.assertEqual(calls["move"], [])

    def test_small_window_that_is_also_minimized_is_restored(self) -> None:
        con, calls = self._run(
            {"title": "微信", "rect": (-32000, -32000, -31840, -31972), "iconic": True}
        )
        self.assertIn((100, con.SW_RESTORE), calls["show"])

    def test_hidden_main_window_is_shown(self) -> None:
        con, calls = self._run({"title": "微信", "rect": (0, 0, 1600, 1000), "visible": False})
        self.assertIn((100, con.SW_SHOWNORMAL), calls["show"])

    def test_healthy_window_is_left_alone(self) -> None:
        _, calls = self._run({"title": "微信", "rect": (0, 0, 1600, 1000)})
        self.assertEqual(calls["move"], [])
        self.assertEqual(calls["show"], [])

    def test_unrelated_window_is_ignored(self) -> None:
        _, calls = self._run({"title": "别的窗口", "rect": (0, 0, 160, 28)})
        self.assertEqual(calls["move"], [])
        self.assertEqual(calls["show"], [])


class ChatWindowGuardTests(unittest.TestCase):
    """回归：每个会话的独立窗口（标题是会话名）也要够大，否则那个人永远收不到回复。"""

    def _run(self, targets, *specs):
        gui, con, calls = _fake_win32(_build_windows(*specs))
        with mock.patch.dict(sys.modules, {"win32gui": gui, "win32con": con}):
            wechat_module.ensure_chat_windows_ready(targets)
        return con, calls

    def test_small_chat_window_is_enlarged(self) -> None:
        """实测踩过：徐灵盈的窗口只有 376x368，她发的消息全被丢掉。"""
        _, calls = self._run(["徐灵盈"], {"title": "徐灵盈", "rect": (0, 0, 376, 368)})
        self.assertEqual(len(calls["move"]), 1)
        args = calls["move"][0]
        self.assertEqual(args[3], wechat_module.MIN_CHAT_WINDOW_SIZE[0])
        self.assertEqual(args[4], wechat_module.MIN_CHAT_WINDOW_SIZE[1])

    def test_minimized_chat_window_is_restored(self) -> None:
        con, calls = self._run(
            ["某个群"],
            {"title": "某个群", "rect": (-32000, -32000, -31840, -31972), "iconic": True},
        )
        self.assertIn((100, con.SW_RESTORE), calls["show"])

    def test_window_not_in_targets_is_untouched(self) -> None:
        """不能去动用户自己打开的其它聊天窗口。"""
        _, calls = self._run(["某个群"], {"title": "别的联系人", "rect": (0, 0, 300, 300)})
        self.assertEqual(calls["move"], [])
        self.assertEqual(calls["show"], [])

    def test_healthy_chat_window_is_left_alone(self) -> None:
        _, calls = self._run(["某个群"], {"title": "某个群", "rect": (0, 0, 1600, 1000)})
        self.assertEqual(calls["move"], [])
        self.assertEqual(calls["show"], [])

    def test_empty_targets_is_noop(self) -> None:
        _, calls = self._run([], {"title": "某个群", "rect": (0, 0, 300, 300)})
        self.assertEqual(calls["move"], [])


if __name__ == "__main__":
    unittest.main()

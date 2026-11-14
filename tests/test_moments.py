from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from social_bot.modules.moments import MomentComment, MomentsModule
from social_bot.tools import WeChatToolExecutor


class FakeMoment:
    def __init__(self, content: str) -> None:
        self.content = content
        self.comments: list[str] = []

    def Comment(self, text: str) -> None:
        self.comments.append(text)


class FakeMomentsWindow:
    def __init__(self) -> None:
        self.items: list[FakeMoment] = []
        self.publish_calls = []
        self.refresh_calls = 0
        self.refresh_result: object = None
        self.raise_on_refresh: Exception | None = None

    def Publish(self, text, media_files=None, privacy_config=None):
        self.publish_calls.append((text, media_files, privacy_config))
        self.items.insert(0, FakeMoment(text))

    def GetMoments(self):
        return list(self.items)

    def Refresh(self):
        self.refresh_calls += 1
        if self.raise_on_refresh is not None:
            raise self.raise_on_refresh
        return self.refresh_result


class FakeClient:
    def __init__(self, window: FakeMomentsWindow) -> None:
        self.window = window
        self.moments_calls = 0

    def Moments(self):
        self.moments_calls += 1
        return self.window


class FakeControl:
    def __init__(self, class_name: str, name: str, children=None) -> None:
        self.ClassName = class_name
        self.Name = name
        self._children = list(children or [])
        self.parent = None
        for child in self._children:
            child.parent = self

    def GetChildren(self):
        return list(self._children)

    def GetParentControl(self):
        return self.parent


class ClickableCommentControl(FakeControl):
    ControlTypeName = "TextControl"

    def __init__(self, name: str) -> None:
        super().__init__("mmui::TimelineCommentText", name)
        self.clicked = False

    def Click(self):
        self.clicked = True


class ClickableActionControl(FakeControl):
    """朋友圈里的操作按钮（「评论」「赞」…），类名不带 comment。"""

    ControlTypeName = "ButtonControl"

    def __init__(self, name: str) -> None:
        super().__init__("mmui::XOutlineButton", name)
        self.clicked = False

    def Click(self):
        self.clicked = True


class FakeEditControl(FakeControl):
    ControlTypeName = "EditControl"

    def __init__(self) -> None:
        super().__init__("Edit", "")
        self.clicked = False
        self.keys: list[str] = []

    def Click(self):
        self.clicked = True

    def SendKeys(self, keys: str):
        self.keys.append(keys)


class FakeSendButton(FakeControl):
    ControlTypeName = "ButtonControl"

    def __init__(self) -> None:
        super().__init__("Button", "发送")
        self.clicked = False

    def Click(self):
        self.clicked = True


class FakeCommentDialog(FakeControl):
    ClassName = "Qt51514QWindowToolSaveBits"

    def __init__(self, edit: FakeEditControl, send: FakeSendButton) -> None:
        super().__init__(self.ClassName, "")
        self._children = [edit, send]


class StrayDialogTests(unittest.TestCase):
    """媒体失败会在微信进程里留下模态"选择文件"对话框，必须清掉否则什么都发不出去。"""

    @contextlib.contextmanager
    def _patch_win32(self, module, *, pid, dialog_pid, class_name="#32770"):
        posted: list[tuple] = []

        class FakeWin32Gui:
            @staticmethod
            def IsWindowVisible(hwnd):
                return True

            @staticmethod
            def GetClassName(hwnd):
                return class_name

            @staticmethod
            def GetWindowText(hwnd):
                return "选择文件"

            @staticmethod
            def PostMessage(hwnd, message, wparam, lparam):
                posted.append((hwnd, message))

            @staticmethod
            def EnumWindows(callback, extra):
                callback(111, extra)  # 微信的对话框
                callback(222, extra)  # 别的进程的对话框

        class FakeWin32Process:
            @staticmethod
            def GetWindowThreadProcessId(hwnd):
                return (1, dialog_pid if hwnd == 111 else 999)

        with mock.patch.object(module, "win32gui", FakeWin32Gui), mock.patch.object(
            module, "win32process", FakeWin32Process
        ), mock.patch.object(module, "win32con", SimpleNamespace(WM_CLOSE=0x0010)):
            yield posted

    def test_only_wechat_process_dialogs_are_closed(self) -> None:
        import social_bot.modules.moments.module as moments_module

        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module._window = SimpleNamespace(control=SimpleNamespace(ProcessId=4242))

        with self._patch_win32(moments_module, pid=4242, dialog_pid=4242) as posted:
            closed = module._close_stray_dialogs()

        self.assertEqual(closed, 1)
        self.assertEqual(posted, [(111, 0x0010)])

    def test_non_dialog_window_classes_are_ignored(self) -> None:
        import social_bot.modules.moments.module as moments_module

        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module._window = SimpleNamespace(control=SimpleNamespace(ProcessId=4242))

        with self._patch_win32(moments_module, pid=4242, dialog_pid=4242, class_name="Chrome_WidgetWin_0") as posted:
            closed = module._close_stray_dialogs()

        self.assertEqual(closed, 0)
        self.assertEqual(posted, [])


class MomentsModuleTests(unittest.TestCase):
    def test_publish_tracks_own_moment_and_comments_under_it(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))

        published = module.publish("今天测试朋友圈", privacy_config={})
        commented = module.comment_last("欢迎大家")

        self.assertTrue(published["success"])
        self.assertTrue(published["tracked"])
        self.assertTrue(commented["success"])
        # 空字典等于"不改隐私设置"，传给 wxauto 的是 None，避免它去点隐私菜单。
        self.assertEqual(window.publish_calls, [("今天测试朋友圈", None, None)])
        self.assertEqual(window.items[0].comments, ["欢迎大家"])

    def test_privacy_config_is_passed_through_when_set(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))

        module.publish("设置隐私", privacy_config={"privacy": "白名单", "tags": ["家人"]})

        self.assertEqual(window.publish_calls[0][2], {"privacy": "白名单", "tags": ["家人"]})

    def test_publish_error_treated_as_success_when_content_is_visible(self) -> None:
        """库可能"发出去了才报错"，这时按成功处理，避免重复发布。"""
        window = FakeMomentsWindow()
        calls: list[tuple] = []

        def publish_then_raise(text, media_files=None, privacy_config=None):
            calls.append((text, media_files, privacy_config))
            window.items.insert(0, FakeMoment(text))
            raise AttributeError("'Menu' object has no attribute 'control'")

        window.Publish = publish_then_raise
        module = MomentsModule(lambda: FakeClient(window))

        result = module.publish("发出去了才报错")

        self.assertTrue(result["success"])
        self.assertTrue(result["tracked"])
        self.assertEqual(len(calls), 1)  # 确认已发出，不再重试

    def test_publish_error_retries_once_then_reports_failure(self) -> None:
        window = FakeMomentsWindow()
        calls: list[tuple] = []

        def always_raise(text, media_files=None, privacy_config=None):
            calls.append((text, media_files, privacy_config))
            raise AttributeError("'Menu' object has no attribute 'control'")

        window.Publish = always_raise
        module = MomentsModule(lambda: FakeClient(window), sleep=lambda _seconds: None)

        result = module.publish("怎么都发不出去")

        self.assertFalse(result["success"])
        self.assertIn("AttributeError", result["reason"])
        self.assertEqual(len(calls), 2)  # 原样一次 + 重开窗口后一次
        self.assertEqual([call[2] for call in calls], [None, None])

    def test_comment_via_ui_clicks_comment_button_and_sends(self) -> None:
        """Comment() 假成功时的替代路径：点「评论」按钮 → 内联输入框 → 发送。"""
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("UI 发评论")
        comment_button = ClickableActionControl("评论")
        edit = FakeEditControl()
        send = FakeSendButton()
        content = FakeControl("mmui::TimelineContentCell", "鲲 UI 发评论")
        cell = FakeControl("ummui::TimelineCell", "", [content, comment_button, edit, send])
        for child in cell._children:
            child.parent = cell
        window.items[0].control = content

        with mock.patch.object(module, "_verify_reply_visible", return_value=True), mock.patch(
            "wxautox4.utils.win32.SetClipboardText"
        ) as clipboard:
            result = module.comment_via_ui("抱歉呀，暂时看不到评论🐟")

        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertIn("评论", result["button"])
        self.assertTrue(comment_button.clicked)
        self.assertEqual(edit.keys, ["{Ctrl}a", "{Ctrl}v"])
        clipboard.assert_any_call("抱歉呀，暂时看不到评论🐟")
        self.assertTrue(send.clicked)
        self.assertEqual(result["action"], "moments_comment_ui")

    def test_comment_via_ui_reports_when_button_is_missing(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("没有评论按钮")
        content = FakeControl("mmui::TimelineContentCell", "鲲 没有评论按钮")
        cell = FakeControl("ummui::TimelineCell", "", [content])
        # 只有「赞」，没有「评论」按钮；另外放一个带「评论」字样的控件做候选提示。
        cell._children.append(FakeControl("mmui::XOutlineButton", "查看评论"))
        for child in cell._children:
            child.parent = cell
        window.items[0].control = content

        result = module.comment_via_ui("说明")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "comment_button_not_found")
        self.assertTrue(any("查看评论" in item for item in result["candidates"]))

    def test_comment_via_ui_reports_when_input_never_appears(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window), sleep=lambda _seconds: None)
        module.publish("输入框不出现")
        comment_button = ClickableActionControl("评论")
        content = FakeControl("mmui::TimelineContentCell", "鲲 输入框不出现")
        cell = FakeControl("ummui::TimelineCell", "", [content, comment_button])
        for child in cell._children:
            child.parent = cell
        window.items[0].control = content

        with mock.patch.object(module, "_find_comment_dialog", return_value=None):
            result = module.comment_via_ui("说明")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "comment_input_not_found")
        self.assertTrue(comment_button.clicked)

    def test_media_path_is_validated_before_publish(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / "photo.png"
            media.write_bytes(b"test")
            result = module.publish("带图片", [str(media)], {})
        self.assertTrue(result["success"])
        self.assertEqual(window.publish_calls[0][1], [str(media)])

    def test_media_failure_falls_back_to_text_only(self) -> None:
        """wxautox4 的媒体处理会在部分客户端上崩（input_files 解包错误），不能拖垮整轮任务。"""
        window = FakeMomentsWindow()
        calls: list[tuple] = []

        def flaky_publish(text, media_files=None, privacy_config=None):
            calls.append((text, media_files, privacy_config))
            if media_files:
                raise ValueError("not enough values to unpack (expected 2, got 1)")
            window.items.insert(0, FakeMoment(text))
            return None

        window.Publish = flaky_publish
        module = MomentsModule(lambda: FakeClient(window))
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / "2.jpg"
            media.write_bytes(b"jpeg")

            result = module.publish("纯文字兜底", [str(media)], {})

        self.assertTrue(result["success"])
        self.assertIn("ValueError", result["media_dropped"])
        # 第一次带图、第二次纯文字。
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], [str(media)])
        self.assertIsNone(calls[1][1])
        self.assertEqual(result["content"], "纯文字兜底")

    def test_media_success_does_not_retry(self) -> None:
        window = FakeMomentsWindow()
        calls: list[tuple] = []
        original = window.Publish

        def counting_publish(text, media_files=None, privacy_config=None):
            calls.append((text, media_files, privacy_config))
            return original(text, media_files, privacy_config)

        window.Publish = counting_publish
        module = MomentsModule(lambda: FakeClient(window))
        with tempfile.TemporaryDirectory() as folder:
            media = Path(folder) / "2.jpg"
            media.write_bytes(b"jpeg")

            result = module.publish("带图发布", [str(media)], {})

        self.assertTrue(result["success"])
        self.assertIsNone(result["media_dropped"])
        self.assertEqual(len(calls), 1)

    def test_publish_error_is_reported_without_raising(self) -> None:
        window = FakeMomentsWindow()

        def broken_publish(text, media_files=None, privacy_config=None):
            raise ValueError("not enough values to unpack (expected 2, got 1)")

        window.Publish = broken_publish
        module = MomentsModule(lambda: FakeClient(window))

        result = module.publish("纯文字也失败")

        self.assertFalse(result["success"])
        self.assertIn("publish_error:ValueError", result["reason"])


    def test_comment_requires_recent_program_tracked_publish(self) -> None:
        module = MomentsModule(lambda: FakeClient(FakeMomentsWindow()))
        result = module.comment_last("不应发送")
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "no_recent_published_moment")

    def test_reads_new_comment_rows_from_tracked_moment(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("有评论的朋友圈")
        window.items[0].comments.append("晚上好")

        comments = module.read_last_comments()

        self.assertEqual([comment.content for comment in comments], ["晚上好"])

    def test_reads_comment_from_wxauto_uia_comment_cell(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("UIA 评论")
        window.items[0].control = FakeControl("ummui::TimelineCell", "UIA 评论", [
            FakeControl("ummui::TimelineCommentCell", "小明：晚上好"),
        ])

        comments = module.read_last_comments()

        # 作者和正文要分开，否则 runner 无法识别「这条评论是不是我自己发的」。
        self.assertEqual([(comment.sender, comment.content) for comment in comments], [("小明", "晚上好")])

    def test_window_level_comment_fallback_when_content_has_no_parent(self) -> None:
        """部分刷新状态只在朋友圈窗口根树保留评论行，正文控件没有父节点。"""
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("窗口级评论")
        window.items[0].control = FakeControl("mmui::TimelineContentCell", "鲲 窗口级评论")
        window.control = FakeControl("mmui::SNSWindow", "朋友圈", [
            window.items[0].control,
            FakeControl("mmui::TimelineCommentCell", "小明：窗口树里的评论"),
        ])

        comments = module.read_last_comments()

        self.assertEqual([(comment.sender, comment.content) for comment in comments], [("小明", "窗口树里的评论")])

    def test_comment_author_and_body_split_from_child_controls(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("拆开的评论")
        window.items[0].control = FakeControl("ummui::TimelineCell", "拆开的评论", [
            FakeControl(
                "ummui::TimelineCommentCell",
                "",
                [FakeControl("Text", "小红"), FakeControl("Text", "下午好呀")],
            ),
        ])

        comments = module.read_last_comments()

        self.assertEqual([(comment.sender, comment.content) for comment in comments], [("小红", "下午好呀")])

    def test_comment_key_is_content_based_so_refresh_does_not_re_reply(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("刷新去重")
        window.items[0].control = FakeControl("ummui::TimelineCell", "刷新去重", [
            FakeControl("ummui::TimelineCommentCell", "小明：晚上好"),
        ])

        first = module.read_last_comments()
        # 模拟微信刷新后控件重建：RuntimeId 变了但正文没变。
        window.items[0].control = FakeControl("ummui::TimelineCell", "刷新去重", [
            FakeControl("ummui::TimelineCommentCell", "小明：晚上好"),
        ])
        second = module.read_last_comments()

        self.assertEqual([comment.key for comment in first], [comment.key for comment in second])

    def test_comments_are_found_as_siblings_of_content_cell(self) -> None:
        """真机结构：moment.control 是正文控件，评论行是它的兄弟节点。"""
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("兄弟节点评论")
        content = FakeControl("mmui::TimelineContentCell", "鲲 兄弟节点评论")
        cell = FakeControl("mmui::TimelineCell", "", [content])
        comment = FakeControl("mmui::TimelineCommentCell", "小明：晚上好")
        comment.parent = cell
        cell._children.append(comment)
        window.items[0].control = content

        comments = module.read_last_comments()

        self.assertEqual([(item.sender, item.content) for item in comments], [("小明", "晚上好")])

    def test_comment_scan_stops_at_own_moment_cell(self) -> None:
        """上溯到自己的朋友圈 cell 就停，不能扫到别人帖子下面的评论。"""
        other_comment = FakeControl("mmui::TimelineCommentCell", "陌生人：别人的评论")
        other_cell = FakeControl("mmui::TimelineCell", "", [other_comment])
        content = FakeControl("mmui::TimelineContentCell", "鲲 我的帖子")
        my_cell = FakeControl("mmui::TimelineCell", "", [content])
        my_comment = FakeControl("mmui::TimelineCommentCell", "小明：我的评论")
        my_comment.parent = my_cell
        my_cell._children.append(my_comment)
        FakeControl("mmui::TimelineList", "", [other_cell, my_cell])

        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("我的帖子")
        window.items[0].control = content

        comments = module.read_last_comments()

        self.assertEqual([(item.sender, item.content) for item in comments], [("小明", "我的评论")])

    def test_comment_scan_does_not_walk_up_to_the_list(self) -> None:
        """如果 moment.control 本身就是朋友圈 cell，也不能再往列表上溯。"""
        other_comment = FakeControl("mmui::TimelineCommentCell", "陌生人：别人的评论")
        other_cell = FakeControl("mmui::TimelineCell", "", [other_comment])
        my_comment = FakeControl("mmui::TimelineCommentCell", "小明：我的评论")
        my_cell = FakeControl("mmui::TimelineCell", "", [my_comment])
        FakeControl("mmui::TimelineList", "", [other_cell, my_cell])

        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("我的帖子")
        window.items[0].control = my_cell
        window.items[0].content = "我的帖子"

        comments = module.read_last_comments()

        self.assertEqual([(item.sender, item.content) for item in comments], [("小明", "我的评论")])

    def test_comment_scan_reports_children_read_failure(self) -> None:
        class BrokenChildren(FakeControl):
            def GetChildren(self):
                raise RuntimeError("COMError: 事件无法调用任何订户")

        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("子控件读取失败")
        window.items[0].control = BrokenChildren("mmui::TimelineContentCell", "鲲 子控件读取失败")

        with self.assertLogs("social_bot.modules.moments.module", level="WARNING") as logs:
            comments = module.read_last_comments()

        self.assertEqual(comments, [])
        self.assertIn("读取子控件失败", "\n".join(logs.output))

    def test_comment_scan_accepts_any_comment_class_but_skips_buttons(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("评论控件识别")
        window.items[0].control = FakeControl("ummui::TimelineCell", "评论控件识别", [
            FakeControl("mmui::TimelineCommentCell", "小明：晚上好"),
            FakeControl("mmui::TimelineCommentText", "小红：下午好"),
            # 「评论」按钮的类名里也带 Comment，绝不能被当成评论行。
            FakeControl("mmui::TimelineCommentButton", "评论"),
        ])

        comments = module.read_last_comments()

        self.assertEqual(
            [(comment.sender, comment.content) for comment in comments],
            [("小明", "晚上好"), ("小红", "下午好")],
        )

    def test_missing_comment_controls_are_reported_with_tree(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("没有评论控件")
        window.items[0].control = FakeControl("ummui::TimelineCell", "没有评论控件", [
            FakeControl("mmui::TimelineContent", "正文"),
        ])

        with self.assertLogs("social_bot.modules.moments.module", level="WARNING") as logs:
            comments = module.read_last_comments()

        self.assertEqual(comments, [])
        joined = "\n".join(logs.output)
        self.assertIn("没有读到评论", joined)
        self.assertIn("TimelineCell", joined)  # 摘要里带上真实控件类名，方便排查

    def test_comment_read_result_is_logged_once_per_change(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("评论读取日志")
        window.items[0].control = FakeControl("ummui::TimelineCell", "评论读取日志", [
            FakeControl("mmui::TimelineCommentCell", "小明：晚上好"),
        ])

        with self.assertLogs("social_bot.modules.moments.module", level="INFO") as logs:
            module.read_last_comments()
            module.read_last_comments()

        hits = [line for line in logs.output if "朋友圈评论读取结果" in line]
        self.assertEqual(len(hits), 1)

    def test_diagnose_reports_window_tree_and_comments(self) -> None:
        window = FakeMomentsWindow()
        window.items.append(FakeMoment("别人的帖子"))
        target = FakeMoment("诊断用的帖子")
        target.control = FakeControl("ummui::TimelineCell", "诊断用的帖子", [
            FakeControl("mmui::TimelineCommentCell", "小明：晚上好"),
        ])
        window.items.insert(0, target)
        module = MomentsModule(lambda: FakeClient(window))

        report = module.diagnose(last_text="诊断用的帖子")

        self.assertTrue(report["success"])
        self.assertEqual(report["moment_count"], 2)
        self.assertEqual(report["matched_by"], "exact")
        self.assertTrue(report["has_control"])
        self.assertIn("mmui::TimelineCommentCell:小明：晚上好", report["comment_cells"])
        self.assertTrue(any("TimelineCell" in item for item in report["control_tree"]))
        self.assertEqual(
            report["parsed_comments"], [{"sender": "小明", "content": "晚上好"}]
        )

    def test_diagnose_reports_failure_without_moments(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))

        report = module.diagnose()

        self.assertTrue(report["success"])
        self.assertEqual(report["reason"], "no_moment_visible")

    def test_moment_lookup_tolerates_whitespace_differences(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        published = module.publish("第一行\n第二行 有空格")
        # 微信读回来的正文换行/空白被规范化过。
        window.items[0].content = "第一行 第二行  有空格"

        self.assertTrue(published["success"])
        self.assertTrue(module.refresh_last_published())
        self.assertTrue(module.has_tracked_target())

    def test_moment_lookup_matches_cell_with_author_and_metadata(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("大家晚上好呀，这里是大肥鱼🐟\n今日上海多云")
        # 真实的朋友圈控件文字带作者、图片数、地点和相对时间。
        window.items[0].content = "鲲 大家晚上好呀，这里是大肥鱼🐟 今日上海多云 包含1张图片 上海 · 万象城 刚刚"

        self.assertTrue(module.refresh_last_published())
        self.assertTrue(module.has_tracked_target())

    def test_media_moment_uses_control_text_when_content_is_only_filename(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        text = "今天也带着好心情出门啦"
        control = FakeControl("mmui::TimelineContentCell", f"鲲 {text} 包含1张图片 刚刚")
        media_moment = SimpleNamespace(content="2.jpg", control=control, runtimeid="media-1")

        target, how = module._match_moment([media_moment], text)

        self.assertIs(target, media_moment)
        self.assertEqual(how, "contains")

    def test_refresh_prefers_saved_moment_identity_over_duplicate_text(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        first = SimpleNamespace(content="same", runtimeid="first")
        second = SimpleNamespace(content="same", runtimeid="second")
        module._last_published_text = "same"
        module._last_published_identity = "second"
        window.items = [first, second]

        self.assertIs(module._find_published_moment("same"), second)

    def test_moment_lookup_matches_truncated_post_text(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("这是一条很长的朋友圈正文，微信在列表里可能只显示最前面的一段文字用来预览")
        window.items[0].content = "鲲 这是一条很长的朋友圈正文，微信在列表里可能只显示最前面的一段文字 刚刚"

        self.assertTrue(module.refresh_last_published())

    def test_locate_retries_until_window_redraws(self) -> None:
        class DelayedWindow(FakeMomentsWindow):
            """发布成功，但界面要过几次读取才绘制出这条新帖子。"""

            def __init__(self) -> None:
                super().__init__()
                self.published_text = ""
                self.locate_calls = 0
                self.reveal_after = 6

            def Publish(self, text, media_files=None, privacy_config=None):
                self.publish_calls.append((text, media_files, privacy_config))
                self.published_text = text

            def GetMoments(self):
                self.locate_calls += 1
                if self.published_text and self.locate_calls >= self.reveal_after:
                    return [FakeMoment(self.published_text)]
                return []

        window = DelayedWindow()
        module = MomentsModule(lambda: FakeClient(window), sleep=lambda _seconds: None)
        published = module.publish("刷新后才会出现的帖子")

        self.assertTrue(published["success"])
        self.assertFalse(published["tracked"])
        self.assertTrue(module.refresh_last_published())
        self.assertGreaterEqual(window.locate_calls, window.reveal_after)

    def test_refresh_failure_falls_back_to_toolbar_button(self) -> None:
        class ToolbarButton:
            def __init__(self) -> None:
                self.clicked = 0

            def Click(self) -> None:
                self.clicked += 1

        window = FakeMomentsWindow()
        window.refresh_result = {"status": "失败", "message": "未找到刷新按钮"}
        button = ToolbarButton()
        window.toolbar_buttons = {"刷新": button}
        module = MomentsModule(lambda: FakeClient(window), sleep=lambda _seconds: None)
        module.publish("刷新按钮兜底")

        self.assertTrue(module.refresh_last_published())
        self.assertEqual(button.clicked, 1)

    def test_dead_window_is_reopened_and_not_closed(self) -> None:
        class DeadWindow(FakeMomentsWindow):
            """模拟微信关掉朋友圈窗口：GetMoments 抛死引用错误，Close 也抛。"""

            def __init__(self) -> None:
                super().__init__()
                self.dead = False
                self.closed = 0

            def GetMoments(self):
                if self.dead:
                    raise RuntimeError("COMError: 事件无法调用任何订户")
                return super().GetMoments()

            def Close(self) -> None:
                self.closed += 1
                raise RuntimeError("COMError: 事件无法调用任何订户")

        class ReopeningClient:
            """重新打开时给出一个健康的新窗口。"""

            def __init__(self, dead_window: DeadWindow) -> None:
                self.dead_window = dead_window
                self.healthy = FakeMomentsWindow()
                self.healthy.items.append(FakeMoment("窗口重开后的帖子"))
                self.calls = 0

            def Moments(self):
                self.calls += 1
                return self.healthy if self.calls > 1 else self.dead_window

        dead = DeadWindow()
        client = ReopeningClient(dead)
        module = MomentsModule(lambda: client, sleep=lambda _seconds: None)
        # 发布成功，但朋友圈窗口随即被微信关掉：读取和 Close 都变成死引用。
        dead.dead = True
        module.publish("窗口重开后的帖子")

        # 窗口读不到 → 作废句柄 → 重新打开 → 在新窗口里定位成功。
        self.assertFalse(module.has_tracked_target())
        self.assertTrue(module.refresh_last_published())
        self.assertEqual(client.calls, 2)
        # 关键：绝不调用 Close()，否则会和异步关窗竞态，拿到正在关闭的窗口。
        self.assertEqual(dead.closed, 0)

    def test_close_tolerates_dead_window(self) -> None:
        class DeadWindow(FakeMomentsWindow):
            def Close(self) -> None:
                raise RuntimeError("COMError: 事件无法调用任何订户")

        module = MomentsModule(lambda: FakeClient(DeadWindow()), sleep=lambda _seconds: None)
        module.publish("收尾容错")

        module.close()  # 不应抛出

    def test_window_reactivation_is_rate_limited(self) -> None:
        window = FakeMomentsWindow()
        client = FakeClient(window)
        module = MomentsModule(lambda: client, sleep=lambda _seconds: None)
        module.publish("重开限流")
        window.items.clear()

        for _ in range(module.HARD_REFRESH_AFTER * 2):
            module.refresh_last_published()

        # 冷却期内只重新激活一次，避免每轮都折腾朋友圈界面。
        self.assertEqual(client.moments_calls, 2)

    def test_reply_clicks_matching_comment_before_sending(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("点对点回复测试")
        comment_control = ClickableCommentControl("小明：晚上好")
        window.items[0].control = FakeControl(
            "ummui::TimelineCell",
            "点对点回复测试",
            [comment_control],
        )
        edit = FakeEditControl()
        send = FakeSendButton()
        dialog = FakeCommentDialog(edit, send)

        with mock.patch.object(module, "_find_comment_dialog", return_value=dialog), mock.patch.object(
            module, "_verify_reply_visible", return_value=True
        ), mock.patch("wxautox4.utils.win32.SetClipboardText") as clipboard:
            result = module.reply_to_comment(
                MomentComment("comment-1", "晚上好", "小明"),
                "收到啦～",
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])
        self.assertTrue(comment_control.clicked)
        self.assertTrue(edit.clicked)
        self.assertEqual(edit.keys, ["{Ctrl}a", "{Ctrl}v"])
        clipboard.assert_any_call("收到啦～")
        self.assertTrue(send.clicked)

    def test_reply_restores_clipboard_after_pasting(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("剪贴板测试")
        window.items[0].control = FakeControl("ummui::TimelineCell", "剪贴板测试", [
            ClickableCommentControl("小明：晚上好"),
        ])

        with mock.patch.object(
            module, "_find_comment_dialog", return_value=FakeCommentDialog(FakeEditControl(), FakeSendButton())
        ), mock.patch.object(module, "_verify_reply_visible", return_value=True), mock.patch(
            "wxautox4.utils.win32.SetClipboardText"
        ) as clipboard, mock.patch(
            "wxautox4.utils.win32.GetClipboardText", create=True, return_value="用户原来的内容"
        ):
            result = module.reply_to_comment(MomentComment("comment-1", "晚上好", "小明"), "收到啦")

        self.assertTrue(result["success"])
        self.assertEqual(clipboard.call_args_list[-1], mock.call("用户原来的内容"))

    def test_verification_reads_comments_after_cell_rerenders(self) -> None:
        """发评论会让 cell 重绘：旧控件读不到，刷新并重新定位后应该能确认到。"""

        class RerenderingWindow(FakeMomentsWindow):
            def __init__(self) -> None:
                super().__init__()
                self.rebuild = False

            def GetMoments(self):
                if not self.rebuild:
                    return super().GetMoments()
                # 每次读取都返回重建过的朋友圈对象：正文标题一样，评论挂在兄弟节点上。
                rebuilt = FakeMoment("确认评论测试")
                content = FakeControl("mmui::TimelineContentCell", "鲲 确认评论测试")
                cell = FakeControl("mmui::TimelineCell", "", [content])
                comment = FakeControl("mmui::TimelineCommentCell", "鲲：收到啦～")
                comment.parent = cell
                cell._children.append(comment)
                rebuilt.control = content
                return [rebuilt]

        window = RerenderingWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("确认评论测试")
        # 发布时抓到的控件已经失效（读不到任何评论）。
        window.items[0].control = FakeControl("mmui::TimelineContentCell", "鲲 确认评论测试")
        window.rebuild = True

        result = module.comment_last("收到啦～")

        self.assertTrue(result["success"])
        self.assertTrue(result["verified"])

    def test_verification_is_skipped_when_no_control(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("没有控件")

        result = module.comment_last("收到啦～")

        self.assertTrue(result["success"])
        self.assertFalse(result["verified"])


        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("未确认的回复")
        window.items[0].control = FakeControl("ummui::TimelineCell", "未确认的回复", [
            ClickableCommentControl("小明：晚上好"),
        ])

        with mock.patch.object(
            module, "_find_comment_dialog", return_value=FakeCommentDialog(FakeEditControl(), FakeSendButton())
        ), mock.patch.object(module, "_verify_reply_visible", return_value=False), mock.patch(
            "wxautox4.utils.win32.SetClipboardText"
        ):
            result = module.reply_to_comment(MomentComment("comment-1", "晚上好", "小明"), "收到啦")

        # 确认不了也仍然算发送成功，避免重发造成重复评论。
        self.assertTrue(result["success"])
        self.assertFalse(result["verified"])

    def test_short_comment_does_not_match_unrelated_control(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("短评论")
        unrelated = ClickableCommentControl("小明：好看")
        window.items[0].control = FakeControl("ummui::TimelineCell", "短评论", [unrelated])

        result = module.reply_to_comment(MomentComment("comment-1", "好", "小红"), "收到")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "comment_control_not_found")
        self.assertFalse(unrelated.clicked)

    def test_post_body_is_not_used_as_comment_target(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("正文不能被当成评论")
        body = ClickableCommentControl("正文不能被当成评论")
        window.items[0].control = FakeControl(
            "ummui::TimelineCell", "正文不能被当成评论", [body]
        )

        result = module.reply_to_comment(
            MomentComment("comment-1", "正文不能被当成评论", "小明"), "收到"
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "comment_control_not_found")
        self.assertFalse(body.clicked)

    def test_reply_rejects_when_matching_comment_control_is_missing(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        module.publish("没有目标评论")
        window.items[0].control = FakeControl("ummui::TimelineCell", "没有目标评论", [])

        result = module.reply_to_comment(MomentComment("comment-1", "晚上好", "小明"), "收到啦")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "comment_control_not_found")


class MomentsToolTests(unittest.TestCase):
    def test_tools_are_exposed_only_when_enabled(self) -> None:
        module = MomentsModule(lambda: FakeClient(FakeMomentsWindow()))
        disabled = WeChatToolExecutor(mock.Mock(), SimpleNamespace(type="text"), "chat", moments_module=module)
        self.assertNotIn("moments_publish", {item["function"]["name"] for item in disabled.definitions})

        enabled = WeChatToolExecutor(
            mock.Mock(),
            SimpleNamespace(type="text"),
            "chat",
            allow_moments_publish=True,
            allow_moments_comment=True,
            moments_module=module,
        )
        names = {item["function"]["name"] for item in enabled.definitions}
        self.assertIn("moments_publish", names)
        self.assertIn("moments_comment", names)

    def test_publish_then_comment_reports_external_side_effect(self) -> None:
        window = FakeMomentsWindow()
        module = MomentsModule(lambda: FakeClient(window))
        executor = WeChatToolExecutor(
            mock.Mock(),
            SimpleNamespace(type="text"),
            "chat",
            allow_moments_publish=True,
            allow_moments_comment=True,
            moments_module=module,
        )

        published = executor.execute(
            "moments_publish",
            {"text": "测试发布", "media_files": [], "privacy_config": {}},
        )
        commented = executor.execute(
            "moments_comment",
            {"text": "测试评论", "target": "last_published"},
        )

        self.assertTrue(published["success"])
        self.assertTrue(published["side_effect_performed"])
        self.assertTrue(commented["success"])
        self.assertTrue(commented["side_effect_performed"])
        self.assertEqual(window.items[0].comments, ["测试评论"])


if __name__ == "__main__":
    unittest.main()

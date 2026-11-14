from __future__ import annotations

import unittest
from datetime import datetime, timezone

from social_bot.modules.moments.runner import MomentsRunner
from social_bot.modules.moments import MomentComment
from social_bot.settings import MomentsSettings


class FakeModel:
    def __init__(self, content: str = "今天也要好好生活") -> None:
        self.content = content
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages)
        return self.content


class SequencedModel:
    """按调用顺序返回不同内容，用来区分「写正文」和「写评论」两次调用。"""

    def __init__(self, *contents: str) -> None:
        self.contents = list(contents)
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages)
        if not self.contents:
            return ""
        return self.contents.pop(0)


class FakeMoments:
    def __init__(self) -> None:
        self.calls = []
        self.reply_calls = []
        self.comment_last_calls = []
        self.comments = []
        self.refresh_count = 0

    def publish(self, text, media_files=None, privacy_config=None):
        self.calls.append((text, media_files, privacy_config))
        return {"success": True, "content": text}

    def comment_last(self, text):
        self.comment_last_calls.append(text)
        self.calls.append((text, None, None))
        return {"success": True, "content": text}

    def reply_to_comment(self, comment, text):
        self.reply_calls.append((comment, text))
        self.calls.append((text, None, None))
        return {"success": True, "content": text}

    def refresh_last_published(self):
        self.refresh_count += 1
        return True

    def read_last_comments(self):
        return list(self.comments)


class MomentsRunnerTests(unittest.TestCase):
    def test_generates_template_content_then_publishes_closing_text(self) -> None:
        model = FakeModel()
        moments = FakeMoments()
        current = [0.0]
        sleeps = []

        def clock() -> float:
            return current[0]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            current[0] += seconds

        settings = MomentsSettings(
            duration_minutes=1,
            poll_interval_seconds=60,
            template="主题：今日状态；语气：自然",
            closing_text="肥鱼累了不回消息了，睡觉去咯",
            media_files=("C:/photo.png",),
            privacy_config={"privacy": "公开"},
        )
        runner = MomentsRunner(
            model,
            moments,
            settings,
            clock=clock,
            sleep=sleep,
            now=lambda: datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc),
        )

        result = runner.run()

        self.assertTrue(result["success"])
        self.assertEqual([call[0] for call in moments.calls], ["今天也要好好生活", "肥鱼累了不回消息了，睡觉去咯"])
        self.assertEqual(moments.calls[0][1:], (("C:/photo.png",), {"privacy": "公开"}))
        self.assertEqual(sleeps, [60.0])
        self.assertIn("主题：今日状态；语气：自然", model.messages[0][1]["content"])

    def test_empty_model_result_is_rejected_before_publish(self) -> None:
        model = FakeModel("   ")
        moments = FakeMoments()
        runner = MomentsRunner(model, moments, MomentsSettings(duration_minutes=0))

        with self.assertRaisesRegex(RuntimeError, "没有生成"):
            runner.run()
        self.assertEqual(moments.calls, [])

    def test_new_comment_is_answered_with_a_public_comment_by_default(self) -> None:
        model = FakeModel("收到啦")
        moments = FakeMoments()
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] == 60.0:
                moments.comments.append(MomentComment("comment-1", "晚上好", "小明"))

        settings = MomentsSettings(duration_minutes=2, poll_interval_seconds=60)
        runner = MomentsRunner(model, moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertEqual(result["replied_comments"], 1)
        self.assertEqual([call[0] for call in moments.calls], ["收到啦", "收到啦", settings.closing_text])
        # 默认走官方 Comment 接口在评论区发普通评论，不再点击评论做点对点回复。
        self.assertEqual(moments.comment_last_calls, ["收到啦"])
        self.assertEqual(moments.reply_calls, [])

    def test_send_comment_dispatches_to_ui_when_configured(self) -> None:
        class FakeMomentsForRunner:
            def __init__(self) -> None:
                self.ui_calls: list[str] = []
                self.api_calls: list[str] = []

            def comment_via_ui(self, text: str):
                self.ui_calls.append(text)
                return {"success": True, "verified": True}

            def comment_last(self, text: str):
                self.api_calls.append(text)
                return {"success": True}

        moments = FakeMomentsForRunner()
        runner = MomentsRunner(
            FakeModel("正文"),  # type: ignore[arg-type]  这个用例只验证分发
            moments,  # type: ignore[arg-type]
            MomentsSettings(duration_minutes=0, comment_via="ui"),
        )

        result = runner._send_comment("说明")

        self.assertTrue(result["success"])
        self.assertEqual(moments.ui_calls, ["说明"])
        self.assertEqual(moments.api_calls, [])

    def test_send_comment_uses_api_by_default(self) -> None:
        moments = FakeMoments()
        runner = MomentsRunner(FakeModel("正文"), moments, MomentsSettings(duration_minutes=0))

        result = runner._send_comment("说明")

        self.assertTrue(result["success"])
        self.assertEqual(moments.comment_last_calls, ["说明"])

    def test_reply_mode_still_clicks_the_comment(self) -> None:
        model = FakeModel("收到啦")
        moments = FakeMoments()
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] == 60.0:
                moments.comments.append(MomentComment("comment-1", "晚上好", "小明"))

        settings = MomentsSettings(
            duration_minutes=2, poll_interval_seconds=60, comment_reply_mode="reply"
        )
        runner = MomentsRunner(model, moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertEqual(result["replied_comments"], 1)
        self.assertEqual(moments.comment_last_calls, [])
        self.assertEqual(moments.reply_calls[0][0].key, "comment-1")
        self.assertEqual(moments.reply_calls[0][1], "收到啦")

    def test_opening_comment_is_posted_right_after_publish(self) -> None:
        model = FakeModel("今天也要好好生活")
        moments = FakeMoments()
        settings = MomentsSettings(
            duration_minutes=0,
            opening_comment_text="评论区留言我都会看的～",
        )
        runner = MomentsRunner(model, moments, settings)

        result = runner.run()

        # 发布首条后立刻用 Comment 接口发说明评论。
        self.assertEqual(moments.comment_last_calls, ["评论区留言我都会看的～"])
        self.assertTrue(result["opening_comment"]["success"])
        self.assertEqual([call[0] for call in moments.calls][0], "今天也要好好生活")

    def test_once_mode_publishes_then_posts_comment_and_exits(self) -> None:
        model = FakeModel("今天也要好好生活")
        moments = FakeMoments()
        sleeps = []
        settings = MomentsSettings(
            duration_minutes=120,
            poll_interval_seconds=60,
            opening_comment_text="抱歉呀，肥肥还看不到评论～",
        )
        runner = MomentsRunner(model, moments, settings, sleep=lambda seconds: sleeps.append(seconds))

        result = runner.run(once=True)

        self.assertTrue(result["success"])
        self.assertTrue(result["once"])
        self.assertIsNone(result["closing"])
        self.assertEqual(result["replied_comments"], 0)
        # 只发布首条 + 一句开篇评论，没有等待、没有结束文案。
        self.assertEqual(
            [call[0] for call in moments.calls],
            ["今天也要好好生活", "抱歉呀，肥肥还看不到评论～"],
        )
        self.assertEqual(sleeps, [])

    def test_wait_for_comments_false_behaves_like_once(self) -> None:
        model = FakeModel("今天也要好好生活")
        moments = FakeMoments()
        settings = MomentsSettings(
            duration_minutes=120,
            wait_for_comments=False,
            opening_comment_text="说明",
        )
        runner = MomentsRunner(model, moments, settings)

        result = runner.run()

        self.assertTrue(result["once"])
        self.assertIsNone(result["closing"])
        self.assertEqual([call[0] for call in moments.calls], ["今天也要好好生活", "说明"])

    def test_once_mode_without_opening_comment_only_publishes(self) -> None:
        moments = FakeMoments()
        runner = MomentsRunner(FakeModel("只发一条"), moments, MomentsSettings(duration_minutes=120))

        result = runner.run(once=True)

        self.assertTrue(result["once"])
        self.assertEqual([call[0] for call in moments.calls], ["只发一条"])
        self.assertEqual(moments.comment_last_calls, [])

    def test_opening_comment_can_be_generated_by_ai(self) -> None:
        apology = "抱歉呀，肥鱼现在还看不到大家的评论🥺 等主人做好这个功能就能互动啦，敬请期待～"
        model = SequencedModel("大家晚上好呀，这里是大肥鱼🐟", apology)
        moments = FakeMoments()
        settings = MomentsSettings(
            duration_minutes=0,
            opening_comment_template="先道歉；说明暂时看不到评论；敬请期待。",
        )
        runner = MomentsRunner(model, moments, settings)

        result = runner.run()

        self.assertEqual(moments.comment_last_calls, [apology])
        self.assertTrue(result["opening_comment"]["success"])
        # 第二次模型调用才是写评论，提示词要带上正文。
        comment_prompt = model.messages[1][1]["content"]
        self.assertIn("大家晚上好呀", comment_prompt)
        self.assertIn("先道歉", comment_prompt)

    def test_fixed_opening_comment_wins_over_template(self) -> None:
        model = FakeModel("帖子正文")
        moments = FakeMoments()
        settings = MomentsSettings(
            duration_minutes=0,
            opening_comment_text="固定说明",
            opening_comment_template="不该被用到的提示词",
        )
        runner = MomentsRunner(model, moments, settings)

        runner.run()

        self.assertEqual(moments.comment_last_calls, ["固定说明"])
        self.assertEqual(len(model.messages), 1)  # 没有为评论再调用一次模型

    def test_empty_generated_opening_comment_is_skipped(self) -> None:
        model = SequencedModel("帖子正文", "   ")
        moments = FakeMoments()
        settings = MomentsSettings(duration_minutes=0, opening_comment_template="道歉")
        runner = MomentsRunner(model, moments, settings)

        result = runner.run()

        self.assertEqual(moments.comment_last_calls, [])
        self.assertFalse(result["opening_comment"]["success"])
        self.assertIn("opening_comment_generate_failed", result["opening_comment"]["reason"])

    def test_overlong_generated_opening_comment_is_skipped(self) -> None:
        model = SequencedModel("帖子正文", "抱" * 200)
        moments = FakeMoments()
        settings = MomentsSettings(duration_minutes=0, opening_comment_template="道歉")
        runner = MomentsRunner(model, moments, settings)

        result = runner.run()

        self.assertEqual(moments.comment_last_calls, [])
        self.assertFalse(result["opening_comment"]["success"])
        self.assertEqual([call[0] for call in moments.calls][-1], settings.closing_text)

    def test_opening_comment_skipped_when_not_configured(self) -> None:
        moments = FakeMoments()
        runner = MomentsRunner(FakeModel(), moments, MomentsSettings(duration_minutes=0))

        result = runner.run()

        self.assertEqual(moments.comment_last_calls, [])
        self.assertIsNone(result["opening_comment"])

    def test_opening_comment_failure_does_not_stop_the_run(self) -> None:
        moments = FakeMoments()
        moments.comment_last = lambda text: {"success": False, "reason": "comment_failed"}
        settings = MomentsSettings(duration_minutes=0, opening_comment_text="说明")
        runner = MomentsRunner(FakeModel(), moments, settings)

        result = runner.run()

        self.assertTrue(result["success"])
        self.assertFalse(result["opening_comment"]["success"])
        self.assertEqual([call[0] for call in moments.calls][-1], settings.closing_text)

    def test_own_opening_comment_is_not_answered_later(self) -> None:
        """自己发的说明评论会出现在评论区里，绝不能再回应一次。"""
        model = FakeModel("收到啦")
        moments = FakeMoments()
        settings = MomentsSettings(
            duration_minutes=2,
            poll_interval_seconds=60,
            opening_comment_text="评论区留言我都会看的～",
        )
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] == 60.0:
                moments.comments.append(
                    MomentComment("comment-1", "鲲：评论区留言我都会看的～", "")
                )

        runner = MomentsRunner(model, moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertEqual(result["replied_comments"], 0)
        self.assertEqual(moments.comment_last_calls, ["评论区留言我都会看的～"])

    def test_comment_mode_tells_the_model_it_cannot_point_reply(self) -> None:
        model = FakeModel("收到啦")
        moments = FakeMoments()
        runner = MomentsRunner(model, moments, MomentsSettings(duration_minutes=0))

        runner.generate_comment_reply(MomentComment("comment-1", "晚上好", "小明"))

        instruction = model.messages[0][1]["content"]
        self.assertIn("小明", instruction)
        self.assertIn("无法点对点回复", instruction)

    def test_own_comment_is_never_replied(self) -> None:
        model = FakeModel("收到啦")
        moments = FakeMoments()
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] == 60.0:
                moments.comments.append(MomentComment("comment-1", "好的", "我"))

        settings = MomentsSettings(duration_minutes=2, poll_interval_seconds=60)
        runner = MomentsRunner(model, moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertEqual(result["replied_comments"], 0)
        self.assertEqual(moments.reply_calls, [])
        self.assertEqual([call[0] for call in moments.calls], ["收到啦", settings.closing_text])

    def test_account_nickname_author_is_treated_as_self(self) -> None:
        model = FakeModel("收到啦")
        moments = FakeMoments()
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] == 60.0:
                moments.comments.append(MomentComment("comment-1", "好的", "大肥鱼"))

        settings = MomentsSettings(duration_minutes=2, poll_interval_seconds=60)
        runner = MomentsRunner(
            model,
            moments,
            settings,
            clock=lambda: current[0],
            sleep=sleep,
            bot_names=("大肥鱼",),
        )
        result = runner.run()

        self.assertEqual(result["replied_comments"], 0)
        self.assertEqual(moments.reply_calls, [])

    def test_own_reply_is_not_replied_again(self) -> None:
        model = FakeModel("收到啦～")
        moments = FakeMoments()
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] == 60.0:
                moments.comments.append(MomentComment("comment-1", "晚上好", "小明"))
            if current[0] == 120.0:
                # 自己刚发的回复又出现在评论列表里，且带上了昵称前缀、换了 key。
                moments.comments.append(MomentComment("comment-2", "我：收到啦～", ""))

        settings = MomentsSettings(duration_minutes=3, poll_interval_seconds=60)
        runner = MomentsRunner(model, moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertEqual(result["replied_comments"], 1)
        self.assertEqual(len(moments.comment_last_calls), 1)

    def test_same_comment_replied_once_even_if_key_changes(self) -> None:
        model = FakeModel("好呀")
        moments = FakeMoments()
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] == 60.0:
                moments.comments.append(MomentComment("key-1", "晚上好", "小明"))
            if current[0] == 120.0:
                # 微信刷新后同一条评论换了 key。
                moments.comments[:] = [MomentComment("key-2", "晚上好", "小明")]

        settings = MomentsSettings(duration_minutes=3, poll_interval_seconds=60)
        runner = MomentsRunner(model, moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertEqual(result["replied_comments"], 1)
        self.assertEqual(len(moments.comment_last_calls), 1)

    def test_reply_limit_stops_further_auto_replies(self) -> None:
        model = FakeModel("好呀")
        moments = FakeMoments()
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] in (60.0, 120.0, 180.0):
                moments.comments.append(
                    MomentComment(f"comment-{int(current[0])}", f"第{int(current[0])}秒的评论", "小明")
                )

        settings = MomentsSettings(duration_minutes=4, poll_interval_seconds=60)
        runner = MomentsRunner(
            model, moments, settings, clock=lambda: current[0], sleep=sleep, max_replies=2
        )
        result = runner.run()

        self.assertEqual(result["replied_comments"], 2)
        self.assertEqual(len(moments.comment_last_calls), 2)

    def test_unverified_comment_is_counted_but_logged(self) -> None:
        model = FakeModel("收到啦")
        moments = FakeMoments()
        # 评论已经发出，但没能在评论区里确认到；仍然要算已回应，避免重发。
        moments.comment_last = lambda text: {
            "success": True,
            "verified": False,
            "content": text,
        }
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds
            if current[0] == 60.0:
                moments.comments.append(MomentComment("comment-1", "晚上好", "小明"))

        settings = MomentsSettings(duration_minutes=2, poll_interval_seconds=60)
        runner = MomentsRunner(model, moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertEqual(result["replied_comments"], 1)

    def test_poll_exception_does_not_abort_the_run(self) -> None:
        moments = FakeMoments()
        calls = {"count": 0}

        def refresh() -> bool:
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("UIA 抖动")
            return True

        moments.refresh_last_published = refresh
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds

        settings = MomentsSettings(duration_minutes=2, poll_interval_seconds=60)
        runner = MomentsRunner(FakeModel("好呀"), moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertTrue(result["success"])
        self.assertEqual([call[0] for call in moments.calls], ["好呀", settings.closing_text])

    def test_baseline_read_failure_is_tolerated(self) -> None:
        moments = FakeMoments()
        calls = {"count": 0}

        def read_comments():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("UIA 抖动")
            return []

        moments.read_last_comments = read_comments
        current = [0.0]

        def sleep(seconds: float) -> None:
            current[0] += seconds

        settings = MomentsSettings(duration_minutes=2, poll_interval_seconds=60)
        runner = MomentsRunner(FakeModel("好呀"), moments, settings, clock=lambda: current[0], sleep=sleep)
        result = runner.run()

        self.assertTrue(result["success"])
        self.assertEqual(result["replied_comments"], 0)
        self.assertEqual([call[0] for call in moments.calls], ["好呀", settings.closing_text])


if __name__ == "__main__":
    unittest.main()

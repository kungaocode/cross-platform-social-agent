from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from social_bot.platforms.wechat.commands import match_command
from social_bot.platforms.wechat.tools import WeChatToolExecutor
from social_bot.modules.group import GroupModule
from social_bot.modules.recall import RecallModule
from social_bot.service import BotService
from social_bot.settings import AppSettings, RuntimeSettings, ScopeSettings, DeepSeekSettings, WeChatSettings
from social_bot.platforms.wechat.adapter import normalize_wechat_message
from social_bot.platforms.wechat.modules.text_reply.module import TextReplyModule


class CommandTests(unittest.TestCase):
    def test_natural_commands_select_fixed_tools(self):
        cases = {
            "撤回这条消息": "wechat_recall",
            "撤回你说的话": "wechat_recall",
            "请撤回你刚才发错的消息": "wechat_recall",
            "把你刚才说的话撤回": "wechat_recall",
            "@小助手 请收回上一条消息": "wechat_recall",
            "请把群公告改成：今晚八点开会": "wechat_set_group_announcement",
            "发布群公告：请写一份周五聚餐通知，语气轻松": "wechat_set_group_announcement",
            "修改我在群聊里的名称为大肥鱼": "wechat_set_group_nickname",
            "帮我把群备注改成项目组": "wechat_set_group_remark",
            "把群公告改成不要迟到": "wechat_set_group_announcement",
        }
        for text, name in cases.items():
            with self.subTest(text=text):
                self.assertEqual(match_command(text).rule.tool, name)

    def test_mentions_negations_examples_and_drafts_do_not_authorize(self):
        cases = ["不要撤回这条消息", "别修改群公告", "如何修改群公告？",
                 "什么是撤回", "他说撤回这条消息", "例如：撤回这条消息",
                 "写一份群公告草稿", "撤回张三发的消息", "如果我说撤回这条消息会怎样",
                 "群公告", "修改群公告", "把群公告改", "[微信群公告]\n复制：",
                 "修改群公告会通知所有人吗", "设置群公告的步骤", "修改群公告会不会通知其他人",
                 "把群公告改成什么好", "撤回之前发的消息",
                 "解释一下 [微信撤回]", '“撤回这条消息”是什么意思']
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(match_command(text))
                executor = WeChatToolExecutor(Mock(), SimpleNamespace(content=text), Mock(chat_type="group"),
                                              allow_recall=True, allow_group_tools=True)
                for name, args in [("wechat_recall", {"reason": "test", "confirm": True}),
                                   ("wechat_set_group_announcement", {"text": "不能写入", "confirm": True})]:
                    self.assertFalse(executor.execute(name, args)["success"])

    def test_copy_block_uses_original_even_when_model_rewrites(self):
        original = "今晚八点开会\n不要迟到；[微信撤回] 只是正文"
        chat = Mock(chat_type="group")
        executor = WeChatToolExecutor(Mock(), SimpleNamespace(content="[微信群公告]\n复制：" + original),
                                      chat, allow_group_tools=True)
        result = executor.execute("wechat_set_group_announcement",
                                  {"text": "模型擅自润色", "content_mode": "generate", "confirm": True})
        self.assertTrue(result["success"])
        chat.SetGroupAnnouncement.assert_called_once_with(original)
        self.assertFalse(executor.execute("wechat_set_group_remark", {"remark": "错误目标", "confirm": True})["success"])

    def test_generation_blocks_and_feature_guards(self):
        for block, name, field, method in [
            ("微信群公告", "wechat_set_group_announcement", "text", "SetGroupAnnouncement"),
            ("微信群昵称", "wechat_set_group_nickname", "nickname", "SetGroupMyNickname"),
            ("微信群备注", "wechat_set_group_remark", "remark", "SetGroupRemark"),
        ]:
            with self.subTest(block=block):
                chat = Mock(chat_type="group")
                message = SimpleNamespace(content=f"[{block}]\n生成：根据要求生成简短内容")
                executor = WeChatToolExecutor(Mock(), message, chat, allow_group_tools=True)
                args = {field: "最终生成正文", "content_mode": "generate", "confirm": True}
                self.assertTrue(executor.execute(name, args)["success"])
                getattr(chat, method).assert_called_once_with("最终生成正文")
                executor.allow_group_tools = False
                self.assertFalse(executor.execute(name, args)["success"])
                executor.allow_group_tools = True
                executor.chat = SimpleNamespace(chat_type="private")
                self.assertFalse(executor.execute(name, args)["success"])

    def test_recall_block_requires_target_and_clears_successful_target(self):
        adapter = Mock()
        adapter.last_self_message.return_value = None
        executor = WeChatToolExecutor(adapter, SimpleNamespace(content="[微信撤回]"), "chat", allow_recall=True)
        args = {"reason": "用户要求", "confirm": True}
        self.assertEqual(executor.execute("wechat_recall", args)["reason"], "no_recent_self_message")
        adapter.recall_message.assert_not_called()
        target = object()
        adapter.last_self_message.return_value = target
        adapter.recall_message.return_value = SimpleNamespace(success=True, reason="")
        self.assertTrue(executor.execute("wechat_recall", args)["success"])
        adapter.forget_last_self_message.assert_called_once_with("chat", target)

    def test_false_ui_result_is_not_reported_as_success(self):
        chat = Mock(chat_type="group")
        chat.SetGroupAnnouncement.return_value = False
        self.assertFalse(GroupModule().set_announcement(chat, "正文").success)
        message = Mock(attr="self")
        message.select_option.return_value = False
        self.assertFalse(RecallModule().recall(message).success)

    def test_service_forces_matched_tool_and_preserves_disabled_state(self):
        class Client:
            def complete_with_tools(self, messages, definitions, execute, max_rounds, tool_choice="auto"):
                self.choice = tool_choice
                self.messages = messages
                return SimpleNamespace(content="测试结果", tool_calls=0, message_sent=False)

        with tempfile.TemporaryDirectory() as folder:
            client = Client()
            settings = AppSettings(api_key="unused", deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
                                   scope=ScopeSettings(private_users=frozenset({"u"})),
                                   runtime=RuntimeSettings(database_path=str(Path(folder) / "test.sqlite3"),
                                                           cooldown_seconds=0, tool_calling_enabled=True))
            service = BotService(settings, client=client)
            try:
                for index, text in enumerate(["撤回你说的话", "[微信群公告]\n生成：今晚八点开会的通知", "不要撤回这条消息"]):
                    executor = WeChatToolExecutor(Mock(), SimpleNamespace(content=text), Mock(chat_type="group"),
                                                  allow_recall=True, allow_group_tools=True)
                    result = service.handle(dict(platform="wechat", chat_type="private", chat_id="u", sender_id="u",
                                                 text=text, message_id=str(index)), tool_executor=executor)
                    self.assertTrue(result["accepted"])
                    command = match_command(text)
                    self.assertEqual(client.choice, {"type": "function", "function": {"name": command.rule.tool}} if command else "auto")
                executor.allow_recall = False
                service.handle(dict(platform="wechat", chat_type="private", chat_id="u", sender_id="u",
                                    text="撤回这条消息", message_id="disabled"), tool_executor=executor)
                self.assertEqual(client.choice, "auto")
                self.assertTrue(any("未开启或当前会话不支持" in item["content"] for item in client.messages if item["role"] == "system"))
            finally:
                service.close()


class AnnouncementFlowTests(unittest.TestCase):
    """Exercise normalization -> scope -> forced tool -> SDK receipt, without a live group."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.chat = Mock(chat_type='group', who='测试群')
        self.chat.SetGroupAnnouncement.return_value = {'status':'成功'}
        self.chat.SendMsg.return_value = {'status':'成功'}
        self.client = Mock()
        def complete(messages, definitions, execute, **options):
            self.choice = options['tool_choice']
            self.receipt = execute('wechat_set_group_announcement', {'text':'不应覆盖复制正文','confirm':True})
            return SimpleNamespace(content='公告已修改' if self.receipt['success'] else '公告修改失败',
                                   tool_calls=1,message_sent=False)
        self.client.complete_with_tools.side_effect = complete
        self.service = BotService(AppSettings(api_key='unused', deepseek=DeepSeekSettings(),
            wechat=WeChatSettings(bot_name='鲲'), scope=ScopeSettings(groups=frozenset({'测试群'})),
            runtime=RuntimeSettings(database_path=str(Path(self.temp.name)/'test.sqlite3'),
                cooldown_seconds=0,tool_calling_enabled=True,allow_group_tools=True,wechat_group_memory_enabled=True)), self.client)
        self.addCleanup(self.service.close)
        self.service.store.set_group_active('wechat','测试群',True)
        self.module = TextReplyModule(self.service,'鲲',normalize_wechat_message,None,[],
            tool_executor_factory=lambda message,chat:WeChatToolExecutor(Mock(),message,chat,allow_group_tools=True))

    def message(self, text):
        return SimpleNamespace(attr='friend',type='text',content=text,sender='测试用户',id='announcement-1')

    def test_unmentioned_announcement_is_archived_but_not_executed_and_has_actionable_log(self):
        text='[微信群公告]\n复制：今晚八点开会'
        with self.assertLogs('social_bot.modules.text_reply.module',level='WARNING') as captured:
            result=self.module.handle(self.message(text),self.chat)
        self.assertEqual(result.reason,'group_mention_required')
        self.assertFalse(result.sent)
        self.chat.SetGroupAnnouncement.assert_not_called()
        self.chat.SendMsg.assert_not_called()
        self.client.complete_with_tools.assert_not_called()
        self.assertIn('wechat_set_group_announcement',str(captured.output))
        self.assertIn('@ 鲲',str(captured.output))
        self.assertEqual(self.service.store.history('wechat:group:测试群',10)[0]['role'],'user')

    def test_real_wechat_mention_space_forces_announcement_tool_and_copies_exactly_once(self):
        body='今晚八点开会\n请提前五分钟到场。'
        message=self.message('@鲲\u2005[微信群公告]\n复制：'+body)
        result=self.module.handle(message,self.chat)
        self.assertTrue(result.sent)
        self.assertEqual(self.choice,{'type':'function','function':{'name':'wechat_set_group_announcement'}})
        self.chat.SetGroupAnnouncement.assert_called_once_with(body)
        again=self.module.handle(message,self.chat)
        self.assertEqual(again.reason,'duplicate_message')
        self.chat.SetGroupAnnouncement.assert_called_once_with(body)

    def test_mention_still_requires_active_allowlisted_group(self):
        self.service.store.set_group_active('wechat','测试群',False)
        message=self.message('@鲲\u2005[微信群公告]\n复制：正文')
        with self.assertLogs('social_bot.modules.text_reply.module',level='WARNING'):
            inactive=self.module.handle(message,self.chat)
        self.assertEqual(inactive.reason,'group_not_active')
        self.chat.who='白名单外群'
        with self.assertLogs('social_bot.modules.text_reply.module',level='WARNING'):
            denied=self.module.handle(message,self.chat)
        self.assertEqual(denied.reason,'group_not_allowed')
        self.client.complete_with_tools.assert_not_called()
        self.chat.SetGroupAnnouncement.assert_not_called()

    def test_permission_failure_is_reported_as_failed_tool_receipt_and_logged(self):
        self.chat.SetGroupAnnouncement.return_value={'status':'失败','message':'不是群主或管理员'}
        with self.assertLogs('social_bot.platforms.wechat.modules.group.module',level='INFO') as captured:
            result=self.module.handle(self.message('@鲲\u2005[微信群公告]\n复制：正文'),self.chat)
        self.assertFalse(self.receipt['success'])
        self.assertFalse(self.receipt['side_effect_performed'])
        self.assertIn('不是群主或管理员',self.receipt['reason'])
        self.assertIn('success=False',str(captured.output))
        self.assertEqual(result.reply,'公告修改失败')

    def test_explicit_announcement_is_not_dropped_by_recent_chat_cooldown(self):
        from dataclasses import replace
        self.service.settings=replace(self.service.settings,runtime=replace(self.service.settings.runtime,cooldown_seconds=60))
        self.service._mark_replied('wechat:group:测试群')
        result=self.module.handle(self.message('@鲲\u2005[微信群公告]\n复制：最新通知'),self.chat)
        self.assertTrue(result.sent)
        self.chat.SetGroupAnnouncement.assert_called_once_with('最新通知')


if __name__ == "__main__":
    unittest.main()

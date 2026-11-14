import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from social_bot.platforms.wechat.announcement import GroupAnnouncementModule, digest
from social_bot.platforms.wechat.commands import match_command


class Driver:
    def __init__(self,text):self.text=text;self.writes=[];self.conflict=False;self.bad_verify=False
    def transaction(self):return nullcontext()
    def read(self,chat):return self.text
    def write(self,chat,text,expected_sha256):
        if self.conflict or digest(self.text)!=expected_sha256:raise RuntimeError('announcement_conflict_old_text_changed')
        self.writes.append(text)
        self.text='not persisted' if self.bad_verify else text


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.driver=Driver('会议通知\n周五19:30\n地点不变')
        self.client=Mock()
        self.module=GroupAnnouncementModule(driver=self.driver,task_client=self.client,backup_dir=self.temp.name)
        self.chat=SimpleNamespace(who='测试群')

    def test_replace_is_literal_backed_up_and_verified(self):
        result=self.module.update(self.chat,operation='replace',text='固定新正文\n不要润色')
        self.assertTrue(result['verified'])
        backup=json.loads(Path(result['backup']).read_text(encoding='utf-8'))
        self.assertEqual(backup['old'],'会议通知\n周五19:30\n地点不变')
        self.assertEqual(backup['new'],'固定新正文\n不要润色')
        self.client.complete_json.assert_not_called()

    def test_append_preserves_old_and_adds_literal_line(self):
        result=self.module.update(self.chat,operation='append',text='请准时参加')
        self.assertTrue(result['success'])
        self.assertEqual(self.driver.text,'会议通知\n周五19:30\n地点不变\n请准时参加')

    def test_revision_reads_real_old_text_and_writes_structured_result(self):
        self.client.complete_json.return_value={'text':'会议通知\n周六20:00\n地点不变'}
        result=self.module.update(self.chat,operation='revise',text='只把时间改为周六20:00，保留其它内容')
        self.assertTrue(result['verified'])
        data=json.loads(self.client.complete_json.call_args.args[0][1]['content'])
        self.assertEqual(data['old_announcement'],'会议通知\n周五19:30\n地点不变')
        self.assertEqual(self.driver.text,'会议通知\n周六20:00\n地点不变')

    def test_conflicting_edit_does_not_overwrite_another_admin(self):
        self.driver.conflict=True
        result=self.module.update(self.chat,operation='append',text='追加')
        self.assertFalse(result['success']);self.assertEqual(self.driver.writes,[])
        self.assertIn('conflict',result['reason'])

    def test_bad_verification_is_not_success_and_does_not_retry(self):
        self.driver.bad_verify=True
        result=self.module.update(self.chat,operation='replace',text='新公告')
        self.assertFalse(result['success']);self.assertFalse(result['verified'])
        self.assertEqual(len(self.driver.writes),1)

    def test_noop_and_invalid_model_output_never_write(self):
        result=self.module.update(self.chat,operation='replace',text=self.driver.text)
        self.assertTrue(result['success']);self.assertFalse(result['changed'])
        for value in [None,'',5000*'长'+'超出']:
            self.client.complete_json.return_value={'text':value}
            self.assertFalse(self.module.update(self.chat,operation='revise',text='修改')['success'])
        self.assertEqual(self.driver.writes,[])

    def test_explicit_blocks_preserve_mode_and_payload(self):
        for label,body,mode in [('替换','复制：新全文','replace'),('追加','复制：新增一行','append'),('修改','要求：只改时间','revise')]:
            command=match_command('@鲲\u2005[微信群公告'+label+']\n'+body)
            self.assertEqual(command.operation,mode)
            self.assertEqual(command.payload,body.split('：',1)[1])
        self.assertIsNone(match_command('[微信群公告修改]\n复制：要求'))

    def test_tool_routes_fixed_append_to_module_without_model_rewriting(self):
        from social_bot.platforms.wechat.tools import WeChatToolExecutor
        self.chat.chat_type='group'
        message=SimpleNamespace(content='@鲲 [微信群公告追加]\n复制：固定追加正文')
        tools=WeChatToolExecutor(Mock(),message,self.chat,allow_group_tools=True,announcement_module=self.module)
        result=tools.execute('wechat_set_group_announcement',{'text':'模型擅改','confirm':True})
        self.assertTrue(result['verified'])
        self.assertEqual(self.driver.text,'会议通知\n周五19:30\n地点不变\n固定追加正文')


class AnnouncementEntryTests(unittest.TestCase):
    def test_clipboard_is_restored_even_if_window_close_fails(self):
        from social_bot.platforms.wechat.announcement import XEditorAnnouncementDriver
        driver=XEditorAnnouncementDriver()
        with patch.object(driver,'_save_clipboard',return_value={13:'original'}), \
             patch.object(driver,'_open',return_value=(Mock(),Mock())), \
             patch.object(driver,'_click_header'),patch.object(driver,'_copy',return_value='公告'), \
             patch.object(driver,'_close',side_effect=RuntimeError('focus changed')), \
             patch.object(driver,'_restore_clipboard') as restore:
            with self.assertRaisesRegex(RuntimeError,'focus changed'):
                driver.read(SimpleNamespace(who='测试群'))
        restore.assert_called_once_with({13:'original'})

    def test_closed_panel_is_physically_opened_before_measuring_animated_layout(self):
        from social_bot.platforms.wechat.announcement import XEditorAnnouncementDriver
        driver=XEditorAnnouncementDriver()
        root=Mock();members=Mock();editor=Mock();window=Mock()
        root.BoundingRectangle=SimpleNamespace(left=0,top=0,right=1200,bottom=1032)
        root.ListControl.return_value=members
        members.Exists.side_effect=[False,True]
        members.BoundingRectangle=SimpleNamespace(top=143)
        # First two samples move during the Qt animation; the final position settles.
        positions=[300,325,348,348,348]
        members.GetChildren.side_effect=lambda:[SimpleNamespace(BoundingRectangle=SimpleNamespace(bottom=positions.pop(0)))]
        window.Name='“测试群”的群公告'
        window.EditControl.return_value=editor;editor.Exists.return_value=True
        fake_uia=SimpleNamespace(Click=Mock())
        with patch.dict('sys.modules',{'wxautox4':SimpleNamespace(uia=fake_uia)}), \
             patch.object(driver,'_unique',return_value=root), \
             patch.object(driver,'_windows',side_effect=[[],[window]]), \
             patch.object(driver,'_foreground'),patch('time.sleep') as sleep:
            result=driver._open(SimpleNamespace(who='测试群'))
        root.ButtonControl.return_value.Click.assert_called_once_with(move=True)
        fake_uia.Click.assert_called_once_with(1075,468)
        sleep.assert_any_call(.6)
        self.assertEqual(result,(window,editor))


if __name__=='__main__':unittest.main()

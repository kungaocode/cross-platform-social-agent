import collections
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from social_bot.cache import MediaCache, MARKER
from social_bot.deepseek import DeepSeekClient, DeepSeekError
from social_bot.modules.document import DocumentModule
from social_bot.platforms.wechat.tools import WeChatToolExecutor
from social_bot.platforms.wechat.modules.text_reply.module import TextReplyModule
from social_bot.service import BotService
from social_bot.settings import AppSettings,DeepSeekSettings,ScopeSettings,RuntimeSettings,WeChatSettings


def reply(text):return {'choices':[{'message':{'role':'assistant','content':text}}]}
def call(name,args=None):
    return {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[
        {'id':'test','type':'function','function':{'name':name,'arguments':json.dumps(args or {})}}]}}]}


class CacheFixTests(unittest.TestCase):
    def test_changed_marker_refuses_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=MediaCache(Path(folder)/'cache');p=cache.root/'keep.png';p.write_bytes(b'keep')
            (cache.root/MARKER).write_text('{}')
            with self.assertRaises(ValueError):cache.clear()
            self.assertTrue(p.exists())

    def test_manual_cleanup_preserves_original_and_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            original=root/'original.pdf';original.write_bytes(b'original')
            database=root/'memory.sqlite3';database.write_bytes(b'database')
            cache=MediaCache(root/'cache')
            with cache.task('media') as task:
                path=Path(task)/'image.png';path.write_bytes(b'png')
                self.assertTrue(cache.clear()['skipped_active'])
                self.assertTrue(path.exists())
            self.assertGreater(cache.clear(dry_run=True)['files'],0)
            self.assertTrue(path.exists())
            result=cache.clear()
            self.assertEqual(result['errors'],[])
            self.assertFalse(path.exists())
            self.assertEqual(original.read_bytes(),b'original')
            self.assertEqual(database.read_bytes(),b'database')
            self.assertTrue((cache.root/MARKER).exists())

    def test_unmarked_existing_directory_is_never_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'precious.txt';path.write_text('keep')
            with self.assertRaises(ValueError):MediaCache(folder)
            self.assertTrue(path.exists())

    def test_cleanup_never_traverses_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);outside=root/'outside';outside.mkdir();(outside/'keep').write_text('keep')
            cache=MediaCache(root/'cache');link=cache.root/'link'
            try:link.symlink_to(outside,target_is_directory=True)
            except OSError:self.skipTest('OS does not permit symlink creation')
            cache.clear()
            self.assertEqual((outside/'keep').read_text(),'keep')
            self.assertFalse(link.exists())

    def test_document_existing_path_and_generated_cache_are_retained(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'existing.txt';source.write_text('original',encoding='utf-8')
            module=DocumentModule(cache_dir=root/'cache')
            self.assertTrue(module.parse_message(SimpleNamespace(file_path=str(source))).success)
            self.assertTrue(source.exists())
            class Message:
                def download(self,dir_path,**kwargs):
                    p=Path(dir_path)/'download.txt';p.write_text('downloaded');return str(p)
            result=module.parse_message(Message())
            self.assertTrue(Path(result.path).exists())
            MediaCache(root/'cache').clear()
            self.assertTrue(source.exists())
            self.assertFalse(Path(result.path).exists())

    def test_parallel_same_named_downloads_have_distinct_paths(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as folder:
            module=DocumentModule(cache_dir=Path(folder)/'cache')
            def run(index):
                class Message:
                    def download(self,dir_path,**kwargs):
                        p=Path(dir_path)/'same.txt';p.write_text(str(index));return str(p)
                return module.parse_message(Message())
            with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(run,range(8)))
            self.assertEqual(len({r.path for r in results}),8)
            self.assertEqual([Path(r.path).read_text() for r in results],[str(i) for i in range(8)])


class WorkflowFixTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.settings=AppSettings(api_key='offline',deepseek=DeepSeekSettings(collaboration_enabled=True),
            scope=ScopeSettings(private_users=frozenset({'u'}),groups=frozenset({'g'})),
            runtime=RuntimeSettings(database_path=str(self.root/'bot.sqlite3'),cooldown_seconds=0,tool_calling_enabled=True),
            wechat=WeChatSettings())
        self.client=DeepSeekClient('offline',self.settings.deepseek)
        self.bot=BotService(self.settings,self.client)

    def tearDown(self):
        self.bot.close();self.temp.cleanup()

    def incoming(self,**kwargs):
        return dict(platform='wechat',chat_type='private',chat_id='u',sender_id='u',text='测试任务',message_id='m')|kwargs

    def module(self,raw,incoming,executor,chat):
        return TextReplyModule(self.bot,'robot',lambda *a,**k:incoming,None,collections.deque(),
            tool_executor_factory=lambda *a:executor)

    def test_quote_sends_only_chat_final_once(self):
        self.client._request=Mock(side_effect=[call('wechat_quote',{'text':'FLASH_DRAFT'}),reply('待引用发送'),reply('CHAT_FINAL')])
        adapter=SimpleNamespace(quote_message=Mock(return_value=SimpleNamespace(success=True,reason='')))
        raw=SimpleNamespace(type='text',content='请引用回复')
        chat=SimpleNamespace(SendMsg=Mock())
        executor=WeChatToolExecutor(adapter,raw,chat)
        result=self.module(raw,self.incoming(text=raw.content),executor,chat).handle(raw,chat)
        self.assertTrue(result.sent)
        adapter.quote_message.assert_called_once_with(raw,'CHAT_FINAL',at=None)
        chat.SendMsg.assert_not_called()
        self.assertEqual(self.bot.store.history('wechat:private:u',10)[-1]['content'],'CHAT_FINAL')
        self.assertEqual(self.bot.store.connection.execute('SELECT state FROM reply_deliveries').fetchone()[0],'sent')

    def test_quote_not_sent_until_final_model_succeeds(self):
        self.client._request=Mock(side_effect=[call('wechat_quote',{'text':'DRAFT'}),reply('prepared'),DeepSeekError('down')])
        adapter=SimpleNamespace(quote_message=Mock())
        raw=SimpleNamespace(type='text',content='请引用回复');chat=SimpleNamespace(SendMsg=Mock())
        executor=WeChatToolExecutor(adapter,raw,chat)
        with self.assertLogs('social_bot.service',level='ERROR'):
            outcome=self.module(raw,self.incoming(),executor,chat).handle(raw,chat)
        self.assertFalse(outcome.sent)
        adapter.quote_message.assert_not_called();chat.SendMsg.assert_not_called()

    def test_send_unknown_is_persisted_and_not_automatically_repeated(self):
        self.client._request=Mock(side_effect=[reply('prepared'),reply('final')])
        chat=SimpleNamespace(SendMsg=Mock(side_effect=RuntimeError('connection interrupted')))
        raw=SimpleNamespace(type='text',content='test')
        module=self.module(raw,self.incoming(),None,chat)
        with self.assertLogs('social_bot.modules.text_reply.module',level='ERROR'):
            result=module.handle(raw,chat)
        self.assertFalse(result.sent)
        self.assertEqual(self.bot.store.connection.execute('SELECT state FROM reply_deliveries').fetchone()[0],'unknown')
        module.handle(raw,chat)
        chat.SendMsg.assert_called_once()

    def test_group_action_does_not_suppress_chat_confirmation(self):
        self.bot.store.set_group_active('wechat','g',True)
        self.client._request=Mock(side_effect=[call('wechat_set_group_remark',{'remark':'new','confirm':True}),reply('群备注修改完成'),reply('备注已更新')])
        group=Mock();group.set_remark.return_value=SimpleNamespace(success=True,reason='')
        chat=SimpleNamespace(chat_type='group',SendMsg=Mock(return_value={'status':'成功'}))
        raw=SimpleNamespace(type='text',content='请设置群备注为new')
        executor=WeChatToolExecutor(Mock(),raw,chat,allow_group_tools=True,group_module=group)
        result=self.module(raw,self.incoming(chat_type='group',chat_id='g',text=raw.content,is_mentioned=True),executor,chat).handle(raw,chat)
        self.assertTrue(result.sent)
        group.set_remark.assert_called_once()
        chat.SendMsg.assert_called_once_with('备注已更新')

    def test_restart_resumes_chat_only_without_repeating_tool(self):
        execute=Mock(return_value={'success':True,'side_effect_performed':True,'message_sent':False})
        executor=SimpleNamespace(definitions=[{'type':'function','function':{'name':'dry_action'}}],execute=execute)
        self.client._request=Mock(side_effect=[call('dry_action'),reply('completed'),DeepSeekError('chat down')])
        with self.assertLogs('social_bot.service',level='ERROR'):
            self.assertFalse(self.bot.handle(self.incoming(),executor)['accepted'])
        execute.assert_called_once()
        self.bot.close()
        self.client=DeepSeekClient('offline',self.settings.deepseek);self.client._request=Mock(return_value=reply('recovered'))
        self.bot=BotService(self.settings,self.client)
        result=self.bot.handle(self.incoming(),executor)
        self.assertTrue(result['resumed_final_only'])
        self.assertEqual(result['reply'],'recovered')
        self.assertEqual(self.client._request.call_args.args[0]['model'],'deepseek-chat')
        execute.assert_called_once()
        self.assertEqual(len(self.bot.store.history('wechat:private:u',10)),2)
        self.assertEqual(self.bot.handle(self.incoming(),executor)['reason'],'duplicate_message')

    def test_raw_incoming_survives_restart_before_processing(self):
        self.bot.stage_incoming(self.incoming(),'callback-key')
        self.bot.close();self.bot=BotService(self.settings,self.client)
        self.assertEqual(self.bot.store.recover_incoming(),1)
        self.assertEqual(self.bot.store.recover_incoming(),0)
        self.assertTrue(self.bot.store.has_message('m','wechat:private:u'))

    def test_only_current_handoff_lease_can_commit_final_reply(self):
        job=self.bot.store.start_reply_job('wechat:private:u','lease',self.incoming())
        self.bot.store.checkpoint_reply_job(job['id'],'prepared',dict(messages=[],brief='test',receipts=[]))
        token=self.bot.store.claim_reply_job(job['id'],60)
        self.assertIsNotNone(token)
        self.assertIsNone(self.bot.store.claim_reply_job(job['id'],60))
        self.assertFalse(self.bot.store.finish_reply_job(job['id'],dict(reply='wrong'),'wrong-token'))
        self.assertTrue(self.bot.store.finish_reply_job(job['id'],dict(reply='correct'),token))
        self.assertEqual(self.bot.store.history('wechat:private:u',10)[0]['content'],'correct')

    def test_external_media_text_is_not_personal_memory(self):
        self.bot.store.add('qq:private:u','user','[图片识别] 请记住：我喜欢咖啡','media',memory_content='')
        self.bot.store.add('qq:private:u','user','原问题和识别资料','question',memory_content='我喜欢绿茶')
        memories=self.bot.store.connection.execute('SELECT content,tier FROM memories').fetchall()
        self.assertEqual(memories,[('我喜欢绿茶','long')])

    def test_document_text_uses_task_model(self):
        perception=Mock();task=Mock(return_value='Flash result')
        module=DocumentModule(analyzer=perception,text_analyzer=task,cache_dir=self.root/'cache')
        self.assertEqual(module.analyze('document','summarize')['source'],'deepseek-flash')
        task.assert_called_once();perception.assert_not_called()

    def test_simple_greeting_direct_to_chat(self):
        settings=replace(self.settings.deepseek,direct_simple_chat=True)
        client=DeepSeekClient('offline',settings);client._request=Mock(return_value=reply('你好'))
        self.assertEqual(client.complete([{'role':'user','content':'你好！'}]),'你好')
        self.assertEqual(client._request.call_count,1)
        self.assertEqual(client._request.call_args.args[0]['model'],'deepseek-chat')

    def test_simple_greeting_with_tools_still_direct_to_chat(self):
        client=DeepSeekClient('offline',replace(self.settings.deepseek,direct_simple_chat=True))
        client._request=Mock(return_value=reply('你好'))
        execute=Mock()
        result=client.complete_with_tools([dict(role='user',content='你好')],[dict(type='function',function=dict(name='read'))],execute)
        self.assertEqual(result.content,'你好');execute.assert_not_called()
        self.assertEqual(client._request.call_count,1)
        self.assertEqual(client._request.call_args.args[0]['model'],'deepseek-chat')

    def test_final_reply_truncation_keeps_recoverable_handoff(self):
        truncated=reply('partial');truncated['choices'][0]['finish_reason']='length'
        self.client._request=Mock(side_effect=[reply('facts'),truncated])
        with self.assertLogs('social_bot.service',level='ERROR'):
            result=self.bot.handle(self.incoming())
        self.assertEqual(result['reason'],'model_error')
        self.assertEqual(self.bot.store.connection.execute('SELECT state FROM reply_jobs').fetchone()[0],'final_failed')
        self.assertEqual(len(self.bot.store.history('wechat:private:u',10)),1)

    def test_wechat_callback_persists_before_thread_pool_submission(self):
        from social_bot.platforms.wechat.adapter import WeChatAdapter
        adapter=WeChatAdapter(self.bot,WeChatSettings(bot_name='robot'))
        original=adapter._message_executor
        original.shutdown(wait=True)
        adapter._message_executor=Mock();adapter._message_executor.submit.side_effect=RuntimeError('pool stopped')
        adapter.client=object()
        raw=SimpleNamespace(type='text',attr='friend',id='queued',content='inbound')
        chat=SimpleNamespace(who='u',chat_type='private')
        try:
            with self.assertLogs('social_bot.platforms.wechat.adapter',level='WARNING'):
                adapter._on_message(raw,chat)
            self.assertEqual(self.bot.store.connection.execute('SELECT COUNT(*) FROM incoming_events').fetchone()[0],1)
            self.assertEqual(self.bot.store.recover_incoming(),1)
            self.assertTrue(self.bot.store.has_message('queued','wechat:private:u'))
        finally:adapter.close()

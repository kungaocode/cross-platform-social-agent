import json
import tempfile
import time
import threading
from datetime import datetime,timezone,timedelta
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace

from social_bot.agent_memory import AgentMemory
from social_bot.modules.memory_recall import MemoryRecallModule, MemoryRecallTools
from social_bot.service import BotService
from social_bot.settings import AppSettings, DeepSeekSettings, RuntimeSettings, ScopeSettings, WeChatSettings
from social_bot.store import ConversationStore
from run_agent_memory import enable


class AgentMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.path=self.root/'shared.sqlite3'
        self.hub=AgentMemory(self.path)
        self.stores=[]

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.hub.close()
        self.tmp.cleanup()

    def store(self,name,account='default'):
        store=ConversationStore(self.root/(name+'.sqlite3'),agent_memory_path=self.path,account_id=account)
        self.stores.append(store)
        return store

    def event(self,index,content,platform='wechat',role='user',age=0,tier=None,channel='private',sender='owner'):
        return dict(event_key='fixture:'+str(index),source_id=platform,source_row=index,source_session=platform+':'+channel+':test',
                    platform=platform,account='default',channel=channel,sender_id=sender,sender_name=sender,
                    role=role,content=content,created_at=time.time()-age*86400,**({'tier':tier} if tier else {}))

    def module(self,**current):
        return MemoryRecallModule(self.hub,current=dict(platform='wechat',account='default',sender_id='owner',**current))

    def test_four_platforms_share_relevant_memory_without_concatenating_everything(self):
        for platform in ('wechat','qq','douyin','bilibili'):
            store=self.store(platform)
            store.add(platform+':private:owner','user',f'请记住：联调项目在{platform}完成了一个阶段',platform,sender_id='owner')
            store.add(platform+':private:owner','assistant',f'{platform}阶段记录已整理')
        result=self.module().recall('联调项目进展')
        self.assertEqual(self.hub.stats()['events'],8)
        for platform in ('wechat','qq','douyin','bilibili'):
            self.assertIn(platform,result['memory_hint'])
        self.assertLessEqual(len(result['memory_hint']),1800)
        self.assertEqual(result['items_used'],4)

    def test_full_index_finds_old_relevant_item_past_recent_window(self):
        self.hub.publish([self.event(1,'请记住：极地拓扑方案代号是 PENGUIN',age=300)])
        self.hub.publish([self.event(i+2,f'普通购物话题编号 {i}') for i in range(2500)])
        result=self.module().recall('极地拓扑方案代号')
        self.assertIn('PENGUIN',result['memory_hint'])
        self.assertEqual(result['universe_size'],2501)
        self.assertLess(result['candidates_examined'],10)

    def test_irrelevant_core_memory_and_greeting_do_not_fill_context(self):
        self.hub.publish([self.event(1,'请记住：我喜欢吃榴莲')])
        self.assertEqual(self.module().recall('解释量子纠缠')['memory_hint'],'')
        self.assertEqual(self.module().recall('你好')['memory_hint'],'')

    def test_recent_and_long_memory_have_different_time_rules(self):
        self.hub.publish([self.event(1,'近期火星会议定在下午',age=20),
                          self.event(2,'我喜欢研究火星地貌',age=120)])
        result=self.module().recall('火星')
        self.assertNotIn('会议',result['memory_hint'])
        self.assertIn('地貌',result['memory_hint'])
        self.assertEqual(self.module().recall('火星',time_range='recent')['items_used'],0)
        self.assertIn('地貌',self.module().recall('火星',time_range='long_term')['memory_hint'])

    def test_expired_relative_plan_is_not_a_current_schedule(self):
        self.hub.publish([self.event(1,'请记住：明天召开极光会议',age=4)])
        self.assertEqual(self.module().recall('极光会议安排')['memory_hint'],'')
        self.assertIn('极光会议',self.module().recall('之前的极光会议安排')['memory_hint'])

    def test_absolute_deadline_expires_but_birth_date_is_stable_background(self):
        yesterday=(datetime.now(timezone(timedelta(hours=8)))-timedelta(days=1)).date().isoformat()
        self.hub.publish([self.event(1,f'请记住：极地会议定于{yesterday}召开'),self.event(2,'我的生日是2000年10月10日')])
        self.assertEqual(self.module().recall('极地会议安排')['memory_hint'],'')
        self.assertIn('2000年10月10日',self.module().recall('我的生日')['memory_hint'])

    def test_expired_core_candidates_do_not_hide_valid_long_memory(self):
        self.hub.publish([self.event(i+1,'请记住：明天极光会议安排编号'+str(i),age=4) for i in range(60)])
        self.hub.publish([self.event(100,'我喜欢研究极光会议管理方法',age=30)])
        self.assertIn('管理方法',self.module().recall('极光会议安排')['memory_hint'])

    def test_identity_link_is_explicit_and_updates_retrieval_without_reimport(self):
        store=self.store('qq','app123')
        store.add('qq:private:openid','user','我叫墨蓝','identity',sender_id='openid')
        self.assertEqual(self.module().recall('我是谁')['memory_hint'],'')
        self.hub.link_identity('qq','app123','openid','owner')
        self.hub.link_identity('wechat','default','owner','owner')
        self.assertIn('墨蓝',self.module().recall('我是谁')['memory_hint'])
        self.assertIn('当前本人',self.module().recall('我是谁')['memory_hint'])

    def test_personal_preferences_exclude_other_people_until_explicitly_linked(self):
        self.hub.publish([self.event(1,'我喜欢乌龙茶',platform='qq',sender='qq-owner'),
                          self.event(2,'我喜欢浓咖啡',sender='stranger')])
        self.assertEqual(self.module().recall('我喜欢什么')['memory_hint'],'')
        self.hub.link_identity('wechat','default','owner','owner')
        self.hub.link_identity('qq','default','qq-owner','owner')
        for query in ('我喜欢什么','我的喜好','继续说说我的偏好'):
            with self.subTest(query=query):
                hint=self.module().recall(query)['memory_hint']
                self.assertIn('乌龙茶',hint)
                self.assertNotIn('浓咖啡',hint)
        # Normal subject queries still share information, with source attribution.
        self.assertIn('浓咖啡',self.module().recall('浓咖啡')['memory_hint'])

    def test_personal_question_does_not_use_video_dialogue_or_generated_identity(self):
        self.hub.publish([self.event(1,'我的名字是视频演员',role='assistant',channel='video',tier='long'),
                          self.event(2,'我的名字是猜测结果',role='assistant')])
        self.assertEqual(self.module().recall('我的名字')['memory_hint'],'')

    def test_idle_background_retry_recovers_without_another_message(self):
        store=self.store('retry')
        publish=store.agent_memory.publish
        store.agent_memory.publish=Mock(side_effect=OSError('offline'))
        store.add('wechat:private:owner','user','请记住：后台恢复测试','idle',sender_id='owner')
        self.assertEqual(self.hub.stats()['events'],0)
        recovered=threading.Event()
        def restore(events):
            publish(events)
            recovered.set()
        store.agent_memory.publish=restore
        store.start_agent_retry(.02)
        worker=store._agent_retry_thread
        self.assertTrue(recovered.wait(3),'idle sync never retried')
        store.stop_agent_retry()
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.hub.stats()['events'],1)
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM agent_outbox WHERE state='pending'").fetchone()[0],0)
        store.flush_agent_memory()
        self.assertEqual(self.hub.stats()['events'],1)

    def test_public_comment_does_not_inherit_private_details(self):
        self.hub.publish([self.event(1,'请记住：秘密项目口令ORANGE',platform='qq'),
                          self.event(2,'公开项目资料在评论区',platform='bilibili',channel='comment')])
        module=self.module(channel='comment',source_id='bilibili',source_session='bilibili:comment:other')
        self.assertNotIn('ORANGE',module.recall('项目')['memory_hint'])
        self.assertIn('公开项目',module.recall('项目')['memory_hint'])

    def test_outage_retains_durable_outbox_and_replays_exactly_once(self):
        store=self.store('wechat')
        original=store.agent_memory.publish
        store.agent_memory.publish=Mock(side_effect=OSError('unavailable'))
        store.add('wechat:private:owner','user','我喜欢乌龙茶','one',sender_id='owner')
        self.assertTrue(store.has_message('one'))
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM agent_outbox WHERE state='pending'").fetchone()[0],1)
        store.agent_memory.publish=original
        store.flush_agent_memory();store.flush_agent_memory()
        self.assertEqual(self.hub.stats()['events'],1)
        self.assertEqual(self.module().recall('乌龙茶')['items_used'],1)

    def test_hub_commit_before_local_ack_does_not_duplicate(self):
        store=self.store('wechat')
        store.add('wechat:private:owner','user','我喜欢青茶','one',sender_id='owner')
        with store.connection:
            store.connection.execute("UPDATE agent_outbox SET state='pending'")
        store.flush_agent_memory()
        result=self.hub.recall('青茶')
        self.assertEqual(self.hub.stats()['events'],1)
        self.assertEqual(result['memories'][0]['repeats'],1)

    def test_forget_removes_shared_copy_and_blocks_stale_replay_but_allows_new_message(self):
        store=self.store('wechat')
        session='wechat:private:owner'
        store.add(session,'user','请记住：秘密MAGENTA','old',sender_id='owner')
        old=json.loads(store.connection.execute('SELECT payload FROM agent_outbox').fetchone()[0])
        store.clear(session)
        self.assertEqual(self.hub.stats()['events'],0)
        self.hub.publish([old])
        self.assertEqual(self.hub.stats()['events'],0)
        store.add(session,'user','我喜欢绿茶','new',sender_id='owner')
        self.assertIn('绿茶',self.module().recall('绿茶')['memory_hint'])

    def test_reply_job_and_delivery_state_join_the_same_chain(self):
        store=self.store('wechat')
        job=store.start_reply_job('wechat:private:owner','m',dict(text='test'))
        store.finish_reply_job(job['id'],dict(reply='模型生成的蓝鲸方案'))
        store.mark_reply_delivery(job['id'],'sending')
        store.mark_reply_delivery(job['id'],'sent')
        result=self.hub.recall('蓝鲸方案')
        self.assertEqual(result['memories'][0]['sources'][0]['delivery'],'sent')
        self.assertEqual(result['memories'][0]['kind'],'assistant_generated')
        self.assertEqual(result['memories'][0]['sources'][0]['sender_name'],'智能体')

    def test_independent_connections_write_to_one_hub(self):
        def writer(index):
            store=ConversationStore(self.root/f'p{index}.sqlite3',agent_memory_path=self.path)
            try:
                for mid in range(8):
                    store.add(f'qq:private:u{index}','user',f'并发共享记忆 {index} {mid}',str(mid),sender_id=str(index))
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(writer,range(4)))
        self.assertEqual(self.hub.stats()['events'],32)

    def test_memory_hint_is_bounded_and_preserves_negative_statement(self):
        self.hub.publish([self.event(1,'我不喜欢加糖的咖啡。'+('无关信息。'*2000))])
        result=self.module().recall('咖啡加糖偏好')
        self.assertLessEqual(len(result['memory_hint']),1800)
        self.assertIn('不喜欢',result['memory_hint'])

    def test_long_single_statement_excerpt_keeps_relevant_tail_and_negation(self):
        self.hub.publish([self.event(1,'背景资料'*700+'我不喜欢加糖的咖啡'+'其它背景'*700)])
        result=self.module().recall('加糖咖啡偏好')
        self.assertIn('不喜欢加糖的咖啡',result['memory_hint'])
        self.assertLessEqual(len(result['memory_hint']),1800)

    def test_on_demand_tool_returns_refined_hint_not_full_records(self):
        self.hub.publish([self.event(1,'请记住：旧版银河项目使用SQLite')])
        tools=MemoryRecallTools(None,self.module())
        result=tools.execute('memory_recall',dict(query='银河项目',time_range='long_term'))
        self.assertIn('SQLite',result['memory_hint'])
        self.assertNotIn('memories',result)
        self.assertNotIn('recent_chain',result)

    def test_specific_past_question_does_not_attach_unrelated_recent_chat(self):
        self.hub.publish([self.event(1,'请记住：北极项目已经立项'),self.event(2,'今晚聚餐吃披萨',platform='qq')])
        hint=self.module().recall('之前的北极项目情况')['memory_hint']
        self.assertIn('北极项目',hint)
        self.assertNotIn('披萨',hint)

    def test_vague_continuation_can_reconnect_cross_platform_recent_episode(self):
        self.hub.publish([self.event(1,'我们正在讨论南极数据接口',platform='qq')])
        self.assertIn('南极数据',self.module().recall('继续刚才的话题')['memory_hint'])

    def test_service_automatically_uses_shared_hint_from_another_platform(self):
        source=self.store('qq')
        source.add('qq:private:owner','user','请记住：北极项目代号ARCTIC','fact',sender_id='owner')
        client=Mock()
        client.complete.return_value='ARCTIC'
        settings=AppSettings(api_key='unused',deepseek=DeepSeekSettings(),wechat=WeChatSettings(),scope=ScopeSettings(private_users=frozenset({'owner'})),
            runtime=RuntimeSettings(database_path=str(self.root/'wechat-live.sqlite3'),agent_memory_enabled=True,agent_memory_path=str(self.path),cooldown_seconds=0))
        bot=BotService(settings,client)
        try:
            result=bot.handle(dict(platform='wechat',chat_type='private',chat_id='owner',sender_id='owner',text='北极项目代号是什么',message_id='question'))
            self.assertTrue(result['accepted'])
            self.assertIn('ARCTIC',str(client.complete.call_args.args[0]))
            self.assertIn('本轮共享记忆提示',str(client.complete.call_args.args[0]))
        finally:
            bot.close()

    def test_enable_only_imports_active_generation_and_preserves_original(self):
        project=self.root/'project';project.mkdir()
        generation=self.root/'generation';generation.mkdir()
        source=generation/'wechat-default.sqlite3'
        old=ConversationStore(source)
        old.add('wechat:private:owner','user','请记住：当前代链路代号CURRENT','m',sender_id='owner');old.close()
        config=project/'config.json'
        config.write_text(json.dumps(dict(runtime=dict(database_path=str(source)))),encoding='utf-8')
        report=enable(config)
        shared=AgentMemory(report['database'])
        try:
            self.assertEqual(shared.stats()['events'],1)
            self.assertTrue(Path(report['backup'],'config.json').is_file())
            self.assertTrue(json.loads(config.read_text())['runtime']['agent_memory_enabled'])
            verified=ConversationStore(source)
            try:
                self.assertEqual(verified.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0],1)
            finally:
                verified.close()
        finally:
            shared.close()

    def test_service_exposes_memory_tool_and_preserves_platform_preferred_tool(self):
        self.hub.publish([self.event(1,'请记住：星河计划使用 SQLite')])
        class Client:
            def complete_with_tools(client,messages,definitions,execute,max_rounds,tool_choice='auto'):
                client.names={item['function']['name'] for item in definitions}
                client.choice=tool_choice
                client.result=execute('memory_recall',{'query':'星河计划','time_range':'long_term'})
                return SimpleNamespace(content='SQLite',tool_calls=1,message_sent=False)
        client=Client()
        settings=AppSettings(api_key='unused',deepseek=DeepSeekSettings(),wechat=WeChatSettings(),scope=ScopeSettings(private_users=frozenset({'owner'})),
            runtime=RuntimeSettings(database_path=str(self.root/'tool-service.sqlite3'),agent_memory_enabled=True,agent_memory_path=str(self.path),tool_calling_enabled=True,cooldown_seconds=0))
        bot=BotService(settings,client)
        delegate=SimpleNamespace(definitions=[dict(type='function',function=dict(name='weather_forecast'))],preferred_tool='weather_forecast',execute=lambda *_:{'success':True})
        try:
            result=bot.handle(dict(platform='qq',chat_type='private',chat_id='owner',sender_id='owner',text='星河计划的存储方案',message_id='tool-q'),tool_executor=delegate)
            self.assertTrue(result['accepted'])
            self.assertIn('memory_recall',client.names)
            self.assertEqual(client.choice['function']['name'],'weather_forecast')
            self.assertIn('SQLite',client.result['memory_hint'])
        finally:
            bot.close()

    def test_current_question_is_not_recalled_as_evidence_for_itself(self):
        class Client:
            def complete_with_tools(client,messages,definitions,execute,max_rounds,tool_choice='auto'):
                client.result=execute('memory_recall',{'query':'香草咖啡','time_range':'auto'})
                return SimpleNamespace(content='还没有已确认的偏好记录',tool_calls=1,message_sent=False)
        client=Client()
        settings=AppSettings(api_key='unused',deepseek=DeepSeekSettings(),wechat=WeChatSettings(),scope=ScopeSettings(private_users=frozenset({'owner'})),
            runtime=RuntimeSettings(database_path=str(self.root/'self-query.sqlite3'),agent_memory_enabled=True,agent_memory_path=str(self.path),tool_calling_enabled=True,cooldown_seconds=0))
        bot=BotService(settings,client)
        try:
            result=bot.handle(dict(platform='wechat',chat_type='private',chat_id='owner',sender_id='owner',text='我喜欢香草咖啡吗？',message_id='question'))
            self.assertTrue(result['accepted'])
            self.assertEqual(client.result['memory_hint'],'')
            later=self.module().recall('之前问的香草咖啡')
            self.assertIn('历史提问',later['memory_hint'])
        finally:
            bot.close()


if __name__=='__main__':
    unittest.main()

"""Offline integration: real service/HTTP/SQLite/bridges, scripted model responses.

No production configuration or credentials are loaded. All HTTP is loopback-only;
social send methods and external video/browser observations are local fixtures.
"""
from __future__ import annotations

import collections
import copy
import json
import re
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from social_bot.modules.link import LinkOutcome
from social_bot.modules.video import OmniVideoAnalyzer, VideoModule
from social_bot.platforms.qq.worker import QQWorker
from social_bot.platforms.wechat.modules.text_reply.module import TextReplyModule
from social_bot.platforms.wechat.tools import WeChatToolExecutor
from social_bot.scheduler import RequestPool
from social_bot.service import BotService
from social_bot.settings import AppSettings, DeepSeekSettings, RuntimeSettings, ScopeSettings, WeChatSettings
from social_bot.video_browser import VideoAgentTools

ROOT = Path(__file__).resolve().parent.parent
PIPELINE_TRACES = []


def completion(text):
    return {'choices':[{'message':{'role':'assistant','content':text},'finish_reason':'stop'}]}


def tool_call(name, arguments=None):
    return {'choices':[{'message':dict(role='assistant',content=None,
        reasoning_content='PRIVATE_FIXTURE_REASONING',tool_calls=[dict(id='fixture-call',type='function',
        function=dict(name=name,arguments=json.dumps(arguments or {})))]),'finish_reason':'tool_calls'}]}


class LocalGateway:
    def __init__(self):
        self.steps = collections.deque()
        self.requests, self.errors = [], []
        self.lock = threading.Lock()
        self.worker = None
        self.route_model = None
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                status = 200
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length < 2_000_000:
                        raise AssertionError('unexpected request size')
                    payload = json.loads(self.rfile.read(length))
                    if self.path == '/worker' and owner.worker is not None:
                        result = owner.worker.handle(payload)
                    elif self.path == '/chat/completions':
                        with owner.lock:
                            owner.requests.append(copy.deepcopy(payload))
                            if owner.route_model is None:
                                expected, status, result = owner.steps.popleft()
                                if payload['model'] != expected:
                                    raise AssertionError(f'model routing mismatch: {payload["model"]} != {expected}')
                        if owner.route_model is not None:
                            result = owner.route_model(payload)
                    else:
                        raise AssertionError('unexpected local endpoint')
                except Exception as exc:
                    with owner.lock:
                        owner.errors.append(type(exc).__name__+': '+str(exc))
                    status, result = 500, {'error':{'message':'offline fixture failure'}}
                body = json.dumps(result,ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=lambda:self.server.serve_forever(poll_interval=.02),daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def script(self, *steps):
        self.steps.extend((model,200,data) for model,data in steps)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class PipelineLogicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='deepseek-offline-pipeline-')
        self.root = Path(self.temp.name)
        self.gateway = LocalGateway()
        self.pool = RequestPool(self.root/'scheduler.sqlite3',concurrency=2,interval=0,wait_seconds=10)
        self.opener = build_opener(ProxyHandler({}))
        def local_only(request, timeout):
            parsed = urlsplit(request.full_url)
            if parsed.hostname != '127.0.0.1' or parsed.port != self.gateway.server.server_port:
                raise AssertionError('offline test blocked non-fixture network request')
            return self.opener.open(request,timeout=timeout)
        self.patches = [patch('social_bot.net._direct',side_effect=local_only),
                        patch('social_bot.scheduler.configured_pool',return_value=self.pool)]
        for item in self.patches:
            item.start()
        self.service = None

    def tearDown(self):
        try:
            if self.service:
                self.service.close()
            with self.pool.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM waiting').fetchone()[0],0)
            self.assertEqual(self.gateway.errors,[])
            self.assertEqual(len(self.gateway.steps),0)
        finally:
            PIPELINE_TRACES.append(dict(case=self._testMethodName,
                requests=[dict(model=r['model'],has_tools=bool(r.get('tools')),
                               tool_messages=sum(m['role']=='tool' for m in r['messages']))
                          for r in self.gateway.requests], fixture_errors=list(self.gateway.errors)))
            for item in reversed(self.patches):
                item.stop()
            self.gateway.close()
            self.temp.cleanup()

    def settings(self, users=('u1',), tools=True):
        return AppSettings(api_key='offline-fixture-key',
            deepseek=DeepSeekSettings(base_url=self.gateway.url+'/chat/completions',
                collaboration_enabled=True,model='deepseek-chat',task_model='deepseek-flash',
                timeout_seconds=5,task_timeout_seconds=5),
            scope=ScopeSettings(private_users=frozenset(users)),
            runtime=RuntimeSettings(database_path=str(self.root/'chat.sqlite3'),cooldown_seconds=0,
                                    tool_calling_enabled=tools),wechat=WeChatSettings())

    def start_service(self, **kwargs):
        if self.service:
            self.service.close()
        self.service=BotService(self.settings(**kwargs))
        return self.service

    def incoming(self, **kwargs):
        return dict(platform='wechat',chat_type='private',chat_id='u1',sender_id='u1',
                    text='离线链路测试',message_id='fixture-message') | kwargs

    def models(self):
        return [r['model'] for r in self.gateway.requests]

    def handoff(self):
        request=self.gateway.requests[-1]
        self.assertEqual(request['model'],'deepseek-chat')
        self.assertNotIn('tools',request)
        self.assertNotIn('PRIVATE_FIXTURE_REASONING',json.dumps(request))
        self.assertTrue(all('tool_calls' not in m and m['role']!='tool' for m in request['messages']))
        return json.loads(request['messages'][-1]['content'].split('\n',1)[1])

    def text_module(self, incoming, executor=None):
        sent=collections.deque(maxlen=10)
        module=TextReplyModule(self.service,'fixture-bot',lambda *a,**k:incoming,None,sent,
            tool_executor_factory=(lambda *a:executor) if executor else None)
        chat=SimpleNamespace(SendMsg=Mock(return_value={'status':'成功'}))
        return module,chat,sent

    def test_wechat_tool_to_flash_to_chat_and_single_send(self):
        self.start_service()
        url='https://example.test/article'
        raw=SimpleNamespace(type='text',content='读取链接 '+url)
        link=Mock()
        link.read.return_value=LinkOutcome(True,url=url,title='fixture article',text='来源中的测试值为7429')
        executor=WeChatToolExecutor(Mock(),raw,'fixture-chat',allow_link=True,link_module=link)
        self.gateway.script(('deepseek-flash',tool_call('link_read',{'url':url})),
            ('deepseek-flash',completion('资料整理：来源中的测试值为7429')),
            ('deepseek-chat',completion('测试值是7429。')))
        module,chat,sent=self.text_module(self.incoming(text=raw.content),executor)
        outcome=module.handle(raw,chat)
        self.assertTrue(outcome.sent)
        chat.SendMsg.assert_called_once_with('测试值是7429。')
        self.assertEqual(list(sent),['测试值是7429。'])
        link.read.assert_called_once_with(url)
        self.assertEqual(self.models(),['deepseek-flash','deepseek-flash','deepseek-chat'])
        package=self.handoff()
        self.assertTrue(package['tool_results'][0]['success'])
        self.assertIn('7429',package['tool_results'][0]['data'])
        history=self.service.store.history('wechat:private:u1',10)
        self.assertEqual([m['role'] for m in history],['user','assistant'])
        self.assertEqual(history[-1]['content'],'测试值是7429。')
        self.assertNotIn('资料整理',json.dumps(history,ensure_ascii=False))

    def test_real_mp4_probe_fixture_perception_flash_summary_chat_reply(self):
        self.start_service()
        path=self.root/'blue.mp4'
        subprocess.run([OmniVideoAnalyzer._ffmpeg(),'-nostdin','-v','error','-f','lavfi',
            '-i','color=c=blue:s=64x64:r=2','-t','3','-c:v','libx264',str(path)],
            check=True,capture_output=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        original=path.read_bytes()
        analyzer=OmniVideoAnalyzer('fixture-key','https://fixture.invalid',
            summarizer=self.service.client.complete_task,summary_model='deepseek-flash')
        analyzer._request=Mock(return_value='固定样本：蓝色静止画面，没有音轨。')
        raw=SimpleNamespace(type='video',content='分析这个测试视频',file_path=str(path))
        executor=WeChatToolExecutor(Mock(),raw,'fixture-chat',allow_video=True,video_module=VideoModule(analyzer))
        self.gateway.script(('deepseek-flash',tool_call('wechat_read_video')),
            ('deepseek-flash',completion('整理视觉证据：0到3秒为蓝色画面，无音轨。')),
            ('deepseek-flash',completion('工具成功，覆盖0到3秒。')),
            ('deepseek-chat',completion('测试片段是蓝色静止画面，没有音轨。')))
        module,chat,_=self.text_module(self.incoming(text=raw.content,message_type='video'),executor)
        outcome=module.handle(raw,chat)
        self.assertTrue(outcome.sent)
        analyzer._request.assert_called_once()
        self.assertEqual(self.models(),['deepseek-flash']*3+['deepseek-chat'])
        evidence=json.loads(self.handoff()['tool_results'][0]['data'])
        self.assertEqual(evidence['summary_model'],'deepseek-flash')
        self.assertEqual(evidence['coverage_seconds'],3)
        self.assertFalse(evidence['has_audio'])
        self.assertEqual(path.read_bytes(),original)
        chat.SendMsg.assert_called_once_with(outcome.reply)

    def test_chat_failure_keeps_input_and_restart_does_not_repeat_action(self):
        self.start_service()
        executed=Mock(return_value=dict(success=True,side_effect_performed=True,message_sent=True))
        executor=SimpleNamespace(definitions=[dict(type='function',function=dict(name='local_dry_action'))],execute=executed)
        self.gateway.script(('deepseek-flash',tool_call('local_dry_action')),
                            ('deepseek-flash',completion('本地模拟动作已经执行一次。')))
        self.gateway.steps.append(('deepseek-chat',500,{'error':{'message':'injected chat failure'}}))
        with self.assertLogs('social_bot.service',level='ERROR'):
            result=self.service.handle(self.incoming(),tool_executor=executor)
        self.assertEqual(result['reason'],'model_error')
        self.assertEqual([m['role'] for m in self.service.store.history('wechat:private:u1',10)],['user'])
        executed.assert_called_once()
        count=len(self.gateway.requests)
        self.start_service()
        self.gateway.script(('deepseek-chat',completion('最终回复恢复成功。')))
        resumed=self.service.handle(self.incoming(),tool_executor=executor)
        self.assertTrue(resumed['resumed_final_only'])
        executed.assert_called_once()
        self.assertEqual(len(self.gateway.requests),count+1)
        self.assertEqual(self.gateway.requests[-1]['model'],'deepseek-chat')

    def test_flash_failure_stops_before_tool_and_final_reply(self):
        self.start_service()
        self.gateway.steps.append(('deepseek-flash',500,{'error':{'message':'injected task failure'}}))
        executor=SimpleNamespace(definitions=[dict(type='function',function=dict(name='local_read'))],execute=Mock())
        with self.assertLogs('social_bot.service',level='ERROR'):
            result=self.service.handle(self.incoming(),tool_executor=executor)
        self.assertEqual(result['reason'],'model_error')
        executor.execute.assert_not_called()
        self.assertEqual(self.models(),['deepseek-flash'])
        self.assertTrue(self.service.store.has_message('fixture-message','wechat:private:u1'))
        self.assertEqual(len(self.service.store.history('wechat:private:u1',10)),1)

    def test_tool_failure_receipt_reaches_chat(self):
        self.start_service()
        executor=SimpleNamespace(definitions=[dict(type='function',function=dict(name='local_read'))],
            execute=Mock(return_value=dict(success=False,reason='fixture_source_unavailable')))
        self.gateway.script(('deepseek-flash',tool_call('local_read')),
            ('deepseek-flash',completion('工具未取得资料。')),('deepseek-chat',completion('资料读取失败，请稍后重试。')))
        result=self.service.handle(self.incoming(),tool_executor=executor)
        self.assertTrue(result['accepted'])
        receipt=self.handoff()['tool_results'][0]
        self.assertFalse(receipt['success'])
        self.assertIn('fixture_source_unavailable',receipt['data'])
        executor.execute.assert_called_once()

    def test_pending_intake_replayed_then_recalled_beyond_recent_window(self):
        self.start_service(tools=False)
        self.service.store.enqueue('wechat:private:u1','user','请记住：离线测试代号是R-01','pending',sender_id='u1')
        self.start_service(tools=False)
        self.assertTrue(self.service.store.has_message('pending','wechat:private:u1'))
        for i in range(15):
            self.service.store.add('wechat:private:u1','user',f'无关测试消息{i}',f'filler-{i}')
        self.gateway.script(('deepseek-flash',completion('记忆资料中代号为R-01。')),
                            ('deepseek-chat',completion('你的测试代号是R-01。')))
        result=self.service.handle(self.incoming(text='测试代号是什么？',message_id='ask-after-restart'))
        self.assertTrue(result['accepted'])
        self.assertIn('R-01',json.dumps(self.gateway.requests[0]['messages'],ensure_ascii=False))
        self.assertEqual(self.models(),['deepseek-flash','deepseek-chat'])
        self.assertEqual(self.service.store.memory_stats()['inbox'].get('pending',0),0)
        self.assertEqual(self.service.store.connection.execute("SELECT COUNT(*) FROM messages WHERE message_id='pending'").fetchone()[0],1)

    def test_parallel_sessions_keep_handoffs_separate_and_respect_scheduler(self):
        users=tuple(f'u{i}' for i in range(6))
        self.start_service(users=users,tools=False)
        active,peak=0,0
        lock=threading.Lock()
        first_pair=threading.Event()
        def model(payload):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active)
                if active==2:first_pair.set()
            try:
                self.assertTrue(first_pair.wait(timeout=3),'scheduler did not admit two independent sessions')
                time.sleep(.02)
                text=json.dumps(payload['messages'],ensure_ascii=False)
                tags=set(re.findall(r'case:thread-\d+',text))
                self.assertEqual(len(tags),1)
                tag=next(iter(tags))
                if payload['model']=='deepseek-flash':return completion('brief '+tag)
                package=json.loads(payload['messages'][-1]['content'].split('\n',1)[1])
                self.assertEqual(package['brief'],'brief '+tag)
                return completion('reply '+tag)
            finally:
                with lock:active-=1
        self.gateway.route_model=model
        def handle(index):
            return self.service.handle(self.incoming(chat_id=users[index],sender_id=users[index],
                text=f'case:thread-{index}',message_id=f'parallel-{index}'))
        with ThreadPoolExecutor(max_workers=6) as executor:
            results=list(executor.map(handle,range(6)))
        self.assertEqual([r.get('reply') for r in results],[f'reply case:thread-{i}' for i in range(6)])
        self.assertEqual(peak,2)
        self.assertEqual(self.service.store.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0],12)

    def test_qq_node_bridge_python_worker_models_and_delivery_receipt(self):
        self.start_service(tools=False)
        self.gateway.worker=QQWorker(self.service,'123',media_reader=SimpleNamespace(
            image_enabled=False,voice_enabled=False,video_enabled=False))
        self.gateway.script(('deepseek-flash',completion('离线QQ资料整理完毕。')),
                            ('deepseek-chat',completion('QQ离线链路测试完成。')))
        source='''
import {deliver} from './qq/bridge.mjs';
const trace=[];
const request=async value=>{trace.push(value.op);const response=await fetch(BASE+'/worker',{
method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(value)});return await response.json();};
const sent=[];
const bot={sendText:async(target,text)=>{trace.push('fake_send');sent.push({target,text});}};
const msg={kind:'c2c',senderId:'u1',messageId:'qq-offline-1',content:'QQ离线链路测试',
replyTarget:{scope:'c2c',targetId:'u1',msgId:'qq-offline-1'}};
const first=await deliver(bot,request,msg,{});
const duplicate=await deliver(bot,request,msg,{});
console.log(JSON.stringify({first,duplicate,sent,trace}));
'''.replace('BASE',json.dumps(self.gateway.url))
        process=subprocess.run(['node','--input-type=module','-e',source],cwd=ROOT,
            capture_output=True,encoding='utf-8',timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(process.returncode,0,process.stderr)
        result=json.loads(process.stdout)
        self.assertTrue(result['first']['accepted'])
        self.assertFalse(result['duplicate']['accepted'])
        self.assertEqual(len(result['sent']),1)
        self.assertEqual(result['sent'][0]['text'],'QQ离线链路测试完成。')
        self.assertEqual(result['trace'],['prepare','sending','fake_send','sent','prepare'])
        self.assertEqual(self.models(),['deepseek-flash','deepseek-chat'])
        self.assertEqual(self.service.store.connection.execute('SELECT state FROM qq_deliveries').fetchone()[0],'sent')

    def test_video_agent_tools_analysis_cache_and_final_chat(self):
        self.start_service()
        url='https://www.douyin.com/video/123456789'
        browser=SimpleNamespace(platform='douyin',max_videos=1,current=None,
            page=SimpleNamespace(context=SimpleNamespace(cookies=lambda:[])))
        browser.observe=lambda:dict(candidates=[dict(url=url,title='fixture video')],current=browser.current)
        def next_video(value=''):
            browser.current=url
            return browser.observe()
        browser.next=next_video
        analyzer=Mock()
        analyzer.analyze.return_value=dict(success=True,url=url,text='固定样本：三秒蓝色画面',
            evidence='audio_visual',coverage_start_seconds=0,coverage_end_seconds=3,partial=False)
        tools=VideoAgentTools(browser,analyzer,self.service.store,'offline-video')
        self.gateway.script(('deepseek-flash',tool_call('video_list')),
            ('deepseek-flash',tool_call('video_next')),('deepseek-flash',tool_call('video_analyze')),
            ('deepseek-flash',completion('有来源资料显示三秒蓝色画面。')),
            ('deepseek-chat',completion('测试视频是三秒蓝色画面。')))
        names=frozenset({'video_list','video_next','video_analyze','video_memory'})
        result=self.service.client.complete_with_tools([dict(role='user',content='刷一条测试视频并总结')],
            tools.definitions,tools.execute,max_rounds=6,continuable_tools=names)
        self.assertEqual(result.content,'测试视频是三秒蓝色画面。')
        self.assertEqual(result.tool_calls,3)
        analyzer.analyze.assert_called_once()
        self.assertEqual(self.models(),['deepseek-flash']*4+['deepseek-chat'])
        self.assertEqual(self.service.store.connection.execute('SELECT state FROM video_observations').fetchone()[0],'done')
        self.assertTrue(tools.execute('video_analyze',{})['success'])
        analyzer.analyze.assert_called_once()
        self.assertEqual(self.service.store.memory_stats()['tiers'],{})
        self.handoff()

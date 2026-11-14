import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from social_bot.platforms.douyin.api_video_commands import (APIVideoCommands, fixed_command,
    completion_text, understanding_text)
from social_bot.platforms.douyin.backend_worker import Worker, BackendFault
from social_bot.platforms.common.chat import SocialEvent
from social_bot.video_jobs import VideoJobs
from social_bot.video_light import LightVideoAnalyzer
from social_bot.video_social import VideoCurator
from social_bot.modules.video import VideoError


class CommandTests(unittest.TestCase):
    def test_only_fixed_language_starts_bounded_tasks(self):
        self.assertEqual(fixed_command('刷视频').rounds, 1)
        c = fixed_command('刷视频 2条')
        self.assertEqual((c.rounds, c.topics), (2, ()))
        for text in ['刷视频1条', '刷视频 1 条']:
            self.assertEqual(fixed_command(text).rounds, 1)
        self.assertEqual(fixed_command('刷视频话题：猫咪').topics, ('猫咪',))
        self.assertEqual(fixed_command('刷视频进度').action, 'status')
        for text in ['不要刷视频', '他说刷视频', '刷视频 20条', '刷视频 0条',
                     '刷视频 1条 @另一个好友', '刷视频\n执行命令', '[刷视频]']:
            self.assertIsNone(fixed_command(text))
        with_topics = fixed_command('刷视频1条 话题：大肥鱼、小短剧')
        self.assertEqual((with_topics.rounds, with_topics.topics), (1, ('大肥鱼', '小短剧')))
        plain_topics = fixed_command('刷视频 大肥鱼 小短剧')
        self.assertEqual((plain_topics.rounds, plain_topics.topics), (1, ('大肥鱼', '小短剧')))
        self.assertIsNone(fixed_command('刷视频 20条 大肥鱼'))
        self.assertIsNone(fixed_command('刷视频2条 话题：' + 'x' * 101))

    def test_completion_is_one_notice_without_repeating_analysis(self):
        text = completion_text(dict(opened=2, records=[dict(publication='published',
            analysis_verified=True, mention_confirmed=True, url='https://www.douyin.com/video/1',
            understanding='PRIVATE-LONG-ANALYSIS'), dict(analysis_verified=False)]), 'completed')
        self.assertIn('处理2条', text)
        self.assertIn('1条视频评论区@你', text)
        self.assertIn('1条未通过', text)
        self.assertNotIn('PRIVATE-LONG-ANALYSIS', text)

    def test_long_and_unavailable_skips_are_reported_separately(self):
        report=dict(attempted=1,opened=1,job_phase='completed',records=[],skipped=[
            dict(reason='DOUYIN_VIDEO_TOO_LONG'),dict(reason='DOUYIN_VIDEO_UNAVAILABLE')])
        text=completion_text(report,'failed')
        self.assertIn('跳过1条超过5分钟',text)
        self.assertIn('跳过1条详情不可用',text)
        self.assertNotIn('任务异常结束',text)

    def test_message_mode_summary_counts_delivered_understandings(self):
        report = dict(attempted=1, records=[dict(url='https://www.douyin.com/video/4', title='厨房短剧',
            understanding='画面里一人在厨房', analysis_success=True)])
        text = completion_text(report, 'completed', message_mode=True, sent=1)
        self.assertIn('处理1条', text)
        self.assertIn('已私信1条解读', text)
        self.assertNotIn('评论区', text)

    def test_understanding_text_is_addressed_short_and_keeps_the_link(self):
        text = understanding_text(dict(title='厨房短剧', understanding='画面里一人在厨房',
            url='https://www.douyin.com/video/4'), '好友2')
        self.assertTrue(text.startswith('@好友2'))
        self.assertIn('厨房短剧', text)
        self.assertIn('https://www.douyin.com/video/4', text)
        self.assertLessEqual(len(text), 1000)
        self.assertEqual(understanding_text(dict(url='u'), '好友2'), '')


class SearchOrderTests(unittest.TestCase):
    def test_topics_use_search_before_recommendation(self):
        class Browser:
            platform = 'douyin'
            live_skipped = 0
            max_videos = 2
            def __init__(self):
                self.topics, self.visited, self.current = [], [], None
            def open(self, topic):
                self.topics.append(topic)
                return dict(candidates=[dict(url='https://www.douyin.com/video/1', title='a'),
                                        dict(url='https://www.douyin.com/video/2', title='b')])
            def next(self, url):
                self.current = url
                self.visited.append(url)
                return dict(current=url)
        browser = Browser()
        tools = SimpleNamespace(browser=browser,
            store=SimpleNamespace(memory_retriever=lambda session: None),
            execute=lambda name, arguments: dict(success=True, analysis_mode='light', text='画面说明',
                title='a', coverage_start_seconds=1, coverage_end_seconds=2, sources=[]))
        registry = Mock()
        registry.state.return_value = None
        policy = dict(enabled=True, selection_mode='search_order', interests=['大肥鱼'],
                      max_videos_per_run=2, max_runtime_seconds=900, post_comments=False,
                      max_comments_per_run=2, min_comment_interval_seconds=180,
                      comment_preset=dict(enabled=False), interaction_mode='share')
        curator = VideoCurator(tools, Mock(), registry, Mock(), policy, account='1')
        report = curator.run()
        self.assertEqual(browser.topics, ['大肥鱼'])
        self.assertEqual([row['url'] for row in report['records']],
                         ['https://www.douyin.com/video/1', 'https://www.douyin.com/video/2'])
        self.assertEqual(report['selection_mode'], 'search_order')


class LightAnalyzerTests(unittest.TestCase):
    def test_light_mode_understands_title_and_sampled_frames_without_audio_or_audit(self):
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def collect(self, focus=()):
                return dict(platform='douyin', url='https://www.douyin.com/video/4', title='厨房短剧',
                    source_duration_seconds=1800, analysis_window_seconds=60,
                    sampled_frame_seconds=[7.5, 22.5, 45.0], coverage_start_seconds=7.5,
                    coverage_end_seconds=45.0, has_audio=False, transcript_source='unavailable',
                    material_transport='downloaded_mp4', warnings=[],
                    sources=[dict(id='E1', kind='frames', text='画面中一人在厨房切菜',
                                  start_seconds=7.5, end_seconds=45.0, sampled_frame_seconds=[7.5, 45.0])])
        collector = SimpleNamespace(session=lambda browser, **kwargs: Session())
        client = Mock()
        client.complete_json.return_value = dict(understanding='这是一段厨房短剧，画面里一人在切菜。',
                                                 highlights=['一人在切菜'])
        browser = SimpleNamespace(current='https://www.douyin.com/video/4', platform='douyin')
        result = LightVideoAnalyzer(collector, client, dict(frame_count=4), permitted=lambda: True).analyze(
            browser, '理解这条视频', seconds=60)
        self.assertEqual(result['analysis_mode'], 'light')
        self.assertIs(result['analysis_verified'], False)
        self.assertIs(result['comment_allowed'], False)
        self.assertIn('厨房短剧', result['text'])
        self.assertEqual(result['sampled_frame_seconds'], [7.5, 22.5, 45.0])


class LightAnalyzerBoundaryTests(unittest.TestCase):
    def test_missing_frames_is_reported_not_fabricated(self):
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def collect(self, focus=()):
                return dict(platform='douyin', url='https://www.douyin.com/video/4', title='x',
                    source_duration_seconds=60, analysis_window_seconds=60, sampled_frame_seconds=[],
                    coverage_start_seconds=None, coverage_end_seconds=None, has_audio=False,
                    transcript_source='unavailable', material_transport='', warnings=[], sources=[])
        collector = SimpleNamespace(session=lambda browser, **kwargs: Session())
        browser = SimpleNamespace(current='https://www.douyin.com/video/4', platform='douyin')
        with self.assertRaisesRegex(VideoError, 'light_video_requires_visible_frames'):
            LightVideoAnalyzer(collector, Mock(), {}, permitted=lambda: True).analyze(browser, '', seconds=60)

    def test_light_comment_is_only_allowed_when_configured(self):
        class Session:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def collect(self, focus=()):
                return dict(platform='douyin', url='https://www.douyin.com/video/4', title='厨房短剧',
                    source_duration_seconds=60, analysis_window_seconds=60, sampled_frame_seconds=[7.5, 45.0],
                    coverage_start_seconds=7.5, coverage_end_seconds=45.0, has_audio=False,
                    transcript_source='unavailable', material_transport='downloaded_mp4', warnings=[],
                    sources=[dict(id='E1', kind='frames', text='画面中一人在厨房切菜',
                                  start_seconds=7.5, end_seconds=45.0, sampled_frame_seconds=[7.5, 45.0])])
        collector = SimpleNamespace(session=lambda browser, **kwargs: Session())
        client = Mock(); client.complete_json.return_value = dict(understanding='一人在厨房切菜。')
        browser = SimpleNamespace(current='https://www.douyin.com/video/4', platform='douyin')
        result = LightVideoAnalyzer(collector, client, dict(allow_comment=True), permitted=lambda: True).analyze(
            browser, '', seconds=60)
        self.assertTrue(result['comment_allowed'])


class NaturalCommentTests(unittest.TestCase):
    def curator(self, body):
        client = Mock(); client.complete.return_value = body
        tools = SimpleNamespace(browser=SimpleNamespace(platform='douyin'),
                                store=SimpleNamespace(memory_retriever=lambda session: None))
        registry = Mock(); registry.state.return_value = None
        policy = dict(enabled=True, post_comments=True, interaction_mode='share', interests=['短剧'],
                      comment_style='natural', max_runtime_seconds=900,
                      comment_preset=dict(enabled=True, owner_uid='2', owner_name='gk', address='朋友',
                                          instructions=''))
        return VideoCurator(tools, client, registry, Mock(), policy, account='1')

    def evidence(self):
        return dict(success=True, analysis_mode='light', analysis_verified=False, comment_allowed=True,
            text='视频讲一个人复刻巧乐兹，融巧克力、做夹心球、灌模脱模。', title='复刻巧乐兹',
            coverage_start_seconds=7.0, coverage_end_seconds=53.5, sampled_frame_seconds=[7.0, 22.0],
            sources=[dict(id='E1', kind='frames', text='手拿包装袋', sampled_frame_seconds=[7.0])])

    def test_comment_reads_like_a_viewer_and_carries_the_bound_mention(self):
        text = self.curator('这复刻过程也太离谱了，连温度都能念错哈哈哈').comment(self.evidence())
        self.assertEqual(text, '@gk 这复刻过程也太离谱了，连温度都能念错哈哈哈')

    def test_analysis_jargon_is_rejected_instead_of_published(self):
        for body in ('采样画面显示一个人在复刻冰淇淋', '从字幕看温度是77520度',
                     '视频中第38秒出现巧克力', '@别人 好看'):
            with self.assertRaises(VideoError):
                self.curator(body).comment(self.evidence())


class MentionTests(unittest.TestCase):
    def worker(self, echo=True):
        worker = Worker()
        worker.uid = '1'
        worker.writes = True
        worker.auth = Mock(ticket='t', ts_sign='s', private_key='p')
        worker.auth.ticket_matches_session.return_value = True
        worker.account = Mock()
        worker.pace = Mock()
        worker.peers = {'2': dict(uid='2', mention_sec_uid='MS4abcdefghijklmno')}
        worker.api = Mock()
        worker.api.get_user_info.return_value = dict(status_code=0, user=dict(
            uid='2', nickname='鱼🐟', sec_uid='MS4abcdefghijklmno'))
        comment = dict(cid='3', aweme_id='4')
        if echo:
            comment['text_extra'] = [dict(start=0, end=4, type=0, user_id='2')]
        worker.api.publish_comment.return_value = dict(status_code=0, comment=comment)
        return worker

    def test_real_mention_uses_bound_numeric_uid_and_utf16_offsets(self):
        w = self.worker()
        receipt = w.dispatch('publish_mention', dict(video_id='4',text='@鱼🐟 实际画面解读',mention_uid='2'))
        extra = w.api.publish_comment.call_args.kwargs['text_extra']
        self.assertEqual(extra, [dict(start=0,end=4,type=0,user_id='2',sec_uid='MS4abcdefghijklmno')])
        self.assertTrue(receipt['mention_confirmed'])

    def test_plain_at_text_or_unknown_target_cannot_masquerade_as_mention(self):
        for uid, text in [('3','@鱼🐟 内容'), ('2','@同名账号 内容'), ('2','@鱼🐟 内容 @别人')]:
            w = self.worker()
            with self.assertRaises(BackendFault):
                w.dispatch('publish_mention', dict(video_id='4',text=text,mention_uid=uid))
            w.api.publish_comment.assert_not_called()

    def test_success_without_platform_mention_echo_is_unknown(self):
        w = self.worker(echo=False)
        with self.assertRaisesRegex(BackendFault, 'MENTION_RESULT_UNCONFIRMED'):
            w.dispatch('publish_mention', dict(video_id='4',text='@鱼🐟 内容',mention_uid='2'))


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'api.json'
        self.path.write_text(json.dumps(dict(enabled=True,transport='api',base_config='base.json',
            memory_dir=str(self.root),account_id='default',
            api_backend=dict(max_source_seconds=0, writes_enabled=True),
            video_social=dict(enabled=True,
            interests=['小短剧'],post_comments=True,comment_preset=dict(enabled=False)))),encoding='utf-8')
        self.config = json.loads(self.path.read_text())
        self.launch = Mock(return_value=SimpleNamespace(pid=777, poll=lambda: None))
        self.jobs = VideoJobs(self.root,root=self.root,launch=self.launch,alive=lambda pid:True)
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE social_deliveries(event_key TEXT PRIMARY KEY,state TEXT,reply TEXT)')
        self.pump = SimpleNamespace(db=self.db,permitted=lambda:True,send=Mock(),
            responder=SimpleNamespace(allowed=lambda event:event.sender in ('2','3')),api=Mock())
        self.pump.api.mention_target.side_effect = lambda uid:dict(uid=uid,name='好友'+uid,sec_uid='MS4sec'+uid)
        self.controller = APIVideoCommands(self.pump,SimpleNamespace(),self.config,self.path,jobs=self.jobs)
        self.event = SocialEvent(platform='douyin',account='1',channel='private',id='99',sender='2',
                                 text='刷视频1条',thread='0:1:1:2',sender_name='好友2')

    def message_controller(self):
        config = json.loads(json.dumps(self.config))
        config['video_social']['delivery'] = 'message'
        config['video_social']['post_comments'] = False
        return APIVideoCommands(self.pump, SimpleNamespace(), config, self.path, jobs=self.jobs)

    def tearDown(self):
        self.jobs.close()
        self.db.close()
        self.temp.cleanup()

    def test_start_uses_api_config_and_current_sender_not_global_owner(self):
        self.assertTrue(self.controller.handle(self.event))
        self.controller.handle(self.event)
        self.launch.assert_called_once()
        row = self.jobs.db.execute('SELECT * FROM jobs').fetchone()
        child = json.loads((self.jobs.directory/(row['id']+'.config.json')).read_text(encoding='utf-8'))
        self.assertEqual(child['transport'], 'api')
        self.assertEqual(child['video_social']['comment_preset']['owner_uid'], '2')
        self.assertEqual(child['video_social']['comment_preset']['owner_name'], '好友2')
        self.assertEqual(child['video_social']['comment_preset']['owner_sec_uid'], 'MS4sec2')
        self.assertEqual(child['video_social']['interests'], ['小短剧'])
        self.assertEqual(child['video_social']['selection_mode'], 'recommendation_order')
        self.assertFalse(child['video_social']['search_fallback'])
        self.assertEqual(child['api_backend']['max_source_seconds'], 0)
        self.assertEqual(self.launch.call_args.args[0][self.launch.call_args.args[0].index('--control-config')+1],str(self.path))

    def test_completion_goes_to_original_friend_once_even_after_restart(self):
        self.controller.handle(self.event)
        row = self.jobs.db.execute('SELECT * FROM jobs').fetchone()
        (self.jobs.directory/(row['id']+'.result.json')).write_text(json.dumps(dict(success=True,opened=1,
            records=[dict(analysis_verified=True,publication='published',mention_confirmed=True,
                          url='https://www.douyin.com/video/4')])),encoding='utf-8')
        self.jobs.processes[row['id']] = SimpleNamespace(poll=lambda:0)
        self.controller.poll()
        restarted = APIVideoCommands(self.pump,SimpleNamespace(),self.config,self.path,jobs=self.jobs)
        restarted.poll()
        self.assertEqual(self.pump.send.call_count, 2)  # one acknowledgement, one completion notice
        notice, text = self.pump.send.call_args.args
        self.assertEqual((notice.sender, notice.thread),('2','0:1:1:2'))
        self.assertIn('1条视频评论区@你',text)

    def test_uncertain_completion_is_not_retried_and_does_not_stop_listener(self):
        from social_bot.platforms.douyin.backend import DouyinAPIError
        self.controller.handle(self.event)
        row = self.jobs.db.execute('SELECT * FROM jobs').fetchone()
        (self.jobs.directory/(row['id']+'.result.json')).write_text(json.dumps(
            dict(success=False,opened=0,records=[])),encoding='utf-8')
        self.jobs.processes[row['id']]=SimpleNamespace(poll=lambda:0)
        self.pump.send.side_effect=DouyinAPIError('DOUYIN_SEND_RESULT_UNCONFIRMED')
        self.controller.poll()
        self.controller.poll()
        self.assertEqual(self.pump.send.call_count,2)
        self.assertEqual(self.db.execute('SELECT notified FROM douyin_video_routes').fetchone()[0],-1)

    def test_unbound_public_and_malformed_commands_do_not_spawn(self):
        from dataclasses import replace
        self.assertFalse(self.controller.handle(replace(self.event,sender='stranger')))
        self.assertFalse(self.controller.handle(replace(self.event,channel='comment')))
        self.assertTrue(self.controller.handle(replace(self.event,text='刷视频 99条')))
        self.launch.assert_not_called()

    def test_each_sender_binds_its_own_mention_target(self):
        from dataclasses import replace
        event = replace(self.event, id='100', sender='3', sender_name='好友3')
        self.assertTrue(self.controller.handle(event))
        row = self.jobs.db.execute('SELECT * FROM jobs').fetchone()
        child = json.loads((self.jobs.directory/(row['id']+'.config.json')).read_text(encoding='utf-8'))
        preset = child['video_social']['comment_preset']
        self.assertEqual((preset['owner_uid'], preset['owner_name'], preset['owner_sec_uid']),
                         ('3', '好友3', 'MS4sec3'))
        route = json.loads(self.db.execute('SELECT payload FROM douyin_video_routes').fetchone()[0])
        self.assertEqual(route['sender'], '3')

    def test_message_mode_skips_public_mention_and_private_delivers_understanding(self):
        controller = self.message_controller()
        self.assertTrue(controller.handle(self.event))
        self.pump.api.mention_target.assert_not_called()
        row = self.jobs.db.execute('SELECT * FROM jobs').fetchone()
        child = json.loads((self.jobs.directory/(row['id']+'.config.json')).read_text(encoding='utf-8'))
        self.assertFalse(child['video_social']['post_comments'])
        self.assertFalse(child['video_social']['comment_preset']['enabled'])
        (self.jobs.directory/(row['id']+'.result.json')).write_text(json.dumps(dict(success=True, attempted=1,
            records=[dict(url='https://www.douyin.com/video/4', title='厨房短剧',
                          understanding='画面里一人在厨房切菜', analysis_success=True)])), encoding='utf-8')
        self.jobs.processes[row['id']] = SimpleNamespace(poll=lambda: 0)
        controller.poll()
        texts = [call.args[1] for call in self.pump.send.call_args_list]
        self.assertEqual(len(texts), 3)  # acknowledgement, understanding, summary
        self.assertTrue(any(t.startswith('@好友2') and '厨房短剧' in t for t in texts))
        self.assertTrue(any('已私信1条解读' in t for t in texts))
        self.assertFalse(any('评论区' in t for t in texts))


if __name__ == '__main__':
    unittest.main()

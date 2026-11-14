import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from social_bot.platforms.douyin.backend import (APIVideoBrowser, DouyinAPIError,
    SpiderClient, backend_options, read_credentials)
from social_bot.platforms.douyin.backend_worker import Worker, BackendFault, classify, varint_field, envelope, feed_envelope
from social_bot.platforms.douyin.api_inbox import APIInboxPump, push_event, peer_bindings
from social_bot.video_evidence import EvidenceSession
from social_bot.modules.video import VideoError

URL = 'https://www.douyin.com/video/123'


class ContentTests(unittest.TestCase):
    def test_feed_filters_long_collections_before_paid_selection(self):
        api = Mock()
        api.feed.return_value = [dict(aweme_id='123',video=dict(duration=20000)),
                                 dict(aweme_id='124',video=dict(duration=1800000))]
        browser = APIVideoBrowser(api, max_source_seconds=300)
        self.assertEqual([x['url'] for x in browser.recommend()['candidates']], [URL])

    def test_live_room_cards_are_skipped_not_opened(self):
        api = Mock()
        api.feed.return_value = [dict(aweme_id='123', cell_room=dict(id_str='9')),
                                 dict(aweme_id='124', desc='普通视频', video=dict(duration=1000))]
        browser = APIVideoBrowser(api, max_source_seconds=0)
        observation = browser.recommend()
        self.assertEqual([x['url'] for x in observation['candidates']],
                         ['https://www.douyin.com/video/124'])
        self.assertEqual(observation['live_skipped'], 1)

    def test_zero_max_source_seconds_removes_the_duration_cap(self):
        api = Mock()
        api.feed.return_value = [dict(aweme_id='123', video=dict(duration=1800000))]
        api.detail.return_value = dict(aweme_id='123', desc='长合集', video=dict(duration=1800000))
        browser = APIVideoBrowser(api, max_source_seconds=0, filter_long=True)
        self.assertEqual([x['url'] for x in browser.recommend()['candidates']], [URL])
        self.assertEqual(browser.next(URL)['video']['duration'], 1800)

    def test_measured_cards_schema_decodes_without_treating_ads_as_candidates(self):
        value = dict(status_code=0, cards=[dict(type=1, aweme=json.dumps(dict(aweme_id='123'))),
            dict(type=1, aweme=json.dumps(dict(aweme_id='124', is_ads=True)))])
        self.assertEqual(feed_envelope(value)['aweme_list'], [dict(aweme_id='123')])
        with self.assertRaises(BackendFault):
            feed_envelope(dict(status_code=0, cards=[dict(aweme='not json')]))

    def test_feed_detail_and_media_without_page_or_claimed_playback(self):
        api = Mock()
        api.feed.return_value = [dict(aweme_id='123', desc='短片')]
        api.detail.return_value = dict(aweme_id='123', desc='短片', video=dict(duration=8000,
            play_addr=dict(url_list=['https://attacker.invalid/video', 'https://v.example.douyinvod.com/a.mp4'])))
        browser = APIVideoBrowser(api)
        self.assertEqual(browser.recommend()['candidates'][0]['url'], URL)
        self.assertEqual(browser.next(URL)['video']['duration'], 8)
        self.assertIsNone(browser.page)
        self.assertEqual(browser.played, set())
        self.assertEqual(browser.media_address(), 'https://v.example.douyinvod.com/a.mp4')
        with self.assertRaises(VideoError):
            browser.next(URL)

    def test_media_binding_cannot_drift_to_another_video(self):
        browser = APIVideoBrowser(Mock())
        browser.current = URL
        browser.current_detail = dict(aweme_id='999')
        with self.assertRaisesRegex(DouyinAPIError, 'DOUYIN_VIDEO_MISMATCH'):
            browser.media_address()

    def test_media_uses_smaller_rendition_and_prefers_cdn_over_redirect(self):
        browser = APIVideoBrowser(Mock())
        browser.current = URL
        browser.current_detail = dict(aweme_id='123', video=dict(
            play_addr=dict(data_size=600000000, url_list=['https://v.douyinvod.com/large.mp4']),
            bit_rate=[dict(play_addr=dict(data_size=20000000, url_list=[
                'https://www.douyin.com/aweme/v1/play/', 'https://v.douyinvod.com/small.mp4']))]))
        self.assertEqual(browser.media_address(), 'https://v.douyinvod.com/small.mp4')

    def test_separate_audio_is_bound_to_the_same_detail_and_host_allowlist(self):
        browser = APIVideoBrowser(Mock())
        browser.current = URL
        browser.current_detail = dict(aweme_id='123', video=dict(bit_rate_audio=[dict(audio_meta=dict(size=100,
            url_list=dict(main_url='https://evil.invalid/a.m4a', backup_url='https://v.douyinvod.com/a.m4a')))]))
        self.assertEqual(browser.audio_address(), 'https://v.douyinvod.com/a.m4a')

    def test_separate_audio_download_does_not_require_video_track(self):
        with tempfile.TemporaryDirectory() as temp:
            browser = APIVideoBrowser(Mock())
            browser.current = URL
            browser.current_detail = dict(aweme_id='123', video=dict(bit_rate_audio=[dict(audio_meta=dict(size=100,
                url_list=dict(main_url='https://v.douyinvod.com/a.m4a')))]))
            session = EvidenceSession(Mock(policy={}), browser, Path(temp), 5, time.monotonic()+60, lambda: True)
            session.duration = 5
            with patch('social_bot.video_evidence.download_media', return_value=dict(duration=4, has_audio=True)) as download:
                session._try_audio()
                self.assertFalse(download.call_args.kwargs['require_video'])
                self.assertEqual(session.duration, 4)
                self.assertEqual(session.audio_media.name, 'source-audio.m4a')

    def test_api_evidence_download_uses_direct_source_and_never_browser_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            browser = APIVideoBrowser(Mock())
            browser.current = URL
            browser.current_detail = dict(aweme_id='123', video=dict(duration=8000,
                play_addr=dict(url_list=['https://v.example.douyinvod.com/a.mp4'])))
            collector = Mock(policy={})
            session = EvidenceSession(collector, browser, Path(temp), 5, time.monotonic()+60, lambda: True)
            session.duration = 5
            with patch('social_bot.video_evidence.download_media', return_value=dict(duration=8, has_audio=True)) as download:
                session._try_media()
                self.assertEqual(download.call_args.args[0], browser.media_address())
                self.assertEqual(session.duration, 5)
            with self.assertRaisesRegex(VideoError, 'api_media_unavailable'):
                session._browser_frames([1, 2], 0)
            browser.current = 'https://www.douyin.com/video/999'
            with self.assertRaisesRegex(VideoError, 'video_changed'):
                session.guard()

    def test_missing_envelope_does_not_mean_empty_feed(self):
        for value in ({}, {'status_code': 0}, {'status_code': 8}, ''):
            with self.assertRaises(BackendFault):
                envelope(value, 'aweme_list')
        self.assertEqual(envelope({'status_code': 0, 'aweme_list': []}, 'aweme_list')['aweme_list'], [])


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.peers = peer_bindings(dict(peers=[dict(uid='2', conversation_id='0:1:1:2', short_id='8')]), '1')
        self.row = dict(sender='2', server_message_id='99', conversation_id='0:1:1:2',
            conversation_short_id='8', conversation_type=1, message_type=7,
            content=json.dumps(dict(text='你好')), create_time=int(time.time()))

    def test_verified_private_push_has_stable_identity(self):
        event = push_event(self.row, '1', self.peers, time.time()-10)
        self.assertEqual((event.sender, event.id, event.thread), ('2', '99', '0:1:1:2'))
        self.assertEqual(push_event(self.row, '1', self.peers, time.time()-10).key, event.key)

    def test_old_self_group_unknown_and_misrouted_pushes_are_ignored(self):
        for overrides in [dict(sender='1'), dict(sender='3'), dict(create_time=1), dict(create_time=0),
                          dict(conversation_id='0:1:1:3'), dict(conversation_type=2),
                          dict(message_type=27), dict(conversation_short_id='9'), dict(server_message_id='')]:
            self.assertIsNone(push_event({**self.row, **overrides}, '1', self.peers, time.time()-10))

    def test_binding_rejects_same_names_and_ambiguous_conversations(self):
        with self.assertRaises(DouyinAPIError):
            peer_bindings(dict(peers=[dict(uid='2', conversation_id='0:1:3:2', short_id='8')]), '1')

    def test_timestamp_survives_minimal_upstream_proto(self):
        # Field 8 bytes, then field 10 varint = 150.
        self.assertEqual(varint_field(b'\x42\x02{}\x50\x96\x01', 10), 150)
        self.assertEqual(varint_field(b'\x42\x02{}', 10), 0)
        with self.assertRaises(ValueError):
            varint_field(b'\x42\xff\x01x', 10)

    def test_duplicate_push_and_restart_do_not_resend_unknown_delivery(self):
        from social_bot.service import BotService
        from social_bot.settings import AppSettings, DeepSeekSettings, ScopeSettings, RuntimeSettings, WeChatSettings
        with tempfile.TemporaryDirectory() as temp:
            settings = AppSettings(api_key='fake', deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
                scope=ScopeSettings(), runtime=RuntimeSettings(database_path=str(Path(temp)/'test.db'),
                    cooldown_seconds=0, tool_calling_enabled=False))
            client = Mock()
            client.complete.return_value = '答复'
            service = BotService(settings, client)
            credentials = dict(peers=[dict(uid='2', conversation_id='0:1:1:2', short_id='8', ticket='local-ticket')])
            api = Mock(uid='1')
            api.call.side_effect = [dict(connected=True), [self.row, self.row], RuntimeError('send timed out')]
            try:
                pump = APIInboxPump(api, service, {}, credentials)
                with self.assertRaises(RuntimeError):
                    pump.step()
                self.assertEqual(service.store.connection.execute('SELECT state FROM social_deliveries').fetchone()[0], 'unknown')
                api.call.side_effect = [dict(connected=True), [self.row]]
                restarted = APIInboxPump(api, service, {}, credentials)
                restarted.step()
                self.assertEqual(sum(c.args[0] == 'send' for c in api.call.call_args_list), 1)
            finally:
                service.close()


class BrowserCommentTests(unittest.TestCase):
    def publisher(self):
        from social_bot.platforms.douyin.browser_comment import BrowserCommentPublisher
        return BrowserCommentPublisher(dict(memory_dir='E:/memory', account_id='default',
                                            browser_channel='msedge'), '1')

    def test_invalid_url_text_and_length_are_rejected_before_any_browser_use(self):
        from social_bot.platforms.douyin.adapter import BridgeError
        publisher = self.publisher()
        target = dict(uid='2', name='gk', sec_uid='MS4abc')
        for url, text in [('https://example.invalid/video/1', '@gk 内容'),
                          ('https://www.douyin.com/video/1', '没有前缀'),
                          ('https://www.douyin.com/video/1', '@gk ' + 'x' * 200)]:
            with self.assertRaises(BridgeError) as caught:
                publisher.publish(url, text, target)
            self.assertTrue(caught.exception.before_write)

    def test_avatar_key_parsing_matches_mention_candidates(self):
        from social_bot.platforms.douyin.browser_comment import BrowserCommentPublisher
        self.assertEqual(
            BrowserCommentPublisher._avatar_key(
                'https://p3.douyinpic.com/aweme/100x100/aweme-avatar/tos-cn-i-0813c000-ce_abc123XYZ.jpeg?from=1'),
            'tos-cn-i-0813c000-ce_abc123XYZ')
        self.assertEqual(
            BrowserCommentPublisher._avatar_key(
                'https://p3-pc.douyinpic.com/img/aweme-avatar/tos-cn-i-0813c000-ce_abc123XYZ~c5_300x300.jpeg'),
            'tos-cn-i-0813c000-ce_abc123XYZ')
        self.assertEqual(BrowserCommentPublisher._avatar_key('data:image/png;base64,xx'), '')


class ReconnectTests(unittest.TestCase):
    def test_only_socket_errors_are_retryable(self):
        from social_bot.platforms.douyin.api_runner import reconnect_settings, retryable_socket_error
        for code in ('DOUYIN_SOCKET_FAILED', 'DOUYIN_SOCKET_DISCONNECTED', 'DOUYIN_SOCKET_CONNECT_TIMEOUT',
                     'DOUYIN_BACKEND_STOPPED', 'DOUYIN_BACKEND_TIMEOUT_OR_STOPPED'):
            self.assertTrue(retryable_socket_error(DouyinAPIError(code)))
        for code in ('DOUYIN_CREDENTIALS_CHANGED', 'DOUYIN_VERIFICATION_REQUIRED', 'DOUYIN_AUTH_EXPIRED',
                     'DOUYIN_DISABLED'):
            self.assertFalse(retryable_socket_error(DouyinAPIError(code)))
        self.assertEqual(reconnect_settings({}), dict(enabled=True, max_attempts=10, max_delay_seconds=60))
        self.assertFalse(reconnect_settings(dict(api_reconnect=dict(enabled=False)))['enabled'])
        self.assertEqual(reconnect_settings(dict(api_reconnect=dict(max_attempts=3, max_delay_seconds=20))),
                         dict(enabled=True, max_attempts=3, max_delay_seconds=20))
        for bad in (dict(max_attempts=0), dict(max_attempts=51), dict(max_delay_seconds=2), dict(enabled='yes')):
            with self.assertRaises(DouyinAPIError):
                reconnect_settings(dict(api_reconnect=bad))
        with self.assertRaises(DouyinAPIError):
            reconnect_settings(dict(api_reconnect=[]))

    def test_reconnect_rebuilds_worker_and_resubscribes(self):
        from social_bot.platforms.douyin.api_runner import reconnect_session
        calls = []
        class Api:
            failures, listened = 2, 0
            def close(self): calls.append('close')
            def start(self):
                calls.append('start')
                if self.failures:
                    self.failures -= 1
                    raise DouyinAPIError('DOUYIN_SOCKET_FAILED')
            def call(self, method):
                calls.append(method); self.listened += 1
        api = Api()
        with patch('social_bot.platforms.douyin.api_runner.time.sleep') as sleep:
            attempt = reconnect_session(api, dict(max_attempts=5, max_delay_seconds=60), lambda: True)
        self.assertEqual(attempt, 3)
        self.assertEqual(api.listened, 1)
        self.assertEqual(calls.count('close'), 3)
        self.assertEqual(sleep.call_count, 3)

    def test_reconnect_stops_on_credentials_change(self):
        from social_bot.platforms.douyin.api_runner import reconnect_session
        class Api:
            def close(self): pass
            def start(self): raise DouyinAPIError('DOUYIN_CREDENTIALS_CHANGED')
            def call(self, method): raise AssertionError('must not resubscribe')
        with patch('social_bot.platforms.douyin.api_runner.time.sleep'):
            with self.assertRaisesRegex(DouyinAPIError, 'DOUYIN_CREDENTIALS_CHANGED'):
                reconnect_session(Api(), dict(max_attempts=3, max_delay_seconds=60), lambda: True)

    def test_reconnect_gives_up_after_max_attempts(self):
        from social_bot.platforms.douyin.api_runner import reconnect_session
        class Api:
            def close(self): pass
            def start(self): raise DouyinAPIError('DOUYIN_SOCKET_FAILED')
            def call(self, method): pass
        with patch('social_bot.platforms.douyin.api_runner.time.sleep'):
            with self.assertRaisesRegex(DouyinAPIError, 'RECONNECT_FAILED'):
                reconnect_session(Api(), dict(max_attempts=2, max_delay_seconds=60), lambda: True)


class BoundaryTests(unittest.TestCase):
    def test_worker_requires_account_and_write_credentials(self):
        worker = Worker()
        worker.auth = Mock()
        with self.assertRaisesRegex(BackendFault, 'ACCOUNT_UNCONFIRMED'):
            worker.dispatch('feed', {})
        with self.assertRaisesRegex(BackendFault, 'WRITES_DISABLED'):
            worker.require_writes()

    def test_errors_never_echo_upstream_cookies_or_urls(self):
        error = RuntimeError('sessionid=secret https://imapi.douyin.com?token=secret')
        self.assertEqual(classify(error), 'DOUYIN_BACKEND_REQUEST_FAILED')
        self.assertEqual(classify(RuntimeError('需要人机验证 token=secret')), 'DOUYIN_VERIFICATION_REQUIRED')

    def test_credential_path_is_resolved_at_original_config(self):
        config = dict(memory_dir='E:/memory', account_id='default', api_backend=dict(credentials_file='private.json'))
        options = backend_options(config, Path.cwd()/'douyin/config.json')
        self.assertEqual(Path(options['credentials']), Path.cwd()/'douyin/private.json')

    def test_zero_source_seconds_is_accepted_as_unlimited(self):
        config = dict(memory_dir='E:/memory', account_id='default', api_backend=dict(max_source_seconds=0))
        options = backend_options(config, Path.cwd()/'douyin/config.json')
        self.assertEqual(options['max_source_seconds'], 0)
        config['api_backend']['max_source_seconds'] = -1
        with self.assertRaises(ValueError):
            backend_options(config, Path.cwd()/'douyin/config.json')

    def test_invalid_credentials_are_not_used(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'credentials.json'
            path.write_text('{}')
            with self.assertRaisesRegex(DouyinAPIError, 'CREDENTIALS_INVALID'):
                read_credentials(path)


if __name__ == '__main__':
    unittest.main()

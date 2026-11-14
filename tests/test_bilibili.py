import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock

from social_bot.platforms.bilibili.client import BilibiliClient, BilibiliError, NoRedirect, video_id, wbi_sign
from social_bot.platforms.bilibili.assistant import VideoAssistant
from social_bot.store import ConversationStore


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())
        self.headers = Message()


class ClientTests(unittest.TestCase):
    def test_wbi_matches_upstream_typescript(self):
        result = wbi_sign({'foo': '114', 'bar': '514', 'baz': 1919810},
            '7cd084941338484aae1ad9425b84077c', '4932caff0ff746eab6f01bf08b70ac45', 1702204169)
        # Verified by executing upstream src/wbi.ts at commit 42cd1c9.
        self.assertEqual(result['w_rid'], '6149fdadf571698ca7e6a567265cd0ee')

    def test_ids_reject_other_hosts_and_short_links(self):
        self.assertEqual(video_id('https://www.bilibili.com/video/BV1xx411c7mD?p=2'), 'BV1xx411c7mD')
        for value in ('https://evil.com/BV1xx411c7mD', 'https://b23.tv/abc', '../secret'):
            with self.assertRaises(ValueError):
                video_id(value)

    def test_cookie_only_sent_to_api(self):
        opener = Mock()
        opener.open.side_effect = [Response({'code': 0, 'data': {}}), Response({'body': []})]
        client = BilibiliClient('private-token', opener=opener)
        client.api('/x/web-interface/nav')
        client._fetch('https://aisubtitle.hdslb.com/test.json')
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(requests[0].get_header('Cookie'), 'SESSDATA=private-token')
        self.assertIsNone(requests[1].get_header('Cookie'))
        with self.assertRaises(BilibiliError):
            client._fetch('https://evil.com/file')
        with self.assertRaises(BilibiliError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.com')

    def test_errors_and_anonymous_nav(self):
        opener = Mock()
        opener.open.side_effect = [Response({'code': -412}), Response({'code': -101, 'data': {'isLogin': False}})]
        client = BilibiliClient(opener=opener)
        with self.assertRaisesRegex(BilibiliError, 'RISK_CONTROL'):
            client.api('/test')
        self.assertFalse(client.account()['logged_in'])

    def test_search_cleaned_and_bounded(self):
        client = BilibiliClient()
        client.cookies = {'buvid3': 'test'}
        client.api = Mock(return_value={'result': [{'bvid': 'BV1xx411c7mD', 'title': '<em>测试</em>&amp;'}] * 20})
        results = client.search('测试')
        self.assertEqual(len(results), 10)
        self.assertEqual(results[0]['title'], '测试&')

    def test_selected_page_subtitles_and_truncation(self):
        client = BilibiliClient()
        client.api = Mock(side_effect=[
            {'title': '测试', 'pages': [{'cid': 1}, {'cid': 2}]},
            {'wbi_img': {'img_url': 'https://i.test/' + 'a' * 32 + '.png', 'sub_url': 'https://i.test/' + 'b' * 32 + '.png'}},
            {'subtitle': {'subtitles': [{'lan': 'zh-CN', 'subtitle_url': '//aisubtitle.hdslb.com/test'}]}}])
        client._fetch = Mock(return_value={'body': [{'from': 0, 'content': '字' * 13000}]})
        detail = client.video('BV1xx411c7mD', 2)
        result = client.subtitles(detail)
        self.assertEqual(detail['cid'], 2)
        self.assertEqual(len(result['text']), 12000)
        self.assertTrue(result['truncated'])


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ConversationStore(Path(self.temp.name) / 'test.sqlite3')
        self.api = Mock()
        self.api.video.return_value = dict(bvid='BV1xx411c7mD', cid=1, page=1, title='测试', description='简介', url='https://www.bilibili.com/video/BV1xx411c7mD')
        self.api.subtitles.return_value = {'text': '资料', 'truncated': False}
        self.model = Mock()
        self.model.complete.return_value = '总结'
        self.assistant = VideoAssistant(self.api, self.model, self.store, '123')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_followup_is_persisted_and_accounts_isolated(self):
        self.assistant.ask('BV1xx411c7mD', '第一问')
        self.assistant.ask('BV1xx411c7mD', '第二问')
        self.assertTrue(any(m['content'] == '第一问' for m in self.model.complete.call_args.args[0]))
        VideoAssistant(self.api, self.model, self.store, '456').ask('BV1xx411c7mD', '其他用户')
        self.assertFalse(any(m['content'] == '第一问' for m in self.model.complete.call_args.args[0]))

    def test_no_subtitles_is_explicit_even_if_model_omits_notice(self):
        self.api.subtitles.side_effect = BilibiliError('SUBTITLES_UNAVAILABLE')
        result = self.assistant.ask('BV1xx411c7mD', '总结')
        self.assertIn('未取得字幕', result)
        self.assertIn('来源：https://', result)

    def test_risk_control_does_not_call_model(self):
        self.api.subtitles.side_effect = BilibiliError('RISK_CONTROL')
        with self.assertRaises(BilibiliError):
            self.assistant.ask('BV1xx411c7mD', '总结')
        self.model.complete.assert_not_called()

    def test_failed_generation_does_not_save_answer(self):
        self.model.complete.side_effect = RuntimeError('failed')
        with self.assertRaises(RuntimeError):
            self.assistant.ask('BV1xx411c7mD', '总结')
        self.assertEqual(self.store.history('bilibili:123:BV1xx411c7mD:1', 8), [])


if __name__ == '__main__':
    unittest.main()

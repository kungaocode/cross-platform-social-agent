import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from social_bot.platforms.douyin.backend import APIVideoBrowser, DouyinAPIError
from social_bot.video_social import VideoCurator, policy_from


class RecommendationOrderTests(unittest.TestCase):
    def make(self, count=2, available=3, snapshot=None):
        self.events = []
        api = Mock(uid='1')
        def feed(index):
            self.events.append('feed')
            return [dict(aweme_id=str(i), desc='任意推荐'+str(i),video=dict(duration=10000))
                    for i in range(1,available+1)]
        api.feed.side_effect = feed
        def detail(url):
            self.events.append('open:'+url.rsplit('/',1)[1])
            return dict(aweme_id=url.rsplit('/',1)[1],video=dict(duration=10000))
        api.detail.side_effect = detail
        browser = APIVideoBrowser(api,max_videos=count,filter_long=False,snapshot_path=snapshot)
        def analyze(*args,**kwargs):
            self.events.append('analyze:'+browser.current.rsplit('/',1)[1])
            return dict(success=False,reason='fixture_analysis_failure')
        tools = SimpleNamespace(browser=browser,store=Mock(),execute=Mock(side_effect=analyze))
        client = Mock()
        client.complete_json.side_effect = AssertionError('must not select by interest')
        policy = policy_from(dict(video_social=dict(enabled=True,max_videos_per_run=count,
            selection_mode='recommendation_order',post_comments=False)))
        registry = Mock()
        registry.state.return_value = None
        curator = VideoCurator(tools,client,registry,Mock(),policy,account='1')
        return curator, api

    def test_order_and_quantity_have_only_one_refresh_after_all_items(self):
        curator, api = self.make()
        report = curator.run()
        self.assertEqual(self.events, ['feed','open:1','analyze:1','open:2','analyze:2','feed'])
        self.assertEqual([r['url'].rsplit('/',1)[1] for r in report['records']], ['1','2'])
        self.assertEqual(report['attempted'],2)
        self.assertTrue(report['recommendation_refresh']['success'])
        api.search.assert_not_called()

    def test_short_batch_reports_shortfall_without_refreshing_to_fill(self):
        curator, api = self.make(count=5,available=2)
        report = curator.run()
        self.assertEqual(report['shortfall'],3)
        self.assertEqual(api.feed.call_count,2)
        self.assertEqual(len(report['records']),2)

    def test_long_video_is_skipped_and_next_short_videos_fill_count(self):
        curator, api = self.make()
        original = api.detail.side_effect
        api.detail.side_effect = lambda url: dict(original(url),video=dict(duration=999000)) if url.endswith('/1') else original(url)
        report = curator.run()
        self.assertEqual(report['skipped'][0]['reason'],'DOUYIN_VIDEO_TOO_LONG')
        self.assertEqual(self.events,['feed','open:1','open:2','analyze:2','open:3','analyze:3','feed'])
        self.assertEqual([r['url'].rsplit('/',1)[1] for r in report['records']],['2','3'])
        self.assertEqual(report['shortfall'],0)

    def test_exactly_five_minutes_is_allowed_but_unknown_duration_is_skipped(self):
        curator, api=self.make()
        original=api.detail.side_effect
        def detail(url):
            value=original(url)
            value['video']['duration']=300000 if url.endswith('/1') else None if url.endswith('/2') else 1000
            return value
        api.detail.side_effect=detail
        report=curator.run()
        self.assertEqual([r['url'].rsplit('/',1)[1] for r in report['records']],['1','3'])
        self.assertEqual(report['skipped'][0]['reason'],'DOUYIN_VIDEO_DURATION_UNKNOWN')

    def test_unavailable_detail_is_skipped_without_faking_identity_mismatch(self):
        from social_bot.platforms.douyin.backend import SpiderClient
        api=SpiderClient({})
        api.call=Mock(return_value=dict(aweme_detail=None))
        with self.assertRaisesRegex(DouyinAPIError,'DOUYIN_VIDEO_UNAVAILABLE'):
            api.detail('https://www.douyin.com/video/1')

    def test_disabling_during_batch_prevents_refresh(self):
        curator, api = self.make()
        permitted = [True]
        def analyze(*args,**kwargs):
            permitted[0]=False
            return dict(success=False)
        curator.tools.execute.side_effect=analyze
        curator.permitted=lambda:permitted[0]
        report=curator.run()
        self.assertEqual(api.feed.call_count,1)
        self.assertFalse(report['recommendation_refresh']['attempted'])

    def test_refresh_failure_does_not_claim_refreshed_or_reprocess(self):
        curator, api = self.make(count=1)
        original=api.feed.side_effect
        def feed(index):
            if api.feed.call_count>1:raise DouyinAPIError('DOUYIN_TIMEOUT')
            return original(index)
        api.feed.side_effect=feed
        report=curator.run()
        self.assertEqual(report['attempted'],1)
        self.assertEqual(report['recommendation_refresh'],dict(attempted=True,success=False,reason='DOUYIN_TIMEOUT'))

    def test_next_command_reuses_end_of_batch_snapshot_before_refreshing(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshot=Path(temp)/'feed.json'
            curator, api=self.make(snapshot=snapshot)
            curator.run()
            self.assertTrue(snapshot.is_file())
            next_browser=APIVideoBrowser(api,filter_long=False,snapshot_path=snapshot)
            initial=next_browser.initial_recommendations()
            self.assertEqual(len(initial['candidates']),3)
            self.assertEqual(api.feed.call_count,2)
            self.assertFalse(snapshot.exists())


if __name__=='__main__':
    unittest.main()

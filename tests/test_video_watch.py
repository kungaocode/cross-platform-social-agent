import unittest
from unittest.mock import Mock

from social_bot.video_social import policy_from
from social_bot.video_watch import RunThreads, watch_comments


class VideoWatchTests(unittest.TestCase):
    def test_share_mode_never_polls_even_with_old_watch_duration(self):
        poll,wait=Mock(),Mock()
        policy=policy_from({'video_social':{'interaction_mode':'share','watch_after_seconds':120}})
        result=watch_comments(policy,[dict(comment_root_id='22')],poll,lambda:True,wait)
        self.assertFalse(result['enabled'])
        poll.assert_not_called();wait.assert_not_called()

    def test_watch_runs_exactly_two_hours_on_an_independent_clock(self):
        now=[900.0]
        poll=Mock()
        policy=policy_from({'video_social':{'interaction_mode':'watch','max_runtime_seconds':30,
            'comment_preset':{'enabled':True,'owner_uid':'123','owner_name':'主人'}}})
        self.assertFalse(policy['comment_preset']['enabled'])
        result=watch_comments(policy,[dict(comment_root_id='22')],poll,lambda:True,
            lambda seconds:now.__setitem__(0,now[0]+seconds),clock=lambda:now[0])
        self.assertTrue(result['completed'])
        self.assertEqual(now[0],8100)
        self.assertEqual(poll.call_count,360)

    def test_cancellation_and_no_published_roots_do_not_keep_watching(self):
        poll=Mock()
        policy={'interaction_mode':'watch'}
        result=watch_comments(policy,[],poll,lambda:True,Mock())
        self.assertFalse(result['enabled'])
        result=watch_comments(policy,[dict(comment_root_id='22')],poll,lambda:False,Mock())
        self.assertFalse(result['completed'])
        poll.assert_not_called()

    def test_only_current_run_roots_are_visible(self):
        registry=Mock()
        registry.owns.return_value=True
        registry.get.return_value={'root_id':'22'}
        scoped=RunThreads(registry,['22'])
        self.assertTrue(scoped.owns('bilibili','100','22',video_id='33'))
        self.assertFalse(scoped.owns('bilibili','100','old'))
        self.assertIsNone(scoped.get('bilibili','100','old'))
        registry.get.assert_not_called()
        registry.owns.assert_called_once_with('bilibili','100','22',video_id='33')


if __name__=='__main__':
    unittest.main()

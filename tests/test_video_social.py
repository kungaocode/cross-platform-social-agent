import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from dataclasses import replace

from social_bot.platforms.bilibili.social import BilibiliSocialClient, BilibiliSource, mention_event
from social_bot.platforms.bilibili.video_social import BilibiliVideoInteraction
from social_bot.platforms.bilibili.chat import BilibiliChatPump
from social_bot.platforms.bilibili.client import BilibiliError
from social_bot.video_participation import ThreadRegistry
from social_bot.video_social import VideoCurator, policy_from
from social_bot.modules.video import VideoError
from social_bot.store import ConversationStore
from social_bot.settings import AppSettings, RuntimeSettings, ScopeSettings, DeepSeekSettings, WeChatSettings
from social_bot.service import BotService
from tests.test_social_chat import Client

URL = "https://www.bilibili.com/video/BV1xx411c7mD"
OTHER = "https://www.bilibili.com/video/BV1yy411c7mD"


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.registry = ThreadRegistry(self.path / "threads.sqlite3", clock=lambda: 1000)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.registry.close()
        self.temp.cleanup()


class ParticipationTests(StorageTests):
    def test_unknown_publication_survives_restart_without_retry(self):
        self.assertTrue(self.registry.claim("bilibili", "100", URL, "正文"))
        self.assertFalse(self.registry.finish("bilibili", "100", URL))
        reopened = ThreadRegistry(self.path / "threads.sqlite3", clock=lambda: 2000)
        try:
            self.assertEqual(reopened.state("bilibili", "100", URL), "unknown")
            self.assertFalse(reopened.claim("bilibili", "100", URL + "?p=2", "重试", cooldown=0))
        finally:
            reopened.close()

    def test_receipt_without_claim_cannot_register_an_owned_root(self):
        self.assertFalse(self.registry.finish("bilibili", "100", URL, dict(root_id="22", video_id="33")))
        self.assertFalse(self.registry.owns("bilibili", "100", "22"))

    def test_account_video_binding_and_cooldown(self):
        self.assertTrue(self.registry.claim("bilibili", "100", URL, "正文"))
        self.assertTrue(self.registry.finish("bilibili", "100", URL, dict(root_id="22", video_id="33")))
        self.assertTrue(self.registry.owns("bilibili", "100", "22", video_id="33"))
        self.assertFalse(self.registry.owns("bilibili", "101", "22"))
        self.assertFalse(self.registry.owns("bilibili", "100", "22", video_id="34"))
        self.assertFalse(self.registry.claim("bilibili", "100", OTHER, "正文"))

    def test_two_process_connections_cannot_bypass_comment_cooldown(self):
        barrier, results, errors = threading.Barrier(2), [], []
        def worker(url):
            registry = ThreadRegistry(self.path / "threads.sqlite3", clock=lambda: 1000)
            try:
                barrier.wait(5)
                results.append(registry.claim("bilibili", "100", url, "正文"))
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                registry.close()
        threads = [threading.Thread(target=worker, args=(url,)) for url in (URL, OTHER)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [False, True])

    def test_replies_require_an_owned_root_and_share_at_delivery_key(self):
        row = dict(id=900, reply_time=time.time(), user=dict(mid=456), item=dict(type="reply", business_id=1,
            subject_id=33, root_id=22, source_id=44, source_content="剧情很有趣", uri=URL))
        api = Mock()
        api.replies.return_value = [row]
        source = BilibiliSource(api, "100", self.registry)
        self.assertEqual(source.poll("replies"), [])
        self.registry.claim("bilibili", "100", URL, "正文")
        self.registry.finish("bilibili", "100", URL, dict(root_id="22", video_id="33"))
        event = source.poll("replies")[0]
        self.assertTrue(event.reply_to_own)
        self.assertFalse(event.mentioned)
        self.assertEqual(event.source_video, URL)
        self.assertEqual(event.key, mention_event({**row, "id": 901}, "100").key)


class BilibiliInboxTests(unittest.TestCase):
    def test_disabled_comment_monitor_polls_private_sessions_only(self):
        with tempfile.TemporaryDirectory() as folder:
            settings=AppSettings(api_key='fixture',deepseek=DeepSeekSettings(),wechat=WeChatSettings(),
                runtime=RuntimeSettings(database_path=str(Path(folder)/'bot.sqlite3')),
                scope=ScopeSettings(private_users=frozenset()))
            service=BotService(settings,Client())
            try:
                api=Mock()
                api.sessions.return_value=['456']
                api.messages.return_value=[]
                pump=BilibiliChatPump(api,service,dict(private_chat_mode='all_sessions',
                    comments_enabled=False,comment_replies_enabled=False),'100',None)
                pump.step()
                api.messages.assert_called_once_with('456')
                api.replies.assert_not_called()
                api.mentions.assert_not_called()
            finally:service.close()

    def test_sessions_are_paginated_and_only_private_chats_are_selected(self):
        api = BilibiliSocialClient("fixture", "fixture")
        api._social = Mock(side_effect=[
            dict(has_more=1, session_list=[dict(talker_id=456, session_type=1, session_ts=200),
                                         dict(talker_id=999, session_type=2, session_ts=190)]),
            dict(has_more=0, session_list=[dict(talker_id=789, session_type=1, session_ts=100)])])
        self.assertEqual(api.sessions(), ["456", "789"])
        self.assertEqual(api._social.call_args_list[1].args[2]["begin_ts"], 190)

    def test_repeating_session_cursor_is_an_error_instead_of_silent_partial_selection(self):
        api = BilibiliSocialClient("fixture", "fixture")
        api._social = Mock(return_value=dict(has_more=1, session_list=[
            dict(talker_id=456, session_type=1, session_ts=200)]))
        with self.assertRaisesRegex(BilibiliError, "SESSION_PAGINATION"):
            api.sessions()

    def test_top_level_comment_uses_video_oid_and_zero_parent_once(self):
        api = BilibiliSocialClient("fixture", "fixture")
        api.video = Mock(return_value=dict(aid=33))
        api._social = Mock(return_value=dict(rpid=22))
        self.assertEqual(api.post_video_comment(URL, "这个片段很有趣 😊")["root_id"], "22")
        form = api._social.call_args.kwargs["data"]
        self.assertEqual((form["oid"], form["parent"], form["root"]), ("33", 0, 0))
        api._social.assert_called_once()

    def test_account_switch_prevents_comment(self):
        api = Mock()
        api.account.return_value = dict(logged_in=True, uid="999")
        interaction = BilibiliVideoInteraction(api, "100", None)
        with self.assertRaisesRegex(BilibiliError, "ACCOUNT_MISMATCH"):
            interaction.publish(URL, "正文")
        api.post_video_comment.assert_not_called()

    def test_browser_cookie_adoption_checks_account_and_domain(self):
        api = BilibiliSocialClient("fixture-session", "fixture-csrf")
        api.set_browser_cookies([dict(name="buvid3", value="normal-cookie", domain=".bilibili.com"),
                                dict(name="buvid4", value="foreign-cookie", domain=".other.invalid")])
        self.assertEqual(api.cookies, dict(buvid3="normal-cookie"))
        with self.assertRaisesRegex(BilibiliError, "ACCOUNT_MISMATCH"):
            api.set_browser_cookies([dict(name="SESSDATA", value="another-session", domain=".bilibili.com")])

    def test_metadata_failure_is_known_to_happen_before_the_comment_write(self):
        api = BilibiliSocialClient("fixture", "fixture")
        api.video = Mock(side_effect=BilibiliError("HTTP_412"))
        api._social = Mock()
        with self.assertRaises(BilibiliError) as caught:
            api.post_video_comment(URL, "画面有趣")
        self.assertTrue(caught.exception.before_write)
        api._social.assert_not_called()

    def test_transient_account_poll_failure_does_not_stop_public_video_watching(self):
        api = Mock()
        api.account.side_effect = BilibiliError("NETWORK_ERROR")
        interaction = BilibiliVideoInteraction(api, "100", None)
        interaction.poll()
        self.assertTrue(interaction.hints["account_read_temporarily_unavailable"])
        api.post_video_comment.assert_not_called()

    def test_read_transport_fallback_never_retries_http_rejections_or_posts(self):
        from urllib.request import Request
        from urllib.error import URLError, HTTPError
        api = BilibiliSocialClient("fixture", "fixture", opener=Mock())
        api._read_fallback = True
        api._direct_opener = Mock()
        api.opener.open.side_effect = URLError("fixture offline")
        api._open(Request("https://api.bilibili.com/x/web-interface/nav"))
        api._direct_opener.open.assert_called_once()
        api._prefer_direct_until = 0
        api._direct_opener.reset_mock()
        with self.assertRaises(URLError):
            api._open(Request("https://api.bilibili.com/x/v2/reply/add", data=b"fixture", method="POST"))
        api._direct_opener.open.assert_not_called()
        api.opener.open.side_effect = HTTPError("https://api.bilibili.com/", 412, "fixture", {}, None)
        with self.assertRaises(HTTPError):
            api._open(Request("https://api.bilibili.com/x/web-interface/nav"))
        api._direct_opener.open.assert_not_called()

    def test_new_inbox_contact_first_message_is_allowed_but_history_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(api_key="fixture", deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
                runtime=RuntimeSettings(database_path=str(Path(folder) / "bot.sqlite3"), cooldown_seconds=0),
                scope=ScopeSettings(private_users=frozenset(), group_activation_enabled=False))
            service = BotService(settings, Client())
            try:
                api = Mock()
                api.sessions.side_effect = [["456"], ["456", "789"]]
                def messages(peer):
                    return [dict(msg_key=1, msg_seqno=1, sender_uid=int(peer), receiver_id=100,
                        receiver_type=1, msg_type=1, content='{"content":"你好"}',
                        timestamp=1 if peer == "456" else time.time() + 1)]
                api.messages.side_effect = messages
                pump = BilibiliChatPump(api, service, {"private_chat_mode": "all_sessions"}, "100", None)
                pump.step()
                api.send_private.assert_not_called()
                pump.next_refresh = 0
                pump.step()
                api.send_private.assert_called_once()
                self.assertEqual(api.send_private.call_args.args[:2], ("100", "789"))
                self.assertIn("789", service.settings.scope.private_users)
                pump.step()
                self.assertEqual(api.send_private.call_count, 1)
            finally:
                service.close()

    def test_temporary_read_failure_does_not_establish_a_baseline_or_replay_history(self):
        from social_bot.platforms.common.chat import PollLoop, SocialEvent
        responder = Mock()
        responder.permitted.return_value = True
        old = SocialEvent("bilibili", "100", "private", "old", "456", "历史", "456", created_at=1)
        source = Mock(side_effect=[None, [old]])
        loop = PollLoop(responder, source, Mock(), baseline_before=time.time())
        loop.step(["456"])
        self.assertNotIn("456", loop.initialized)
        responder.db.execute.return_value.fetchone.return_value = (1,)
        loop.step(["456"])
        responder.baseline.assert_called_once_with([old])
        responder.handle.assert_not_called()


class CuratorTests(StorageTests):
    def make_curator(self, *, success=True, permitted=lambda: True):
        self.store = ConversationStore(self.path / "videos.sqlite3", agent_memory_path=self.path / "shared.sqlite3")
        self.stores.append(self.store)
        browser = SimpleNamespace(platform="bilibili", visited=[], recommend=Mock(return_value={
            "blocked": False, "candidates": [dict(url=URL, title="大肥鱼二创", visited=False)]}), next=Mock())
        browser.next.side_effect = lambda url: browser.visited.append(url)
        evidence = dict(success=success, text="画面中人物打开一扇门，随后发现了一只戴帽子的猫。",
                        title="大肥鱼二创", url=URL, coverage_start_seconds=0, coverage_end_seconds=60, partial=True)
        tools = SimpleNamespace(browser=browser, store=self.store, execute=Mock(return_value=evidence))
        client = Mock()
        client.complete_json.return_value = dict(url=URL)
        client.complete.return_value = "开门遇到戴帽子的猫，这个反转挺有趣 😊"
        publish = Mock(return_value=dict(root_id="22", video_id="33"))
        policy = policy_from({"video_social": {"enabled": True, "max_videos_per_run": 1}})
        curator = VideoCurator(tools, client, self.registry, publish, policy, account="100", permitted=permitted)
        return curator, client, publish

    def test_dry_run_generates_a_draft_without_claim_or_send(self):
        curator, client, publish = self.make_curator()
        report = curator.run(dry_run=True)
        self.assertEqual(report["records"][0]["publication"], "draft")
        publish.assert_not_called()
        self.assertIsNone(self.registry.state("bilibili", "100", URL))

    def test_two_sharing_comments_wait_for_spacing_and_continue_to_second_video(self):
        curator,client,publish=self.make_curator()
        now=[1000.0]
        self.registry.clock=lambda:now[0]
        curator.clock=lambda:now[0]
        curator.deadline=2000
        curator.wait=lambda seconds:now.__setitem__(0,now[0]+seconds)
        curator.policy.update(max_videos_per_run=2,max_comments_per_run=2)
        curator.policy['comment_preset'].update(enabled=True,owner_uid='456',owner_name='指定主人')
        curator.tools.execute.return_value['sources']=[dict(id='E1',kind='frames',text='戴帽子的猫')]
        curator.browser.recommend.side_effect=lambda:dict(candidates=[
            dict(url=url,title='猫',visited=url in curator.browser.visited) for url in (URL,OTHER)])
        body=dict(intro='我刚看到一只猫',visual='猫戴着帽子',visual_evidence_ids=['E1'])
        client.complete_json.side_effect=[dict(url=URL),body,dict(url=OTHER),body]
        publish.side_effect=[dict(root_id='22',video_id='33'),dict(root_id='23',video_id='34')]
        report=curator.run()
        self.assertEqual((report['opened'],report['published']),(2,2))
        self.assertEqual(now[0],1180)
        self.assertTrue(all(row['comment'].startswith('@指定主人 ') for row in report['records']))

    def test_watch_mode_rejects_generated_mentions_before_publishing(self):
        curator,client,publish=self.make_curator()
        curator.policy['interaction_mode']='watch'
        client.complete.return_value='@主人 来看看这只猫'
        report=curator.run()
        self.assertEqual(report['records'][0]['publication'],'comment_generation_failed')
        publish.assert_not_called()

    def test_owner_preset_binds_name_address_and_observed_visual_citation(self):
        curator,client,publish=self.make_curator()
        curator.policy['comment_preset']=policy_from({'video_social':{'comment_preset':{
            'enabled':True,'owner_uid':'456','owner_name':'指定主人','address':'主人'}}})['comment_preset']
        curator.tools.execute.return_value.update(analysis_mode='deep',analysis_verified=True,
            sources=[dict(id='E1',kind='frames',text='开门后看到戴帽子的猫。',sampled_frame_seconds=[5,10])])
        client.complete_json.side_effect=[dict(url=URL),dict(intro='我刚看到一段开门遇猫的短片',
            visual='门后蹲着一只戴帽子的猫',visual_evidence_ids=['E1'],reaction='这个反转挺可爱 😊')]
        report=curator.run(dry_run=True)
        text=report['records'][0]['comment']
        self.assertTrue(text.startswith('@指定主人 主人，'))
        self.assertIn('画面里，门后蹲着一只戴帽子的猫',text)
        self.assertLessEqual(len(text),180)
        client.complete.assert_not_called();publish.assert_not_called()

    def test_invalid_owner_share_visual_reference_never_reaches_publisher(self):
        curator,client,publish=self.make_curator()
        curator.policy['comment_preset']=policy_from({'video_social':{'comment_preset':{
            'enabled':True,'owner_uid':'456','owner_name':'指定主人'}}})['comment_preset']
        curator.tools.execute.return_value['sources']=[dict(id='E1',kind='frames',text='猫')]
        client.complete_json.side_effect=[dict(url=URL),dict(intro='视频',visual='凭空的内容',visual_evidence_ids=['E99'])]
        report=curator.run()
        self.assertEqual(report['records'][0]['publication'],'comment_generation_failed')
        publish.assert_not_called()
        self.assertIsNone(self.registry.state('bilibili','100',URL))

    def test_failed_analysis_never_generates_or_posts_a_comment(self):
        curator, client, publish = self.make_curator(success=False)
        report = curator.run()
        self.assertFalse(report["records"][0]["analysis_success"])
        client.complete.assert_not_called()
        publish.assert_not_called()

    def test_unresolved_deep_conclusion_is_never_used_for_a_public_comment(self):
        curator, client, publish = self.make_curator()
        curator.tools.execute.return_value.update(analysis_mode="deep",analysis_verified=False,comment_allowed=False)
        result=curator.run()
        self.assertEqual(result["records"][0]["publication"],"unverified_conclusion_no_comment")
        client.complete.assert_not_called()
        publish.assert_not_called()

    def test_hallucinated_url_is_rejected_before_opening_or_analysis(self):
        curator, client, publish = self.make_curator()
        client.complete_json.return_value = dict(url=OTHER)
        with self.assertRaisesRegex(VideoError, "outside_observed"):
            curator.run()
        curator.tools.execute.assert_not_called()
        publish.assert_not_called()

    def test_public_comment_prompt_excludes_private_chat_memory(self):
        curator, client, publish = self.make_curator()
        self.store.add("wechat:private:owner", "user", "大肥鱼的PRIVATE_CONTACT_ONLY_X联系方式是12345")
        self.store.add("bilibili:group:100:comment:old", "user", "大肥鱼二创擅长反转")
        report = curator.run()
        self.assertEqual(report["published"], 1)
        self.assertNotIn("PRIVATE_CONTACT_ONLY_X", str(client.complete.call_args_list))
        self.assertEqual(self.registry.state("bilibili", "100", URL), "published")

    def test_disable_during_analysis_stops_before_generation_and_send(self):
        active = [True]
        curator, client, publish = self.make_curator(permitted=lambda: active[0])
        evidence = curator.tools.execute.return_value
        def analyze(*args):
            active[0] = False
            return evidence
        curator.tools.execute.side_effect = analyze
        curator.run()
        client.complete.assert_not_called()
        publish.assert_not_called()

    def test_uncertain_publish_is_durable_and_never_automatically_retried(self):
        curator, client, publish = self.make_curator()
        publish.side_effect = TimeoutError()
        report = curator.run()
        self.assertEqual(report["records"][0]["publication"], "unknown_no_retry")
        self.assertEqual(self.registry.state("bilibili", "100", URL), "unknown")
        self.assertFalse(self.registry.claim("bilibili", "100", URL, "重复", cooldown=0))

    def test_comment_failure_does_not_prevent_the_remaining_video_rounds(self):
        curator, client, publish = self.make_curator()
        curator.policy["max_videos_per_run"] = 2
        curator.browser.recommend.side_effect = lambda: dict(candidates=[
            dict(url=url, title="二创", visited=url in curator.browser.visited) for url in (URL, OTHER)])
        client.complete_json.side_effect = [dict(url=URL), dict(url=OTHER)]
        publish.side_effect = TimeoutError()
        report = curator.run()
        self.assertEqual(report["opened"], 2)
        self.assertEqual(report["published"], 0)
        publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()

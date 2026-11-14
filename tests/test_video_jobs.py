import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from social_bot.video_jobs import VideoCommand, VideoJobs, video_command
from social_bot.settings import AppSettings, RuntimeSettings, ScopeSettings, DeepSeekSettings, WeChatSettings
from social_bot.service import BotService
from social_bot.deepseek import DeepSeekClient, DeepSeekError


class VideoCommandTests(unittest.TestCase):
    def test_modes_are_bound_by_the_explicit_message(self):
        for text,mode in [('去B站刷两条视频，@主人，不监视评论区','share'),
                          ('去B站刷两条视频，监视模式，不@主人，监视评论区两小时','watch')]:
            command=video_command(text)
            self.assertEqual((command.rounds,command.mode),(2,mode))

    def test_explicit_commands_bind_platform_default_five_and_literal_topics(self):
        for text, platform in [("去B站刷视频，想看二创", "bilibili"),
                               ("在抖音刷五轮视频，看看小短剧", "douyin"),
                               ("帮我刷B站视频", "bilibili")]:
            command = video_command(text)
            self.assertEqual((command.platform, command.rounds), (platform, 5))
        self.assertEqual(video_command("刷视频", "douyin").platform, "douyin")
        command = video_command("[刷视频]\n平台：B站\n轮数：五轮\n话题：二创、小短剧")
        self.assertEqual(command.topics, ("二创", "小短剧"))
        self.assertFalse(video_command("B站刷视频，只看不评论").post_comments)

    def test_questions_negation_examples_and_public_comments_never_start_jobs(self):
        for text in ["不要刷视频", "怎么刷视频", "刷视频功能如何实现", "他说：刷视频", '"刷视频"',
                     "如果有人发刷视频就启动", "B站和抖音刷视频"]:
            self.assertIsNone(video_command(text), text)
        self.assertIsNone(video_command("刷视频", "bilibili", "group"))
        self.assertEqual(video_command("刷视频进度").action, "status")


class VideoJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.alive = {424242}
        self.launch = Mock(return_value=SimpleNamespace(pid=424242))
        for platform in ("bilibili", "douyin"):
            (self.root / platform).mkdir()
            config = dict(base_config="../config.json", memory_dir=str(self.root),
                          credentials_file="credentials.json", enabled=True,
                          video_social=dict(enabled=True, interests=["二创"], post_comments=True,
                              comment_preset=dict(enabled=True,owner_uid='123',owner_name='主人账号')))
            (self.root / platform / "config.json").write_text(json.dumps(config), encoding="utf-8")
        self.jobs = VideoJobs(self.root, root=self.root, launch=self.launch, alive=lambda pid: pid in self.alive)

    def tearDown(self):
        self.jobs.close()
        self.temp.cleanup()

    def start(self, key="one", owner="chat-a", platform="bilibili", topics=("二创",)):
        return self.jobs.start(VideoCommand("start", platform), owner=owner, event_key=key, topics=topics)

    def test_duplicate_messages_and_parallel_platform_jobs_are_bounded(self):
        first = self.start()
        duplicate = self.start()
        self.assertEqual(first["job_id"], duplicate["job_id"])
        self.assertTrue(duplicate["cached"])
        self.assertFalse(self.start(key="two", owner="chat-b")["success"])
        self.launch.assert_called_once()
        self.assertEqual(self.jobs.status("chat-b")["state"], "no_job")

    def test_topics_are_data_and_argv_never_uses_a_shell(self):
        topic = '二创"; $(should-not-execute)'
        result = self.start(topics=[topic])
        args, kwargs = self.launch.call_args
        self.assertNotIn(topic, args[0])
        self.assertNotIn("shell", kwargs)
        config = json.loads((self.jobs.directory / (result["job_id"] + ".config.json")).read_text(encoding="utf-8"))
        self.assertEqual(config["video_social"]["interests"], [topic])
        self.assertEqual(config["video_social"]["max_videos_per_run"], 5)
        self.assertEqual(config["video_social"]["watch_after_seconds"], 0)
        self.assertIn("--control-config", args[0])
        self.assertEqual(config['video_social']['max_comments_per_run'],5)
        self.assertTrue(config['video_social']['comment_preset']['enabled'])
        self.assertFalse(config['comments_enabled'])
        self.assertFalse(config['comment_replies_enabled'])

    def test_watch_job_has_no_mentions_or_private_chat_and_two_hour_followup(self):
        result=self.jobs.start(VideoCommand('start','bilibili',rounds=2,mode='watch'),
            owner='chat-a',event_key='watch',topics=[])
        config=json.loads((self.jobs.directory/(result['job_id']+'.config.json')).read_text(encoding='utf-8'))
        self.assertEqual(config['video_social']['interaction_mode'],'watch')
        self.assertEqual(config['video_social']['watch_after_seconds'],7200)
        self.assertEqual(config['video_social']['max_comments_per_run'],2)
        self.assertFalse(config['video_social']['comment_preset']['enabled'])
        self.assertFalse(config['comments_enabled'])
        self.assertTrue(config['comment_replies_enabled'])
        self.assertEqual(config['peers'],[])
        self.assertEqual(config['private_chat_mode'],'allowlist')

    def test_process_completion_is_reported_with_actual_counts(self):
        result = self.start()
        self.alive.clear()
        (self.jobs.directory / (result["job_id"] + ".result.json")).write_text(
            json.dumps(dict(opened=5, published=1)), encoding="utf-8")
        status = self.jobs.status("chat-a")
        self.assertEqual((status["state"], status["opened"], status["published"]), ("completed", 5, 1))
        self.assertEqual(self.jobs.status("chat-b")["state"], "no_job")

    def test_crash_during_two_hour_watch_does_not_claim_completion(self):
        result=self.start()
        self.alive.clear()
        (self.jobs.directory/(result['job_id']+'.result.json')).write_text(
            json.dumps(dict(opened=2,published=2,job_phase='watching')),encoding='utf-8')
        self.assertEqual(self.jobs.status('chat-a')['state'],'unknown')

    def test_crash_after_progress_report_does_not_claim_completed(self):
        result=self.start()
        self.alive.clear()
        (self.jobs.directory/(result['job_id']+'.result.json')).write_text(
            json.dumps(dict(success=True,opened=1,attempted=0,job_phase='running')),encoding='utf-8')
        status=self.jobs.status('chat-a')
        self.assertEqual(status['state'],'unknown')
        self.assertEqual(status['attempted'],0)

    def test_uncertain_crash_survives_restart_and_prevents_automatic_second_start(self):
        result = self.start()
        self.alive.clear()
        self.assertEqual(self.jobs.status("chat-a")["state"], "unknown")
        self.jobs.close()
        self.jobs = VideoJobs(self.root, root=self.root, launch=self.launch, alive=lambda pid: False)
        self.assertFalse(self.start(key="new")["success"])
        self.launch.assert_called_once()

    def test_disabled_platform_does_not_spawn(self):
        path = self.root / "bilibili" / "config.json"
        config = json.loads(path.read_text()); config["enabled"] = False
        path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.start()["reason"], "video_platform_not_enabled")
        self.launch.assert_not_called()

    def test_douyin_bootstrap_cookies_are_not_exposed_and_are_cleaned_after_exit(self):
        secret = "fixture-private-session"
        result = self.jobs.start(VideoCommand("start", "douyin"), owner="chat-a", event_key="one", topics=[],
            cookies=[dict(name="sessionid", value=secret, domain=".douyin.com", path="/")])
        cookie = self.jobs.directory / (result["job_id"] + ".cookies.json")
        self.assertTrue(cookie.exists())
        self.assertNotIn(secret, str(result))
        self.assertNotIn(secret, str(self.launch.call_args))
        self.alive.clear()
        self.jobs.status("chat-a")
        self.assertFalse(cookie.exists())


class ChatIntegrationTests(unittest.TestCase):
    def test_four_private_chat_routes_force_the_tool_and_do_not_wait_for_video(self):
        class Client:
            def complete_with_tools(self, messages, definitions, execute, **kwargs):
                self.preferred = kwargs["tool_choice"]["function"]["name"]
                self.receipt = execute(self.preferred, {"topics": ["小短剧"], "confirm": True})
                return SimpleNamespace(content="已启动，最多刷五条后退出。", tool_calls=1, message_sent=False)
        with tempfile.TemporaryDirectory() as folder:
            settings = AppSettings(api_key="fixture", deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
                scope=ScopeSettings(private_users=frozenset({"friend"}), group_activation_enabled=False),
                runtime=RuntimeSettings(database_path=str(Path(folder) / "chat.sqlite3"),
                                        cooldown_seconds=0, tool_calling_enabled=True))
            client = Client()
            service = BotService(settings, client)
            manager = Mock()
            manager.start.return_value = dict(success=True, state="running", background=True)
            service._video_jobs = manager
            try:
                for platform in ("wechat", "qq", "bilibili", "douyin"):
                    result = service.handle(dict(platform=platform, chat_type="private", sender_id="friend",
                        chat_id="friend", message_id="start-" + platform, text="去B站刷视频，想看小短剧"))
                    self.assertTrue(result["accepted"])
                    self.assertEqual(client.preferred, "video_browse_share")
                self.assertEqual(manager.start.call_count, 4)
                self.assertEqual(manager.start.call_args.args[0].rounds, 5)
                denied = service.handle(dict(platform="wechat", chat_type="private", sender_id="stranger",
                    chat_id="stranger", message_id="denied", text="刷视频"))
                self.assertFalse(denied["accepted"])
                self.assertEqual(manager.start.call_count, 4)
            finally:
                service.close()

    def test_structured_selection_uses_task_json_without_chat_rewrite(self):
        client = DeepSeekClient("fixture", DeepSeekSettings(collaboration_enabled=True, task_model="deepseek-flash"))
        client._request = Mock(return_value=dict(choices=[dict(message=dict(content='{"url":null}'), finish_reason="stop")]))
        client._final_reply = Mock()
        result = client.complete_json([dict(role="user", content="输出JSON")])
        self.assertEqual(result, dict(url=None))
        payload = client._request.call_args.args[0]
        self.assertEqual(payload["response_format"], dict(type="json_object"))
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["thinking"], dict(type="disabled"))
        client._final_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()

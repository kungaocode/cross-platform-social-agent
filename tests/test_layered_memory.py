import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from social_bot.memory import strength
from social_bot.service import BotService
from social_bot.settings import AppSettings, ScopeSettings, RuntimeSettings, DeepSeekSettings, WeChatSettings
from social_bot.store import ConversationStore


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = ConversationStore(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_tiers_repetition_decay_and_authorship(self):
        for i in range(3):
            self.store.add('wechat:group:g', 'user', '周六一起跑步', str(i), sender_id='a')
        self.store.add('wechat:group:g', 'user', '我喜欢茶', 'p', sender_id='b')
        self.store.add('wechat:group:g', 'user', '请记住：叫我阿甲', 'c', sender_id='a')
        self.store.add('wechat:group:g', 'assistant', '请记住：虚构内容')
        self.assertEqual(self.store.memory_stats()['tiers'], {'long': 2, 'core': 1})
        items = self.store.recall('wechat:group:g', '喜欢什么')
        self.assertEqual(next(x for x in items if x['content'] == '我喜欢茶')['sender_id'], 'b')
        self.assertEqual(self.store.recall('qq:group:g', '茶'), [])
        self.assertAlmostEqual(strength('recent', 1, 1, 3), .5)
        self.assertAlmostEqual(strength('long', 1, 1, 90), 1)
        self.assertEqual(strength('core', 1, 1, 999), 4)

    def test_scoped_dedup_and_no_false_reinforcement(self):
        self.assertTrue(self.store.add('s1', 'user', 'abc', 'same'))
        self.assertFalse(self.store.add('s1', 'user', 'abc', 'same'))
        self.assertTrue(self.store.add('s2', 'user', 'abc', 'same'))
        self.assertEqual(self.store.connection.execute('SELECT SUM(repeats) FROM memories').fetchone()[0], 2)

    def test_crash_after_durable_enqueue_replays_on_restart(self):
        code = ('from social_bot.store import ConversationStore; import os; '
                f's=ConversationStore({str(self.path)!r}); '
                's.enqueue("s", "user", "crash-safe", "crash"); os._exit(0)')
        subprocess.run([sys.executable, '-c', code], check=True)
        other = ConversationStore(self.path)
        try:
            self.assertTrue(other.has_message('crash'))
            self.assertEqual(other.memory_stats()['inbox'], {'done': 1})
        finally:
            other.close()

    def test_failed_transaction_keeps_event_and_retry_is_atomic(self):
        original = self.store._consolidate
        self.store._consolidate = lambda *args: (_ for _ in ()).throw(RuntimeError('injected failure'))
        self.store.add('s', 'user', 'recover me', 'f')
        self.assertFalse(self.store.has_message('f'))
        self.assertEqual(self.store.memory_stats()['inbox'], {'failed': 1})
        self.store._consolidate = original
        self.store.retry_failed()
        self.assertTrue(self.store.has_message('f'))
        self.assertEqual(self.store.memory_stats()['tiers'], {'recent': 1})

    def test_independent_connections_concurrent_intake(self):
        def worker(index):
            store = ConversationStore(self.path)
            try:
                for i in range(20):
                    store.add('s', 'user', f'{index}-{i}', f'{index}-{i}')
                    store.add('s', 'user', 'duplicate', 'shared')
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(worker, range(6)))
        self.assertEqual(self.store.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 121)
        self.assertEqual(self.store.memory_stats()['inbox'], {'done': 121})

    def test_forget_cannot_replay_or_recall(self):
        self.store.add('s', 'user', '请记住：secret', 'x')
        self.store.enqueue('s', 'user', 'pending', 'y')
        self.store.clear('s')
        self.store.drain()
        self.assertEqual(self.store.history('s', 20), [])
        self.assertEqual(self.store.recall('s', 'secret'), [])
        self.assertEqual(self.store.memory_stats(), {'tiers': {}, 'inbox': {}})

    def test_model_failure_keeps_input(self):
        class Broken:
            def complete(self, messages):
                raise RuntimeError('provider down')
        settings = AppSettings(api_key='unused', deepseek=DeepSeekSettings(), wechat=WeChatSettings(), scope=ScopeSettings(private_users=frozenset({'u'})),
                               runtime=RuntimeSettings(database_path=str(self.path), cooldown_seconds=0))
        bot = BotService(settings, Broken())
        try:
            result = bot.handle(dict(platform='qq', chat_type='private', sender_id='u', chat_id='u',
                                     text='请记住：我喜欢蓝色', message_id='failure'))
            self.assertEqual(result['reason'], 'model_error')
            self.assertTrue(bot.store.has_message('failure'))
            self.assertEqual(bot.store.memory_stats()['tiers'], {'core': 1})
        finally:
            bot.close()

    def test_old_long_term_memory_reaches_model_beyond_recent_history(self):
        class Client:
            def complete(self, messages):
                self.messages = messages
                return 'ok'
        client = Client()
        settings = AppSettings(api_key='unused', deepseek=DeepSeekSettings(), wechat=WeChatSettings(), scope=ScopeSettings(private_users=frozenset({'u'})),
                               runtime=RuntimeSettings(database_path=str(self.path), cooldown_seconds=0, max_context_messages=2))
        bot = BotService(settings, client)
        try:
            bot.store.add('qq:private:u', 'user', '我喜欢乌龙茶', 'old', sender_id='u')
            for i in range(10):
                bot.store.add('qq:private:u', 'user', f'闲聊{i}', f'chat-{i}')
            bot.handle(dict(platform='qq', chat_type='private', sender_id='u', chat_id='u', text='我喜欢什么茶', message_id='q'))
            self.assertIn('我喜欢乌龙茶', json.dumps(client.messages, ensure_ascii=False))
        finally:
            bot.close()

    def test_old_history_expires_from_recent_window_but_long_memory_remains(self):
        self.store.add('s', 'user', '我喜欢乌龙茶', 'old', sender_id='u')
        with self.store.connection:
            self.store.connection.execute("UPDATE messages SET created_at=datetime('now','-8 days')")
        self.assertEqual(self.store.history('s', 12), [])
        self.assertIn('乌龙茶', self.store.recall('s', '喜欢什么茶')[0]['content'])

    def test_qq_archive_does_not_call_model_or_reply(self):
        from social_bot.platforms.qq.worker import QQWorker
        class Never:
            def complete(self, messages):
                raise AssertionError('archive must not request model')
        settings = AppSettings(api_key='unused', deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
            scope=ScopeSettings(private_users=frozenset({'u'})),
            runtime=RuntimeSettings(database_path=str(self.path)))
        bot = BotService(settings, Never())
        worker = QQWorker(bot, '123')
        try:
            event = dict(op='archive', message=dict(kind='c2c', chat_id='u', sender_id='u',
                         message_id='overflow', text='请记住：入库池测试'))
            self.assertTrue(worker.handle(event)['archived'])
            self.assertTrue(worker.handle(event)['archived'])
            self.assertEqual(bot.store.memory_stats()['tiers'], {'core': 1})
            self.assertEqual(len(bot.store.history('qq:private:u', 20)), 1)
        finally:
            bot.close()

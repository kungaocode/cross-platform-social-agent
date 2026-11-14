import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from social_bot.platforms.douyin.adapter import (BridgeError, ChatLoop, DOMChat, DouyinScope,
                                                 fresh_by_signatures, fresh_messages, build_service, load_config)
from social_bot.service import BotService
from social_bot.settings import AppSettings, DeepSeekSettings, ScopeSettings, RuntimeSettings, WeChatSettings


def message(text, mid='', own=False):
    return dict(id=mid, text=text, self=own, type='text')


class NewMessagesTests(unittest.TestCase):
    def test_baseline_and_lost_anchor_never_replay_history(self):
        self.assertEqual(fresh_messages(None, [message('旧消息')]), [])
        self.assertEqual(fresh_messages([message('a')], [message('b')]), [])

    def test_empty_initialized_chat_accepts_first_incoming(self):
        self.assertEqual(fresh_messages([], [message('第一条')]), [message('第一条')])

    def test_new_text_and_self_filter(self):
        old = [message('old', '1')]
        new = old + [message('hi', '2'), message('mine', '3', True)]
        self.assertEqual(fresh_messages(old, new), [new[1]])

    def test_repeated_identical_without_id_counts_each_new_bubble(self):
        old = [message('你好')]
        self.assertEqual(len(fresh_messages(old, old * 2)), 1)
        old = [message('你好', '1')]
        self.assertEqual(len(fresh_messages(old, old + [message('你好', '2')])), 1)

    def test_sliding_window_keeps_retained_anchor(self):
        old = [message('a'), message('b')]
        self.assertEqual([m['text'] for m in fresh_messages(old, [message('b'), message('c')])], ['c'])

    def test_no_shared_anchor_rebuilds_baseline(self):
        old = [message('a'), message('b')]
        self.assertEqual(fresh_messages(old, [message('x'), message('y')]), [])

    def test_mid_list_change_uses_anchor(self):
        old = [message('a'), message('b')]
        self.assertEqual([m['text'] for m in fresh_messages(old, [message('x'), message('b'), message('c')])],
                         ['c'])

    def test_unaligned_signatures_signal_keep_old_baseline(self):
        from social_bot.platforms.douyin.adapter import signature
        previous = [signature(message('a')), signature(message('b'))]
        self.assertIsNone(fresh_by_signatures(previous, [message('x'), message('y')]))
        self.assertEqual(fresh_by_signatures(previous, [message('a'), message('b'), message('c')]),
                         [message('c')])


class ScopeSettingsTests(unittest.TestCase):
    def test_private_all_allowed_but_group_needs_activation_and_name(self):
        scope = DouyinScope(ScopeSettings(group_activation_enabled=True), ['测试群'])
        self.assertTrue(scope.check(dict(chat_type='private', sender_id='陌生人', text='你好')).allowed)
        self.assertEqual(scope.check(dict(chat_type='group', chat_id='测试群', text='随便说')).reason,
                         'group_mention_required')
        self.assertTrue(scope.check(dict(chat_type='group', chat_id='测试群', text='鲲 你好',
                                         is_mentioned=True)).allowed)
        self.assertFalse(scope.group_is_allowed('别的群'))


class ConfigTests(unittest.TestCase):
    def write(self, temp, **overrides):
        payload = dict(base_config='base.json', memory_dir=temp, account_id='default',
                       browser_channel='msedge', peers=[], all_peers=True, group_names=['测试群'],
                       bot_name='鲲', max_peers=5, enabled=True, poll_seconds=20)
        payload.update(overrides)
        path = Path(temp) / 'config.json'
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    def test_all_peers_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            config = load_config(self.write(temp))
            self.assertTrue(config['all_peers'])
            self.assertEqual(config['group_names'], ['测试群'])
            self.assertEqual((config['bot_name'], config['max_peers']), ('鲲', 5))

    def test_invalid_values_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for overrides in ({'all_peers': 'yes'}, {'max_peers': 0}, {'max_peers': 51},
                              {'group_names': ['同一群', '同一群']}, {'bot_name': 5},
                              {'group_names': ['']}, {'message_detection':'unknown'},
                              {'full_scan_seconds':True}, {'full_scan_seconds':0}):
                with self.assertRaises(ValueError):
                    load_config(self.write(temp, **overrides))


class AllPeersGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = Mock()
        self.client.complete.return_value = '答复'
        settings = AppSettings(api_key='fake', deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
            scope=ScopeSettings(group_activation_enabled=True, group_activate_keyword='转肥鱼',
                                group_activate_reply='大肥鱼收到', group_deactivate_keyword='转人工',
                                group_deactivate_reply='已转人工'),
            runtime=RuntimeSettings(database_path=str(Path(self.temp.name)/'test.db'), cooldown_seconds=0))
        self.service = BotService(settings, self.client)
        self.messages = {'测试群': [message('旧消息', '1')], '朋友': [message('旧消息', '2')]}
        self.dom = Mock()
        self.dom.names.return_value = ['测试群', '朋友']
        self.dom.read.side_effect = lambda peer: list(self.messages[peer])
        self.loop = ChatLoop(self.service, self.dom, [], all_peers=True,
                             group_names=['测试群'], bot_name='鲲')
        self.loop.step()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def push(self, peer, text, mid):
        self.messages[peer].append(message(text, mid))

    def attention(self):
        self.now = [0]
        self.loop.clock = lambda: self.now[0]
        self.rows = [dict(name='测试群', active=False, unread=False, hint='旧消息'),
                     dict(name='朋友', active=True, unread=False, hint='旧消息')]
        self.dom.conversation_states.return_value = self.rows
        self.loop.last_scan.clear()
        self.loop.step()
        self.dom.select.reset_mock()
        self.dom.read.reset_mock(side_effect=False)

    def test_idle_only_checks_open_chat_without_switching_other_chats(self):
        self.attention()
        self.loop.step()
        self.assertEqual([c.args[0] for c in self.dom.select.call_args_list], ['朋友'])

    def test_unread_is_processed_before_open_chat_and_not_replied_twice(self):
        self.attention()
        self.rows[0]['unread'] = True
        self.push('测试群', '转肥鱼', 'unread-1')
        self.loop.step()
        self.assertEqual([c.args[0] for c in self.dom.select.call_args_list], ['测试群','朋友'])
        self.dom.send.assert_called_once_with('测试群','大肥鱼收到')
        self.loop.step()
        self.dom.send.assert_called_once()

    def test_active_chat_new_message_is_seen_even_without_unread_badge(self):
        self.attention()
        self.push('朋友','打开中收到的新消息','active-1')
        self.loop.step()
        self.dom.send.assert_called_once_with('朋友','答复')

    def test_preview_change_detects_message_when_badge_is_missing(self):
        self.attention()
        self.rows[0]['active'], self.rows[1]['active'] = True, False
        self.rows[1]['hint'] = '新消息'
        self.push('朋友','新消息','preview-1')
        self.loop.step()
        self.dom.send.assert_called_once_with('朋友','答复')

    def test_periodic_scan_catches_unread_false_negative_and_identical_preview(self):
        self.attention()
        self.rows[0]['active'], self.rows[1]['active'] = True, False
        self.push('朋友','旧消息','no-badge-1')
        self.loop.step()
        self.dom.send.assert_not_called()
        self.now[0] = 61
        self.loop.step()
        self.dom.send.assert_called_once_with('朋友','答复')

    def test_failed_read_is_retried_after_click_has_cleared_the_badge(self):
        self.attention()
        self.rows[0]['unread'] = True
        self.dom.select.side_effect = BridgeError('页面重绘')
        with self.assertRaises(BridgeError):self.loop.step()
        self.rows[0]['unread'] = False
        self.dom.select.side_effect = None
        self.dom.select.reset_mock()
        self.loop.step()
        self.assertIn('测试群',[c.args[0] for c in self.dom.select.call_args_list])

    def test_every_visible_conversation_is_polled(self):
        called = [call.args[0] for call in self.dom.select.call_args_list]
        self.assertEqual(called[:2], ['测试群', '朋友'])
        self.push('朋友', '在吗', '3')
        self.loop.step()
        self.dom.send.assert_called_once_with('朋友', '答复')
        self.assertIn('douyin:private:朋友',
                      [row[0] for row in self.loop.db.execute('SELECT DISTINCT session_id FROM messages')])

    def test_group_silent_until_activation_word(self):
        self.push('测试群', '随便聊聊', '4')
        self.loop.step()
        self.dom.send.assert_not_called()
        self.client.complete.assert_not_called()

    def test_activation_then_bot_name_triggers_and_stop_word_disables(self):
        self.push('测试群', '转肥鱼', '5')
        self.loop.step()
        self.dom.send.assert_called_once_with('测试群', '大肥鱼收到')

        self.dom.send.reset_mock()
        self.client.complete.reset_mock()
        self.push('测试群', '今天吃什么', '6')
        self.loop.step()
        self.dom.send.assert_not_called()
        self.assertEqual(self.client.complete.call_count, 0)

        self.push('测试群', '鲲 今天吃什么', '7')
        self.loop.step()
        self.dom.send.assert_called_once_with('测试群', '答复')
        self.assertEqual(self.client.complete.call_count, 1)

        self.dom.send.reset_mock()
        self.push('测试群', '转人工', '8')
        self.loop.step()
        self.dom.send.assert_called_once_with('测试群', '已转人工')
        self.push('测试群', '鲲 还在吗', '9')
        self.loop.step()
        self.dom.send.assert_called_once()

    def test_group_messages_keep_sender_separate(self):
        self.messages['测试群'].append(dict(message('转肥鱼', '10'), sender='甲'))
        self.messages['测试群'].append(dict(message('鲲 第一条', '11'), sender='乙'))
        self.messages['测试群'].append(dict(message('鲲 第二条', '12'), sender='丙'))
        self.loop.step()
        rows = self.loop.db.execute(
            "SELECT sender_id FROM messages WHERE session_id='douyin:group:测试群' AND role='user' "
            "ORDER BY rowid").fetchall()
        self.assertEqual([row[0] for row in rows], ['乙', '丙'])

    def test_group_consecutive_messages_inherit_sender(self):
        self.messages['测试群'].append(dict(message('转肥鱼', '20'), sender='甲'))
        self.messages['测试群'].append(dict(message('鲲 第一句', '21'), sender='甲'))
        self.messages['测试群'].append(dict(message('鲲 第二句', '22'), sender=''))
        self.loop.step()
        rows = self.loop.db.execute(
            "SELECT sender_id FROM messages WHERE session_id='douyin:group:测试群' AND role='user' "
            "ORDER BY rowid").fetchall()
        self.assertEqual([row[0] for row in rows], ['甲', '甲'])


class EventScopeFallbackTests(unittest.TestCase):
    def test_comment_scope_keeps_private_allowed_and_group_verified_only(self):
        from social_bot.platforms.common.chat import EventScope
        inner = DouyinScope(ScopeSettings(), [])
        scope = EventScope(inner.settings, fallback=inner)
        self.assertTrue(scope.check(dict(chat_type='private', sender_id='陌生人', text='你好')).allowed)
        self.assertEqual(scope.check(dict(chat_type='group', chat_id='测试群', text='hi')).reason,
                         'unverified_comment')


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = Mock()
        self.client.complete.return_value = '答复'
        settings = AppSettings(api_key='fake', deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
            scope=ScopeSettings(private_users=frozenset({'朋友'})),
            runtime=RuntimeSettings(database_path=str(Path(self.temp.name)/'test.db'), cooldown_seconds=0))
        self.service = BotService(settings, self.client)
        self.dom = Mock()
        self.loop = ChatLoop(self.service, self.dom, ['朋友'])
        self.dom.read.return_value = [message('旧消息', '1')]
        self.loop.step()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def incoming(self):
        self.dom.read.return_value = [message('旧消息', '1'), message('新消息', '2')]

    def test_new_message_once(self):
        self.dom.send.assert_not_called()
        self.incoming()
        self.loop.step()
        self.loop.step()
        self.dom.send.assert_called_once_with('朋友', '答复')
        self.client.complete.assert_called_once()

    def test_identical_text_without_dom_ids_replies_to_each_bubble(self):
        self.dom.read.return_value = [message('旧消息', '1'), message('同一句')]
        self.loop.step()
        self.dom.read.return_value += [message('同一句')]
        self.loop.step()
        self.assertEqual(self.dom.send.call_count, 2)

    def test_unsent_later_message_survives_interrupted_batch_and_restart(self):
        self.dom.read.return_value = [message('旧消息', '1'), message('第一条', '2'), message('第二条', '3')]
        self.dom.send.side_effect = BridgeError('结果未知')
        with self.assertRaises(BridgeError):
            self.loop.step()
        self.dom.send.side_effect = None
        reopened = ChatLoop(self.service, self.dom, ['朋友'])
        reopened.step()
        states = [r[0] for r in reopened.db.execute('SELECT state FROM douyin_deliveries ORDER BY rowid')]
        self.assertEqual(states, ['unknown', 'sent'])
        self.assertEqual(self.dom.send.call_count, 2)

    def test_unknown_delivery_not_retried(self):
        self.incoming()
        self.dom.send.side_effect = BridgeError('unknown')
        with self.assertRaises(BridgeError):
            self.loop.step()
        self.assertEqual(self.loop.db.execute('SELECT state FROM douyin_deliveries').fetchone()[0], 'unknown')
        self.loop.step()
        self.dom.send.assert_called_once()

    def test_disable_during_model_prevents_send(self):
        self.incoming()
        self.loop.permitted = Mock(side_effect=[True, False])
        with self.assertRaises(BridgeError):
            self.loop.step()
        self.dom.send.assert_not_called()

    def test_wrong_chat_prevents_model(self):
        self.incoming()
        self.dom.select.side_effect = BridgeError('wrong chat')
        with self.assertRaises(BridgeError):
            self.loop.step()
        self.client.complete.assert_not_called()


if __name__ == '__main__':
    unittest.main()

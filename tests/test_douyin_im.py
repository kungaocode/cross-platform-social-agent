import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from social_bot.platforms.douyin.im_protocol import fields, conversation_rows, send_receipt
from social_bot.platforms.douyin.api_setup import security_material, bind_peer, save_credentials, setup_session
from social_bot.platforms.douyin.api_inbox import APIInboxPump, push_event
from social_bot.platforms.douyin.backend import DouyinAPIError
from social_bot.platforms.douyin.backend_worker import Worker, BackendFault


def number(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def scalar(key, value):
    return number(key << 3)+number(value)


def blob(key, value):
    if isinstance(value, str):
        value = value.encode()
    return number((key << 3) | 2)+number(len(value))+value


PEER = dict(uid='2', conversation_id='0:1:1:2', short_id='8', ticket='test-ticket')


class WireTests(unittest.TestCase):
    def test_discovers_nested_existing_private_conversations(self):
        conv = blob(1, PEER['conversation_id'])+scalar(2, 8)+scalar(3, 1)+blob(4, 'test-ticket')
        raw = blob(6, blob(610, blob(1, conv)))
        self.assertEqual(conversation_rows(raw, '1'), [PEER])
        self.assertEqual(conversation_rows(raw, '9'), [])
        self.assertEqual(conversation_rows(blob(6, conv.replace(scalar(3, 1), scalar(3, 2))), '1'), [])

    def test_truncated_oversized_and_duplicate_receipts_fail(self):
        for raw in [b'\x00', b'\x0a\x04a', b'\x80'*11, b'x'*2_000_001]:
            with self.assertRaises(ValueError):
                fields(raw)
        for raw in [b'', scalar(1, 100)+scalar(1, 100), scalar(1, 100)+scalar(3, 1)]:
            with self.assertRaises(ValueError):
                send_receipt(raw)

    def test_send_requires_server_id_and_successful_business_receipt(self):
        def receipt(inner):
            return scalar(1, 100)+blob(6, blob(100, inner))
        self.assertEqual(send_receipt(receipt(scalar(1, 99))),
                         dict(accepted_by_server=True, server_message_id='99'))
        for inner in [b'', scalar(1, 0), scalar(1, 99)+scalar(3, 1),
                      scalar(1, 99)+scalar(5, 1), scalar(1, 99)+scalar(8, 1)]:
            with self.assertRaises(ValueError):
                send_receipt(receipt(inner))

    def test_plain_ok_string_is_not_a_send_receipt(self):
        with self.assertRaises(ValueError):
            send_receipt(scalar(1, 100)+blob(4, 'OK'))


class SetupTests(unittest.TestCase):
    def test_setup_verifies_before_replacing_and_does_not_auto_bind_candidates(self):
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            path = Path(temp)/'credentials.json'
            original = dict(uid='1', cookie='sessionid=old', peers=[])
            snapshot = json.dumps(original).encode()
            path.write_bytes(snapshot)
            (Path(temp)/'douyin-profile-default').mkdir()
            context, page, pw = Mock(), Mock(), Mock()
            context.pages = [page]
            context.__enter__ = Mock(return_value=context)
            context.__exit__ = Mock(return_value=False)
            pw.chromium.launch_persistent_context.return_value = context
            context.cookies.return_value = [dict(name='sessionid', value='fresh')]
            storage = {'security-sdk/s_sdk_sign_data_key/web_protect': json.dumps(dict(
                data=dict(ticket='new-ticket', ts_sign='new-sign'))),
                'security-sdk/s_sdk_crypt_sdk': json.dumps(dict(data=dict(ec_privateKey='new-key')))}
            page.evaluate.side_effect = [storage, dict(ua='Mozilla/5.0 Chrome/155.0')]
            conv = blob(1, PEER['conversation_id'])+scalar(2, 8)+scalar(3, 1)+blob(4, 'test-ticket')
            def navigate(*args, **kwargs):
                callback = context.on.call_args.args[1]
                callback(Mock(url='https://imapi.douyin.com/v2/conversation/get_info_list',
                              body=Mock(return_value=blob(6, blob(610, blob(1, conv))))))
            page.goto.side_effect = navigate
            client = Mock(uid='1')
            client.call.return_value = dict(write_session_matches=True)
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=False)
            stack.enter_context(patch('social_bot.platforms.douyin.api_setup.SpiderClient', return_value=client))
            manager = stack.enter_context(patch('playwright.sync_api.sync_playwright'))
            manager.return_value.__enter__.return_value = pw
            result = setup_session(dict(memory_dir=temp, account_id='default', browser_channel='msedge'),
                                   dict(credentials=str(path)), seconds=0)
            saved = json.loads(path.read_text())
            self.assertEqual(saved['peers'], [])
            self.assertEqual(saved['discovered_peers'], [PEER])
            self.assertEqual(saved['cookie'], 'sessionid=fresh')
            self.assertEqual(result['messages_sent'], 0)
            self.assertEqual(len(list(Path(temp).glob('.douyin-*'))), 0)
            # Failed verification preserves the existing file and removes temporary secrets.
            snapshot = path.read_bytes()
            page.evaluate.side_effect = [storage, dict(ua='Mozilla/5.0 Chrome/155.0')]
            client.call.return_value = dict(write_session_matches=False)
            with self.assertRaisesRegex(DouyinAPIError, 'SESSION_MISMATCH'):
                setup_session(dict(memory_dir=temp, account_id='default', browser_channel='msedge'),
                              dict(credentials=str(path)), seconds=0)
            self.assertEqual(path.read_bytes(), snapshot)
            self.assertEqual(len(list(Path(temp).glob('.douyin-*'))), 0)

    def test_parses_current_wrapped_security_material(self):
        storage = {'security-sdk/s_sdk_sign_data_key/web_protect': json.dumps(dict(data=json.dumps(
            dict(ticket='ticket', ts_sign='ts', client_cert='cert')))),
            'security-sdk/s_sdk_crypt_sdk': json.dumps(dict(data=json.dumps(dict(ec_privateKey='key'))))}
        self.assertEqual(security_material(storage), dict(ticket='ticket', ts_sign='ts', client_cert='cert', private_key='key'))
        self.assertEqual(security_material({'security-sdk/s_sdk_crypt_sdk': 'null'}), {})
        self.assertEqual(security_material({}), {})

    def test_binding_is_explicit_and_preserves_other_material(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'credentials.json'
            original = dict(uid='1', cookie='sessionid=fake', peers=[], discovered_peers=[PEER], private_key='local-only')
            path.write_text(json.dumps(original), encoding='utf-8')
            with self.assertRaisesRegex(DouyinAPIError, 'PEER_NOT_DISCOVERED'):
                bind_peer(dict(credentials=str(path)), '9')
            self.assertEqual(json.loads(path.read_text()), original)
            result = bind_peer(dict(credentials=str(path)), '2', '测试好友')
            updated = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(result['bound_peers'], 1)
            self.assertEqual(updated['peers'], [dict(PEER, name='测试好友')])
            self.assertEqual(updated['private_key'], 'local-only')
            self.assertNotIn('ticket', result)

    def test_atomic_save_refuses_concurrently_changed_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'credentials.json'
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(DouyinAPIError, 'CREDENTIALS_CHANGED'):
                save_credentials(path, {}, b'old')
            self.assertEqual(path.read_bytes(), b'changed')
            self.assertEqual(list(Path(temp).iterdir()), [path])


class WriteBoundaryTests(unittest.TestCase):
    def worker(self):
        worker = Worker()
        worker.auth = Mock(ticket='ticket', ts_sign='sign', private_key='key')
        worker.auth.ticket_matches_session.return_value = True
        worker.writes, worker.uid, worker.peers = True, '1', {'2': PEER}
        worker.pace = Mock()
        worker.account = Mock(return_value={'uid': '1'})
        worker.send_text = Mock(return_value=dict(accepted_by_server=True, server_message_id='99'))
        return worker

    def test_sender_rejects_unbound_or_modified_target_and_blank_text(self):
        worker = self.worker()
        for peer, text in [(dict(PEER, ticket='other'), 'hi'),
                           (dict(PEER, uid='3'), 'hi'), (PEER, ''), (PEER, 'x'*1001)]:
            with self.assertRaises(BackendFault):
                worker.dispatch('send', dict(peer=peer, text=text))
        worker.send_text.assert_not_called()

    def test_sender_requires_same_login_and_write_switch(self):
        worker = self.worker()
        worker.auth.ticket_matches_session.return_value = False
        with self.assertRaisesRegex(BackendFault, 'SESSION_MISMATCH'):
            worker.dispatch('send', dict(peer=PEER, text='hi'))
        worker.writes = False
        with self.assertRaisesRegex(BackendFault, 'WRITES_DISABLED'):
            worker.dispatch('send', dict(peer=PEER, text='hi'))
        worker.send_text.assert_not_called()

    def test_success_returns_receipt_and_passes_stable_client_id(self):
        worker = self.worker()
        self.assertEqual(worker.dispatch('send', dict(peer=PEER, text='hi', client_message_id='stable')),
                         dict(accepted_by_server=True, server_message_id='99'))
        worker.send_text.assert_called_once_with(PEER, 'hi', 'stable')

    def test_nan_timestamp_is_never_accepted(self):
        row = dict(sender='2', server_message_id='99', conversation_id=PEER['conversation_id'],
            conversation_short_id='8', conversation_type=1, message_type=7,
            content=json.dumps(dict(text='hi')), create_time='nan')
        self.assertIsNone(push_event(row, '1', {'2': PEER}, time.time()-10))

    def test_real_responder_receives_generates_sends_and_deduplicates(self):
        from social_bot.service import BotService
        from social_bot.settings import AppSettings, DeepSeekSettings, ScopeSettings, RuntimeSettings, WeChatSettings
        with tempfile.TemporaryDirectory() as temp:
            settings = AppSettings(api_key='fake', deepseek=DeepSeekSettings(), wechat=WeChatSettings(),
                scope=ScopeSettings(), runtime=RuntimeSettings(database_path=str(Path(temp)/'test.db'),
                    cooldown_seconds=0, tool_calling_enabled=False))
            model = Mock()
            model.complete.return_value = '你好，收到测试消息'
            service = BotService(settings, model)
            row = dict(sender='2', server_message_id='99', conversation_id=PEER['conversation_id'],
                conversation_short_id='8', conversation_type=1, message_type=7,
                content=json.dumps(dict(text='你好')), create_time=int(time.time()))
            api = Mock(uid='1')
            api.call.side_effect = [dict(connected=True), [row, row],
                dict(accepted_by_server=True, server_message_id='100'), [row]]
            try:
                pump = APIInboxPump(api, service, {}, dict(peers=[PEER]))
                pump.step()
                pump.step()
                calls = [c for c in api.call.call_args_list if c.args[0] == 'send']
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].args[1]['peer'], PEER)
                self.assertTrue(calls[0].args[1]['client_message_id'])
                self.assertEqual(service.store.connection.execute('SELECT state FROM social_deliveries').fetchone()[0], 'sent')
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()

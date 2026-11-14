"""Private JSON-lines worker; upstream imports never enter the bot interpreter."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
from urllib.parse import urlsplit

try:
    from .im_protocol import send_receipt, fields, single
except ImportError:  # Executed directly by the isolated interpreter.
    from im_protocol import send_receipt, fields, single

PIN = 'd151f9c2980abc304c1ae4918c2272675cd9e4c2'


class BackendFault(Exception):
    pass


def classify(exc):
    if isinstance(exc, BackendFault):
        return str(exc)
    # Only fixed error codes leave the worker. Upstream exceptions include tokens/URLs.
    text = str(exc).lower()
    if 'dtrait' in text:
        return 'DOUYIN_DEVICE_MATERIAL_INVALID'
    if any(v in text for v in ('人机验证', '二次身份验证', 'captcha', 'bdturing', 'challenge')):
        return 'DOUYIN_VERIFICATION_REQUIRED'
    if any(v in text for v in ('timeout', 'timed out')):
        return 'DOUYIN_TIMEOUT'
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return 'DOUYIN_BACKEND_DEPENDENCY_MISSING'
    return 'DOUYIN_BACKEND_REQUEST_FAILED'


def response_diagnostic(method, url, headers, response):
    """Export only fixed metadata; never response text, tokens or signed URLs."""
    path = urlsplit(url).path
    path = path if re.fullmatch(r'/[a-zA-Z0-9_./-]{1,160}', path) else '/redacted'
    incoming = {k.lower():v for k,v in response.headers.items()}
    outgoing = {k.lower():v for k,v in (headers or {}).items()}
    raw = response.content
    kind = 'empty' if not raw else 'protobuf' if urlsplit(url).hostname == 'imapi.douyin.com' else 'other'
    business, identity_error = None, None
    if raw and raw[:1] in (b'{',b'[') and len(raw)<2_000_000:
        kind = 'json'
        try:
            value = json.loads(raw)
            code = value.get('status_code') if isinstance(value,dict) else None
            if re.fullmatch(r'-?[0-9]{1,10}',str(code)):
                business = int(code)
            data = value.get('data') if isinstance(value,dict) else None
            error = data.get('error_code') if isinstance(data,dict) else None
            if re.fullmatch(r'-?[0-9]{1,10}',str(error)):
                identity_error = int(error)
        except (ValueError,TypeError):
            pass
    elif raw and raw.lstrip()[:1] == b'<':
        kind = 'html'
    return dict(method=method.upper(), path=path, http_status=response.status_code, body_kind=kind,
        business_status=business, identity_error_code=identity_error,
        captcha_header=bool(incoming.get('x-vc-bdturing-parameters')),
        identity_header=bool(incoming.get('x-tt-verify-passport-decision')),
        request_guard={k:bool(outgoing.get(k)) for k in ('bd-ticket-guard-client-data','x-tt-session-dtrait','x-secsdk-csrf-token')})


def response_error(diagnostic):
    if diagnostic['body_kind'] != 'json':
        if diagnostic['captcha_header']:
            return 'DOUYIN_CAPTCHA_REQUIRED'
        if diagnostic['identity_header']:
            return 'DOUYIN_IDENTITY_VERIFICATION_REQUIRED'
    return {401:'DOUYIN_AUTH_EXPIRED',403:'DOUYIN_FORBIDDEN',412:'DOUYIN_PRECONDITION_FAILED',
            429:'DOUYIN_RATE_LIMITED'}.get(diagnostic['http_status'])


def envelope(value, required=None):
    if not isinstance(value, dict):
        raise BackendFault('DOUYIN_BAD_RESPONSE')
    status = value.get('status_code')
    if status not in (0, '0'):
        raise BackendFault('DOUYIN_API_REJECTED')
    if required and required not in value:
        raise BackendFault('DOUYIN_INCOMPLETE_RESPONSE')
    return value


def positive(value):
    text = str(value or '')
    return text if re.fullmatch(r'[1-9][0-9]{0,24}', text) else ''


def feed_envelope(value):
    value = envelope(value)
    if 'aweme_list' in value:
        if not isinstance(value['aweme_list'], list):
            raise BackendFault('DOUYIN_FEED_SCHEMA_CHANGED')
        return value
    # Measured 2026-11-13: module/feed returns cards[].aweme as a JSON string.
    cards = value.get('cards')
    if not isinstance(cards, list):
        raise BackendFault('DOUYIN_INCOMPLETE_RESPONSE')
    rows = []
    for card in cards[:100]:
        if not isinstance(card, dict) or 'aweme' not in card:
            continue
        aweme = card['aweme']
        try:
            if isinstance(aweme, str) and len(aweme) < 2_000_000:
                aweme = json.loads(aweme)
        except ValueError:
            raise BackendFault('DOUYIN_FEED_SCHEMA_CHANGED') from None
        if not isinstance(aweme, dict) or not positive(aweme.get('aweme_id')):
            raise BackendFault('DOUYIN_FEED_SCHEMA_CHANGED')
        if not aweme.get('is_ads'):
            rows.append(aweme)
    return {**value, 'aweme_list': rows}


def varint_field(data, wanted):
    """Read an unknown scalar preserved by upstream's minimal MessageBody schema."""
    offset = 0
    def number():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError('truncated protobuf')
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('oversized protobuf varint')
    while offset < len(data):
        tag = number()
        field, wire = tag >> 3, tag & 7
        if not field:
            raise ValueError('invalid protobuf field')
        if wire == 0:
            value = number()
            if field == wanted:
                return value
        elif wire == 2:
            length = number()
            offset += length
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
        else:
            raise ValueError('unsupported protobuf wire type')
        if offset > len(data):
            raise ValueError('truncated protobuf')
    return 0


class Worker:
    def __init__(self):
        self.auth = self.api = self.socket = None
        self.uid = ''
        self.inbox = queue.Queue(maxsize=500)
        self.socket_error = ''
        self.socket_detail = ''
        self.connected = threading.Event()
        self.receiver = None
        self.last_request = 0.0
        self.halted = ''
        self.writes = False
        self.diagnostics = {}
        self.peers = {}
        self.request_history = []
        self.write_attempted = False

    def initialize(self, args):
        repo = Path(args['repo']).resolve()
        # Avoid loading a different upstream checkout accidentally.
        import subprocess
        commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
            stderr=subprocess.DEVNULL, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip()
        if commit != PIN:
            raise BackendFault('DOUYIN_UPSTREAM_VERSION_MISMATCH')
        sys.path.insert(0, str(repo))
        os.chdir(repo)
        credentials = args['credentials']
        profile = credentials.get('browser_profile', {})
        if not isinstance(profile, dict):
            raise BackendFault('DOUYIN_CREDENTIALS_INVALID')
        for key, value in profile.items():
            if key in {'ua', 'browser_version', 'engine_version', 'cpu_core_num', 'device_memory',
                       'screen_width', 'screen_height', 'inner_width', 'inner_height',
                       'outer_width', 'outer_height', 'avail_width', 'avail_height',
                       'webgl_vendor', 'webgl_renderer'} and isinstance(value, (str, int)):
                os.environ['DY_FP_'+key.upper()] = str(value)
        from loguru import logger
        logger.remove()
        from dy_apis.douyin_api import DouyinAPI
        from builder.auth import DouyinAuth
        from builder.params import Params
        from utils import http_client
        # Upstream has a mutable default params={} shared by unrelated endpoints.
        original_init = Params.__init__
        Params.__init__ = lambda obj, params=None: original_init(obj, {} if params is None else dict(params))
        original_request = http_client.Session.request
        def checked_request(session, method, url, **kwargs):
            kwargs['verify'] = True
            kwargs['timeout'] = 20
            if method.upper() == 'POST' and urlsplit(url).path.rstrip('/') in ('/aweme/v1/web/comment/publish','/v1/message/send'):
                self.write_attempted = True
            response = original_request(session, method, url, **kwargs)
            diagnostic = response_diagnostic(method,url,kwargs.get('headers'),response)
            self.request_history = (self.request_history+[diagnostic])[-20:]
            code = response_error(diagnostic)
            if code:
                raise BackendFault(code)
            return response
        http_client.Session.request = checked_request
        cookie = credentials.get('cookie', '')
        if not isinstance(cookie, str) or not cookie or len(cookie) > 100000 or '\n' in cookie or '\r' in cookie:
            raise BackendFault('DOUYIN_CREDENTIALS_INVALID')
        # Build without creator bootstrap or from_cookie's implicit .env/network imports.
        auth = DouyinAuth()
        auth.perepare_auth(cookie, '', '')
        for key in ('ticket', 'ts_sign', 'client_cert', 'private_key'):
            setattr(auth, key, credentials.get(key) or None)
        auth.ensure_http_session()
        self.auth, self.api = auth, DouyinAPI
        # The subprocess accepts only the credential snapshot, never caller-supplied tickets.
        self.peers = {str(p.get('uid')): p for p in credentials.get('peers', []) if isinstance(p, dict)}
        self.writes = args.get('writes_enabled') is True
        return {'ready': True, 'commit': PIN, 'network_calls': 0}

    def pace(self):
        remaining = 3 - (time.monotonic() - self.last_request)
        if remaining > 0:
            time.sleep(remaining)
        self.last_request = time.monotonic()

    def account(self):
        uid = positive(self.api.get_my_uid(self.auth))
        if not uid:
            raise BackendFault('DOUYIN_ACCOUNT_UNCONFIRMED')
        if self.uid and uid != self.uid:
            raise BackendFault('DOUYIN_ACCOUNT_CHANGED')
        self.uid = uid
        return {'uid': uid, 'logged_in': True}

    def require_writes(self):
        if not self.writes:
            raise BackendFault('DOUYIN_API_WRITES_DISABLED')
        if any(not getattr(self.auth, k, None) for k in ('ticket', 'ts_sign', 'private_key')):
            raise BackendFault('DOUYIN_WRITE_CREDENTIALS_MISSING')
        if not self.auth.ticket_matches_session():
            raise BackendFault('DOUYIN_WRITE_SESSION_MISMATCH')

    def capabilities(self):
        missing = [k for k in ('ticket', 'ts_sign', 'private_key') if not getattr(self.auth, k, None)]
        matched = not missing and self.auth.ticket_matches_session() is True
        return dict(account_bound=bool(self.uid), writes_enabled=self.writes,
                    missing_write_material=missing, write_session_matches=matched,
                    bound_peers=len(self.peers),
                    readiness_scope='local_material_only',
                    send_cookie_present={k:bool((getattr(self.auth,'cookie',{}) or {}).get(k))
                        for k in ('UIFID','passport_mfa_token','x_tt_token','passport_csrf_token')},
                    ready_to_reply=bool(self.uid and self.writes and matched and self.peers
                                        and all(p.get('ticket') for p in self.peers.values())))

    def send_text(self, peer, text, client_message_id):
        from builder.header import HeaderBuilder, HeaderType
        from builder.proto import ProtoBuilder
        from utils.dy_util import generate_a_bogus, splice_url
        from utils import http_client
        path = '/v1/message/send'
        headers = HeaderBuilder().build(HeaderType.PROTOBUF)
        headers.set_header('referer', 'https://www.douyin.com/')
        headers.with_bd(path, self.auth)
        try:
            token, device = self.api.get_identity_security_token(self.auth)
        except Exception:
            raise BackendFault('DOUYIN_IDENTITY_TOKEN_UNAVAILABLE') from None
        request = ProtoBuilder.build_send_message_request(self.auth, peer['conversation_id'],
            int(peer['short_id']), peer['ticket'], message_type=7,
            content=dict(aweType=700, type=0, richTextInfos=[], text=text),
            client_message_id=client_message_id,
            identity_security_token=token, identity_security_device_id=device)
        params = {'msToken': self.auth.msToken}
        params['a_bogus'] = generate_a_bogus(splice_url(params), host='www.douyin.com')
        params.update(verifyFp=self.auth.cookie['s_v_web_id'], fp=self.auth.cookie['s_v_web_id'])
        response = http_client.post('https://imapi.douyin.com'+path, params=params,
            headers=headers.get(), cookies=self.auth.cookie, data=request.SerializeToString(), verify=True)
        if response.status_code != 200:
            raise BackendFault('DOUYIN_SEND_RESULT_UNCONFIRMED')
        try:
            outer = fields(response.content)
            inner = fields(single(fields(single(outer,6,b'')),100,b''))
            wire = dict(command=single(outer,1),status=single(outer,3,0),send_status=single(inner,3,0),
                check_code=single(inner,5,0),async_send=single(inner,8,0),
                server_message_id_present=type(single(inner,1)) is int and single(inner,1)>0)
            # Only scalar protocol diagnostics; never strings from the response.
            if all(type(v) in (int,bool,type(None)) for v in wire.values()):
                self.request_history.append(dict(path=path,im_receipt=wire))
        except (TypeError,ValueError):
            pass
        try:
            return send_receipt(response.content)
        except (TypeError, ValueError):
            raise BackendFault('DOUYIN_SEND_RESULT_UNCONFIRMED') from None

    def listen(self):
        if self.socket:
            return {'connected': self.connected.is_set()}
        from dy_apis.douyin_recv_msg import DouyinRecvMsg
        from static import Live_pb2, Response_pb2
        from google.protobuf.json_format import MessageToDict
        # Fail on gaps. Reconnecting without a measured catch-up cursor would lose messages.
        receiver = DouyinRecvMsg(self.auth, auto_reconnect=False)

        def on_message(ws, raw):
            try:
                if len(raw) > 2_000_000:
                    raise ValueError
                frame = Live_pb2.PushFrame()
                frame.ParseFromString(raw)
                if frame.payloadType != 'pb':
                    return
                response = Response_pb2.Response()
                response.ParseFromString(frame.payload)
                if not response.body.HasField('new_message_notify'):
                    return
                message = response.body.new_message_notify.message
                row = MessageToDict(message, preserving_proto_field_name=True)
                # static/Response.proto omits field 10; the richer upstream schema names it create_time.
                row['create_time'] = varint_field(message.SerializeToString(), 10)
                self.inbox.put_nowait(row)
            except queue.Full:
                self.socket_error = 'DOUYIN_INBOX_OVERFLOW'
                ws.close()
            except Exception:
                self.socket_error = 'DOUYIN_MESSAGE_DECODE_FAILED'
                ws.close()

        def on_error(ws, error):
            # Class names only: upstream messages can embed URLs or tokens.
            self.socket_detail = type(error).__name__
            self.socket_error = 'DOUYIN_SOCKET_FAILED'
            self.connected.clear()

        def on_close(ws, *args):
            self.connected.clear()
            if not self.socket_error:
                self.socket_error = 'DOUYIN_SOCKET_DISCONNECTED'

        receiver.on_message = on_message
        receiver.on_open = lambda ws: self.connected.set()
        receiver.on_error = on_error
        receiver.on_close = on_close
        self.receiver = receiver
        self.socket = receiver._new_ws()
        threading.Thread(target=lambda: self.socket.run_forever(origin='https://www.douyin.com'),
                         daemon=True).start()
        if not self.connected.wait(12):
            raise BackendFault(self.socket_error or 'DOUYIN_SOCKET_CONNECT_TIMEOUT')
        return {'connected': True}

    def dispatch(self, method, args):
        if method == 'init':
            return self.initialize(args)
        if method == 'diagnostics':
            return dict(**self.diagnostics, requests=self.request_history, write_attempted=self.write_attempted,
                        halted=self.halted)
        if self.halted:
            raise BackendFault(self.halted)
        if not self.auth:
            raise BackendFault('DOUYIN_BACKEND_NOT_INITIALIZED')
        if method == 'drain':
            if self.socket_error:
                raise BackendFault(self.socket_error)
            rows = []
            while len(rows) < 100:
                try:
                    rows.append(self.inbox.get_nowait())
                except queue.Empty:
                    break
            return rows
        if method == 'capabilities':
            return self.capabilities()
        self.pace()
        if method == 'account':
            return self.account()
        if not self.uid:
            raise BackendFault('DOUYIN_ACCOUNT_UNCONFIRMED')
        if method == 'feed':
            value = self.api.get_feed(self.auth, count='10', refresh_index=str(args.get('index', 2)))
            self.diagnostics = dict(endpoint='feed', keys=[k for k in value if re.fullmatch(r'[a-zA-Z_]{1,60}', k)],
                shapes={k: type(v).__name__ for k, v in value.items() if re.fullmatch(r'[a-zA-Z_]{1,60}', k)},
                status_code=value.get('status_code')) if isinstance(value, dict) else dict(endpoint='feed', shape=type(value).__name__)
            cards = value.get('cards') if isinstance(value, dict) else None
            if isinstance(cards, list) and cards and isinstance(cards[0], dict):
                self.diagnostics['first_card_shapes'] = {k: type(v).__name__ for k, v in cards[0].items()
                    if re.fullmatch(r'[a-zA-Z_]{1,60}', k)}
                self.diagnostics['first_card_nested_keys'] = {k: [n for n in v if re.fullmatch(r'[a-zA-Z_]{1,60}', n)]
                    for k, v in cards[0].items() if isinstance(v, dict) and re.fullmatch(r'[a-zA-Z_]{1,60}', k)}
            return feed_envelope(value)
        if method == 'detail':
            return envelope(self.api.get_work_info(self.auth, args['url']), 'aweme_detail')
        if method == 'comments':
            return envelope(self.api.get_work_out_comment(self.auth, args['url'], cursor=str(args.get('cursor', 0))), 'comments')
        if method == 'search':
            return envelope(self.api.search_general_work(self.auth, args['query']), 'data')
        if method in ('profile', 'mention_target'):
            peer = self.peers.get(str(args.get('uid'))) if method == 'mention_target' else None
            sec = peer.get('mention_sec_uid', '') if peer else args.get('sec_uid', '')
            if (method == 'mention_target' and not peer) or not re.fullmatch(r'MS4[A-Za-z0-9_-]{10,150}', sec):
                raise BackendFault('DOUYIN_MENTION_TARGET_MISSING')
            user = envelope(self.api.get_user_info(self.auth, 'https://www.douyin.com/user/'+sec), 'user')['user']
            uid, name = positive(user.get('uid')), user.get('nickname')
            if (not uid or user.get('sec_uid') != sec or (peer and uid != peer['uid'])
                    or not isinstance(name, str) or not name.strip() or len(name) > 40
                    or any(c in name for c in '@＠\r\n')):
                raise BackendFault('DOUYIN_MENTION_TARGET_MISMATCH')
            return dict(uid=uid, sec_uid=sec, name=name)
        if method == 'listen':
            return self.listen()
        if method == 'send_preflight':
            self.write_attempted = False
            try:
                token, _ = self.api.get_identity_security_token(self.auth)
            except Exception:
                raise BackendFault('DOUYIN_IDENTITY_TOKEN_UNAVAILABLE') from None
            return dict(identity_token_present=bool(token), messages_sent=0)
        if method == 'send':
            self.write_attempted = False
            self.require_writes()
            # Resolve only the existing, explicitly bound conversation. Never create a new one.
            supplied = args.get('peer', {})
            peer = self.peers.get(str(supplied.get('uid')))
            if not peer or supplied != peer:
                raise BackendFault('DOUYIN_CONVERSATION_UNCONFIRMED')
            text = args.get('text')
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                raise BackendFault('DOUYIN_PRIVATE_TEXT_INVALID')
            self.account()
            self.require_writes()
            parts = peer['conversation_id'].split(':')
            if (len(parts) != 4 or parts[:2] != ['0', '1']
                    or set(parts[2:]) != {self.uid, peer['uid']} or not peer.get('ticket')):
                raise BackendFault('DOUYIN_CONVERSATION_UNCONFIRMED')
            return self.send_text(peer, text, args.get('client_message_id'))
        if method in ('publish', 'publish_mention'):
            self.write_attempted = False
            self.require_writes()
            self.account()
            extra = []
            target = None
            if method == 'publish_mention':
                target = self.dispatch('mention_target', dict(uid=args.get('mention_uid')))
                prefix = '@'+target['name']+' '
                if (not args.get('text', '').startswith(prefix)
                        or any(c in args['text'][len(prefix):] for c in '@＠')):
                    raise BackendFault('DOUYIN_MENTION_TEXT_MISMATCH')
                # Douyin comment offsets use JavaScript UTF-16 code units.
                extra = [dict(start=0, end=len(prefix.rstrip().encode('utf-16-le'))//2,
                              type=0, user_id=target['uid'], sec_uid=target['sec_uid'])]
            value = envelope(self.api.publish_comment(self.auth, args['video_id'], args['text'], text_extra=extra))
            comment = value.get('comment') or {}
            if not positive(comment.get('cid')) or str(comment.get('aweme_id')) != args['video_id']:
                raise BackendFault('DOUYIN_COMMENT_RESULT_UNCONFIRMED')
            receipt = {'root_id': str(comment['cid']), 'video_id': args['video_id'], 'type': 1}
            if target:
                confirmed = any(isinstance(item, dict) and str(item.get('user_id')) == target['uid']
                    and item.get('type') in (0, '0') and item.get('start') in (0, '0')
                    and str(item.get('end')) == str(extra[0]['end'])
                    for item in comment.get('text_extra') or [])
                if not confirmed:
                    raise BackendFault('DOUYIN_MENTION_RESULT_UNCONFIRMED')
                receipt.update(mention_uid=target['uid'], mention_name=target['name'], mention_confirmed=True)
            return receipt
        raise BackendFault('DOUYIN_BACKEND_METHOD_INVALID')

    def close(self):
        if self.socket:
            self.socket.close()
        if self.auth:
            self.auth.close()


def main():
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    output = sys.stdout
    worker = Worker()
    try:
        with open(os.devnull, 'w', encoding='utf-8') as silent:
            # Includes prints from upstream callbacks running in other threads.
            sys.stdout = sys.stderr = silent
            for raw in sys.stdin:
                request = {}
                try:
                    if len(raw) > 1_000_000:
                        raise BackendFault('DOUYIN_BACKEND_REQUEST_TOO_LARGE')
                    request = json.loads(raw)
                    if not isinstance(request,dict):
                        raise BackendFault('DOUYIN_BACKEND_REQUEST_INVALID')
                    if request.get('method') in ('send','publish','publish_mention'):
                        worker.write_attempted = False
                    result = worker.dispatch(request['method'], request.get('args', {}))
                    response = {'ok': True, 'data': result}
                except Exception as exc:
                    code = classify(exc)
                    if code in {'DOUYIN_VERIFICATION_REQUIRED', 'DOUYIN_CAPTCHA_REQUIRED',
                                'DOUYIN_IDENTITY_VERIFICATION_REQUIRED','DOUYIN_AUTH_EXPIRED',
                                'DOUYIN_FORBIDDEN','DOUYIN_PRECONDITION_FAILED','DOUYIN_RATE_LIMITED','DOUYIN_API_REJECTED',
                                'DOUYIN_ACCOUNT_CHANGED', 'DOUYIN_BACKEND_REQUEST_FAILED'}:
                        worker.halted = code
                    response = {'ok': False, 'code': code,
                        'before_write':isinstance(request,dict) and request.get('method') in ('send','publish','publish_mention') and not worker.write_attempted,
                        'diagnostics':dict(requests=worker.request_history, write_attempted=worker.write_attempted,
                            socket_detail=worker.socket_detail if code == 'DOUYIN_SOCKET_FAILED' else '')}
                output.write(json.dumps(response, ensure_ascii=False) + '\n')
                output.flush()
    finally:
        with contextlib.redirect_stdout(open(os.devnull, 'w')), contextlib.redirect_stderr(open(os.devnull, 'w')):
            worker.close()


if __name__ == '__main__':
    main()

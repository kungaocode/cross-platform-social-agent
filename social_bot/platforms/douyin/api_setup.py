"""One-time local session import; normal IM operation remains HTTP/WebSocket only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

from .backend import DouyinAPIError, SpiderClient, read_credentials
from .api_inbox import peer_bindings
from .im_protocol import conversation_rows


def save_credentials(path, value, snapshot):
    """Atomic replacement, refusing a concurrent change to the login or bindings."""
    path = Path(path)
    if path.read_bytes() != snapshot:
        raise DouyinAPIError('DOUYIN_CREDENTIALS_CHANGED', before_write=True)
    fd, temporary = tempfile.mkstemp(prefix='.douyin-session-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        if path.read_bytes() != snapshot:
            raise DouyinAPIError('DOUYIN_CREDENTIALS_CHANGED', before_write=True)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def security_material(storage):
    result = {}
    for key, names in [
        ('security-sdk/s_sdk_sign_data_key/web_protect', ('ticket', 'ts_sign', 'client_cert')),
        ('security-sdk/s_sdk_crypt_sdk', ('ec_privateKey',)),
    ]:
        try:
            data = json.loads(storage[key])
            data = data.get('data', data)
            if isinstance(data, str):
                data = json.loads(data)
            for name in names:
                if isinstance(data.get(name), str) and data[name]:
                    result['private_key' if name == 'ec_privateKey' else name] = data[name]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return result


def bind_peer(options, uid, name=''):
    path = Path(options['credentials'])
    snapshot = path.read_bytes()
    credentials = read_credentials(path)
    candidates = peer_bindings(dict(peers=credentials.get('discovered_peers', [])), credentials.get('uid'))
    if uid not in candidates:
        raise DouyinAPIError('DOUYIN_PEER_NOT_DISCOVERED', before_write=True)
    peers = peer_bindings(credentials, credentials.get('uid'))
    peers[uid] = dict(candidates[uid], name=name[:80] or peers.get(uid, {}).get('name', ''))
    credentials['peers'] = list(peers.values())
    peer_bindings(credentials, credentials.get('uid'))
    save_credentials(path, credentials, snapshot)
    return dict(bound=True, uid=uid, bound_peers=len(peers), messages_sent=0)


def setup_session(config, options, seconds=60):
    """Refresh a verified account and collect tickets from normal read responses.

    The user opens the desired existing private conversations in the setup window.
    No message editor is accessed. Candidate discovery does not enable replies.
    """
    from playwright.sync_api import sync_playwright
    from .adapter import browser_args
    path = Path(options['credentials'])
    snapshot = path.read_bytes()
    previous = read_credentials(path)
    with SpiderClient(options) as current:
        account = current.uid
    profile = Path(config['memory_dir']) / ('douyin-profile-'+config['account_id'])
    if not profile.is_dir():
        raise DouyinAPIError('DOUYIN_BROWSER_PROFILE_MISSING')
    candidates = {}
    capture_failed = []
    with sync_playwright() as pw:
        with pw.chromium.launch_persistent_context(str(profile), channel=config['browser_channel'],
                headless=False, viewport={'width': 1920, 'height': 1000}, args=browser_args(config)) as context:
            def response_received(response):
                address = urlsplit(response.url)
                if (address.scheme != 'https' or address.hostname != 'imapi.douyin.com'
                        or not address.path.startswith(('/v1/message/get_user_message',
                            '/v2/conversation/get_', '/v1/conversation/get_',
                            '/v1/message/init', '/v2/message/init'))):
                    return
                try:
                    raw = response.body()
                    if len(raw) > 2_000_000:
                        return
                    for peer in conversation_rows(raw, account):
                        existing = candidates.get(peer['uid'])
                        if existing and (existing['conversation_id'], existing['short_id']) != (
                                peer['conversation_id'], peer['short_id']):
                            capture_failed.append(True)
                        candidates[peer['uid']] = peer
                except Exception:
                    # No response contents or URLs go to logs.
                    capture_failed.append(True)

            context.on('response', response_received)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto('https://www.douyin.com/chat', wait_until='domcontentloaded', timeout=90000)
            print(f'请在专用窗口打开需要测试的已有私聊；{seconds} 秒后自动保存。无需发送消息。', flush=True)
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                page.wait_for_timeout(500)
            storage = page.evaluate('''() => Object.fromEntries([
                'security-sdk/s_sdk_sign_data_key/web_protect', 'security-sdk/s_sdk_crypt_sdk'
                ].map(k => [k, localStorage.getItem(k)]))''')
            cookies = context.cookies('https://www.douyin.com/')
            material = security_material(storage)
            if any(not material.get(k) for k in ('ticket', 'ts_sign', 'private_key')):
                raise DouyinAPIError('DOUYIN_WRITE_CREDENTIALS_MISSING')
            credentials = dict(previous, **material,
                cookie='; '.join(c['name']+'='+c['value'] for c in cookies))
            captured = page.evaluate('''() => ({ua:navigator.userAgent,
                cpu_core_num:navigator.hardwareConcurrency, device_memory:navigator.deviceMemory||8,
                screen_width:screen.width, screen_height:screen.height,
                inner_width:innerWidth, inner_height:innerHeight,
                outer_width:outerWidth, outer_height:outerHeight,
                avail_width:screen.availWidth, avail_height:screen.availHeight})''')
            import re
            version = re.search(r'(?:Chrome|Chromium)/([0-9.]+)', captured['ua'])
            if version:
                captured.update(browser_version=version[1], engine_version=version[1])
            credentials['browser_profile'] = captured
            # Do not carry session-bound tickets forward from the previous capture.
            selected = peer_bindings(previous, account)
            if capture_failed:
                raise DouyinAPIError('DOUYIN_CONVERSATION_CAPTURE_FAILED')
            if any(uid not in candidates for uid in selected):
                raise DouyinAPIError('DOUYIN_BOUND_PEER_NOT_REDISCOVERED')
            credentials['peers'] = [dict(candidates[uid], **{k:p[k] for k in ('name', 'mention_sec_uid') if k in p})
                                    for uid, p in selected.items()]
            old_candidates = {p['uid']:p for p in previous.get('discovered_peers', [])}
            for uid, peer in candidates.items():
                old = selected.get(uid) or old_candidates.get(uid, {})
                peer.update({k:old[k] for k in ('name', 'mention_sec_uid') if k in old})
            credentials['discovered_peers'] = list(candidates.values())[:50]
    # Verify the captured cookie belongs to the same account before touching the real file.
    fd, temporary = tempfile.mkstemp(prefix='.douyin-verify-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(credentials, stream)
        os.chmod(temporary, 0o600)
        with SpiderClient(dict(options, credentials=temporary, expected_uid=account)) as api:
            capability = api.call('capabilities')
            if capability.get('write_session_matches') is not True:
                raise DouyinAPIError('DOUYIN_WRITE_SESSION_MISMATCH')
        save_credentials(path, credentials, snapshot)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return dict(imported=True, account_bound=True, writes_credentials_present=True,
                candidates=[dict(uid=p['uid'], short_id=p['short_id']) for p in candidates.values()],
                bound_peers=len(credentials['peers']), messages_sent=0)

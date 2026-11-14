"""Douyin backend diagnostics, local session setup and explicit private-message tests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from social_bot.platforms.douyin.adapter import load_config, browser_args
from social_bot.platforms.douyin.backend import SpiderClient, backend_options, DouyinAPIError, APIVideoBrowser

ROOT = Path(__file__).resolve().parent


def export_session(config, options):
    """Read this project's profile once. Never navigate to a Douyin page."""
    from playwright.sync_api import sync_playwright
    destination = Path(options['credentials'])
    if destination.exists():
        raise DouyinAPIError('DOUYIN_EXPORT_DESTINATION_EXISTS')
    profile = Path(config['memory_dir']) / ('douyin-profile-'+config['account_id'])
    if not profile.is_dir():
        raise DouyinAPIError('DOUYIN_BROWSER_PROFILE_MISSING')
    with sync_playwright() as pw:
        with pw.chromium.launch_persistent_context(str(profile), channel=config['browser_channel'],
                headless=True, viewport={'width': 1920, 'height': 1000}, args=browser_args(config)) as context:
            cookies = context.cookies('https://www.douyin.com/')
            if not any(c['name'] == 'sessionid' and c['value'] for c in cookies):
                raise DouyinAPIError('DOUYIN_PROFILE_NEEDS_LOGIN')
            # Read from an already open matching origin only; no navigation or storage-state export.
            security = {}
            for page in context.pages:
                if page.url.startswith('https://www.douyin.com/'):
                    security = page.evaluate('''() => Object.fromEntries([
                        'security-sdk/s_sdk_sign_data_key/web_protect', 'security-sdk/s_sdk_crypt_sdk'
                    ].map(k=>[k,localStorage.getItem(k)]))''')
                    break
            credentials = dict(cookie='; '.join(c['name']+'='+c['value'] for c in cookies), peers=[])
            if context.pages:
                captured = context.pages[0].evaluate('''() => ({ua:navigator.userAgent,
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
            for key, field in [('security-sdk/s_sdk_sign_data_key/web_protect', 'protect'),
                               ('security-sdk/s_sdk_crypt_sdk', 'keys')]:
                try:
                    data = json.loads(json.loads(security[key])['data'])
                    if field == 'protect':
                        credentials.update({k: data[k] for k in ('ticket', 'ts_sign', 'client_cert') if k in data})
                    else:
                        credentials['private_key'] = data['ec_privateKey']
                except (KeyError, TypeError, ValueError):
                    pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: do not overwrite another account's credentials or peer bindings.
    with destination.open('x', encoding='utf-8') as target:
        json.dump(credentials, target, ensure_ascii=False, indent=2)
    try:
        with SpiderClient(options, discover=True) as api:
            credentials['uid'] = api.uid
        destination.write_text(json.dumps(credentials, ensure_ascii=False, indent=2), encoding='utf-8')
    except BaseException:
        # The local export is useful for diagnosis, but it is still unbound and cannot run the bot.
        raise
    return dict(exported=True, account_bound=True, writes_credentials_present=all(credentials.get(k)
        for k in ('ticket', 'ts_sign', 'private_key')), browser_navigations=0, messages_sent=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'export', 'probe', 'socket', 'analyze',
        'setup', 'status', 'bind', 'receive', 'send', 'enable-replies'])
    parser.add_argument('--config', type=Path, default=ROOT/'douyin/config.json')
    parser.add_argument('--url', help='Optional canonical video URL for the read probe')
    parser.add_argument('--seconds', type=int, default=20)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--peer-uid', help='Explicit numeric UID of a discovered private contact')
    parser.add_argument('--name', default='', help='Local display name for the selected contact')
    parser.add_argument('--text', help='Text for the explicit send command (never logged in reports)')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60:
        parser.error('--seconds 须为 1～60')
    if args.command == 'analyze' and not args.url:
        parser.error('analyze 需要 --url 指定视频')
    if args.command in ('send', 'bind') and not args.peer_uid:
        parser.error('需要 --peer-uid 指定测试联系人数字 UID')
    if args.command == 'send' and (not args.text or not args.text.strip() or len(args.text) > 1000):
        parser.error('send 需要 --text，长度为 1～1000 字符')
    config = load_config(args.config)
    options = backend_options(config, args.config)
    try:
        if args.command == 'check':
            result = dict(repo_present=Path(options['repo']).is_dir(), python_present=Path(options['python']).is_file(),
                credentials_present=Path(options['credentials']).is_file(), network_calls=0, messages_sent=0)
        elif args.command == 'export':
            result = export_session(config, options)
        elif args.command == 'setup':
            from social_bot.platforms.douyin.api_setup import setup_session
            result = setup_session(config, options, args.seconds)
        elif args.command == 'bind':
            from social_bot.platforms.douyin.api_setup import bind_peer
            result = bind_peer(options, args.peer_uid, args.name)
        elif args.command == 'enable-replies':
            from social_bot.platforms.douyin.api_setup import save_credentials
            from social_bot.platforms.douyin.api_inbox import peer_bindings
            from social_bot.platforms.douyin.backend import read_credentials
            if config.get('transport') != 'api' or config.get('enabled') is not True:
                raise DouyinAPIError('DOUYIN_API_CONFIG_REQUIRED')
            if config.get('comments_enabled') or config.get('comment_replies_enabled'):
                raise DouyinAPIError('DOUYIN_API_NOTIFICATIONS_NOT_IMPLEMENTED')
            snapshot = args.config.read_bytes()
            with SpiderClient(dict(options, writes_enabled=True)) as api:
                peers = peer_bindings(read_credentials(options['credentials']), api.uid)
                capability = api.call('capabilities')
                if not peers or capability.get('ready_to_reply') is not True:
                    raise DouyinAPIError('DOUYIN_API_NOT_READY_TO_REPLY')
                api.call('send_preflight')
            raw = json.loads(snapshot.decode('utf-8-sig'))
            raw.setdefault('api_backend', {})['writes_enabled'] = True
            save_credentials(args.config, raw, snapshot)
            result = dict(enabled=True, bound_peers=len(peers), messages_sent=0,
                          next_command='python run_douyin.py listen --config '+str(args.config))
        else:
            config_snapshot = args.config.read_bytes()
            def permitted():
                if args.command not in ('send', 'receive'):
                    return True
                return config.get('enabled') is True and args.config.read_bytes() == config_snapshot
            with SpiderClient(options, permitted=permitted) as api:
                if args.command in ('status', 'receive', 'send'):
                    from social_bot.platforms.douyin.backend import read_credentials
                    from social_bot.platforms.douyin.api_inbox import peer_bindings, push_event
                    credentials = read_credentials(options['credentials'])
                    peers = peer_bindings(credentials, api.uid)
                    if args.command == 'status':
                        result = api.call('capabilities')
                        result.update(success=True, messages_sent=0,
                            candidates=[dict(uid=p['uid'], short_id=p['short_id'])
                                for p in peer_bindings(dict(peers=credentials.get('discovered_peers', [])), api.uid).values()])
                    elif args.command == 'send':
                        peer = peers.get(args.peer_uid)
                        if not peer:
                            raise DouyinAPIError('DOUYIN_PEER_NOT_BOUND', before_write=True)
                        result = api.call('send', dict(peer=peer, text=args.text))
                        result.update(success=True, messages_sent=1)
                    else:
                        if not peers:
                            raise DouyinAPIError('DOUYIN_API_PEERS_REQUIRED')
                        started = int(time.time())
                        api.call('listen')
                        deadline = time.monotonic()+args.seconds
                        seen, summaries, pushes = set(), [], 0
                        print('后台只收信测试已连接，请让已绑定联系人发送新文字；不会回复。', flush=True)
                        while time.monotonic() < deadline:
                            rows = api.call('drain')
                            pushes += len(rows)
                            for row in rows:
                                event = push_event(row, api.uid, peers, started)
                                if event and event.key not in seen:
                                    seen.add(event.key)
                                    summaries.append(dict(sender=event.sender, message_id=event.id,
                                                          text_length=len(event.text)))
                            time.sleep(.5)
                        result = dict(success=True, connected=True, pushes=pushes,
                                      private_text_received=len(summaries), events=summaries, messages_sent=0)
                elif args.command == 'analyze':
                    from social_bot.settings import load_settings
                    from social_bot.deepseek import DeepSeekClient
                    from social_bot.deep_video import DeepVideoAnalyzer, deep_policy
                    from social_bot.video_evidence import VideoEvidenceCollector
                    from social_bot.video_browser import VideoAgentTools
                    from social_bot.store import ConversationStore
                    settings = load_settings(config['base_config'])
                    policy = deep_policy(config.get('video_social', {}).get('deep_understanding'))
                    browser = APIVideoBrowser(api)
                    browser.candidates = {args.url: '用户指定视频'}
                    browser.next(args.url)
                    collector = VideoEvidenceCollector.from_settings(settings, policy=policy)
                    deep = DeepVideoAnalyzer(collector, DeepSeekClient(settings.api_key, settings.deepseek), policy)
                    store = ConversationStore.from_runtime(settings.runtime,
                        database_path=Path(config['memory_dir'])/'video-browser.sqlite3', account_id=api.uid)
                    try:
                        tools = VideoAgentTools(browser, None, store, 'douyin:'+api.uid,
                            seconds=max(2, args.seconds), max_seconds=300, deep_analyzer=deep)
                        evidence = tools.execute('video_analyze', dict(question='概述可验证的视频内容，区分画面和声音。'))
                        result = {k: evidence.get(k) for k in ('success', 'reason', 'analysis_verified',
                            'material_transport', 'sampled_frame_seconds', 'has_audio', 'transcript_source',
                            'coverage_start_seconds', 'coverage_end_seconds', 'text') if k in evidence}
                        result.update(transport='api', browser_navigations=0, messages_sent=0)
                    finally:
                        store.close()
                elif args.command == 'probe':
                    browser = APIVideoBrowser(api)
                    try:
                        observation = browser.recommend()
                    except DouyinAPIError as exc:
                        result = dict(success=False, reason=exc.code, diagnostics=api.call('diagnostics'), messages_sent=0)
                        observation = None
                    if observation is None:
                        if args.output:
                            args.output.parent.mkdir(parents=True, exist_ok=True)
                            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                        return 1
                    result = dict(success=True, account_bound=True, candidates=len(observation['candidates']),
                        transport='api', browser_navigations=0, messages_sent=0)
                    url = args.url or (observation['candidates'][0]['url'] if observation['candidates'] else '')
                    if url:
                        detail = api.detail(url)
                        comments = api.comments(url)
                        result.update(video_id=str(detail['aweme_id']), video_detail=True,
                                      comments=len(comments.get('comments') or []))
                else:
                    api.call('listen')
                    deadline = time.monotonic()+args.seconds
                    count = 0
                    while time.monotonic() < deadline:
                        count += len(api.call('drain'))
                        time.sleep(.5)
                    result = dict(success=True, connected=True, seconds=args.seconds, pushes=count,
                                  messages_sent=0, gap_recovery_implemented=False)
    except DouyinAPIError as exc:
        result = dict(success=False, reason=exc.code, messages_sent=0)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('success') is False else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('已停止。')

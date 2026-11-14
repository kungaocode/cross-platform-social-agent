"""Entry points shared by the existing chat and video launchers."""
from __future__ import annotations

import json
from pathlib import Path
import time

from .backend import APIVideoBrowser, SpiderClient, DouyinAPIError, backend_options, read_credentials


def unchanged(path, snapshot):
    try:
        return Path(path).read_bytes() == snapshot
    except OSError:
        return False


SOCKET_ERRORS = frozenset({'DOUYIN_SOCKET_FAILED', 'DOUYIN_SOCKET_DISCONNECTED',
                           'DOUYIN_SOCKET_CONNECT_TIMEOUT', 'DOUYIN_BACKEND_STOPPED',
                           'DOUYIN_BACKEND_TIMEOUT_OR_STOPPED'})


def retryable_socket_error(exc):
    """A lost socket or a dead worker is retried; auth and credential faults stop the listener."""
    return getattr(exc, 'code', '') in SOCKET_ERRORS


def reconnect_session(api, settings, permitted):
    """Rebuild the worker and resubscribe after a transient transport failure.

    Returns the successful attempt index; raises for non-retryable faults or when attempts run out.
    """
    delay = 1
    for attempt in range(1, settings['max_attempts'] + 1):
        if not permitted():
            raise DouyinAPIError('DOUYIN_DISABLED', before_write=True)
        time.sleep(delay)
        try:
            api.close()
            api.start()
            api.call('listen')
        except DouyinAPIError as exc:
            if not retryable_socket_error(exc):
                raise
            delay = min(settings['max_delay_seconds'], delay * 2)
            print(json.dumps(dict(event='douyin_socket_retry_failed', attempt=attempt, code=exc.code),
                             ensure_ascii=False), flush=True)
            continue
        return attempt
    raise DouyinAPIError('DOUYIN_SOCKET_RECONNECT_FAILED')


def reconnect_settings(config):
    raw = config.get('api_reconnect', {})
    if not isinstance(raw, dict):
        raise DouyinAPIError('DOUYIN_API_RECONNECT_INVALID')
    enabled = raw.get('enabled', True)
    attempts, delay = raw.get('max_attempts', 10), raw.get('max_delay_seconds', 60)
    if type(enabled) is not bool:
        raise DouyinAPIError('DOUYIN_API_RECONNECT_INVALID')
    if type(attempts) is not int or not 1 <= attempts <= 50:
        raise DouyinAPIError('DOUYIN_API_RECONNECT_INVALID')
    if type(delay) is not int or not 5 <= delay <= 300:
        raise DouyinAPIError('DOUYIN_API_RECONNECT_INVALID')
    return dict(enabled=enabled, max_attempts=attempts, max_delay_seconds=delay)


def chat(command, config, path):
    options = backend_options(config, path)
    if command == 'check':
        print(json.dumps(dict(transport='api', repo_present=Path(options['repo']).is_dir(),
            python_present=Path(options['python']).is_file(), credentials_present=Path(options['credentials']).is_file(),
            writes_enabled=options['writes_enabled'], network_calls=0), ensure_ascii=False))
        return
    if command == 'login':
        raise DouyinAPIError('DOUYIN_USE_API_EXPORT_COMMAND')
    if command == 'mentions':
        raise DouyinAPIError('DOUYIN_API_NOTIFICATIONS_NOT_IMPLEMENTED')
    snapshot = Path(path).read_bytes()
    permitted = lambda: config.get('enabled') is True and unchanged(path, snapshot)
    with SpiderClient(options, permitted=permitted) as api:
        from .api_inbox import peer_bindings, APIInboxPump
        credentials = read_credentials(options['credentials'])
        peers = peer_bindings(credentials, api.uid)
        if command == 'list':
            print(json.dumps([dict(uid=p['uid'], name=p.get('name', ''), conversation_id=p['conversation_id'])
                              for p in peers.values()], ensure_ascii=False))
            return
        if not options['writes_enabled']:
            raise DouyinAPIError('DOUYIN_API_WRITES_DISABLED')
        if config.get('comments_enabled') or config.get('comment_replies_enabled'):
            raise DouyinAPIError('DOUYIN_API_NOTIFICATIONS_NOT_IMPLEMENTED')
        if api.call('capabilities').get('ready_to_reply') is not True:
            raise DouyinAPIError('DOUYIN_API_NOT_READY_TO_REPLY')
        api.call('send_preflight')
        from .adapter import build_service
        service = build_service(config)
        # API chat launches video jobs only through the separately enabled fixed-command controller.
        service.video_fixed_commands_only = True
        try:
            pump = APIInboxPump(api, service, config, credentials, permitted=permitted)
            if config.get('api_video_commands', {}).get('enabled') is True:
                from .api_video_commands import APIVideoCommands
                pump.video_commands = APIVideoCommands(pump, service, config, path)
            print('后台接口监听已连接；仅回复凭据文件中绑定的个人文字会话。', flush=True)
            reconnect = reconnect_settings(config)
            while permitted():
                try:
                    pump.step()
                except DouyinAPIError as exc:
                    if not reconnect['enabled'] or not retryable_socket_error(exc):
                        raise
                    lost_at = time.monotonic()
                    print(json.dumps(dict(event='douyin_socket_lost', code=exc.code,
                        at=time.strftime('%Y-%m-%dT%H:%M:%S')), ensure_ascii=False), flush=True)
                    attempt = reconnect_session(api, reconnect, permitted)
                    print(json.dumps(dict(event='douyin_socket_reconnected', attempt=attempt,
                        gap_seconds=round(time.monotonic() - lost_at, 1)), ensure_ascii=False), flush=True)
                    continue
                time.sleep(config['poll_seconds'])
        finally:
            service.close()


def video(config, path, control, args, policy, settings):
    from ...deepseek import DeepSeekClient
    from ...deep_video import DeepVideoAnalyzer
    from ...video_evidence import VideoEvidenceCollector
    from ...video_browser import VideoAgentTools
    from ...video_social import VideoCurator
    from ...video_participation import ThreadRegistry
    from ...store import ConversationStore
    from .adapter import build_service
    options = backend_options(config, path)
    # Do not silently degrade @ mentions or two-hour thread monitoring to ordinary comments.
    if policy['post_comments'] and policy['max_comments_per_run'] and not args.dry_run:
        if not options['writes_enabled']:
            raise DouyinAPIError('DOUYIN_API_WRITES_DISABLED', before_write=True)
        if policy['interaction_mode'] == 'watch':
            raise DouyinAPIError('DOUYIN_API_COMMENT_WATCH_NOT_IMPLEMENTED', before_write=True)
    snapshot, control_snapshot = Path(path).read_bytes(), Path(control).read_bytes()
    permitted = lambda: (config.get('enabled') is True and unchanged(path, snapshot)
                         and unchanged(control, control_snapshot))
    with SpiderClient(options, permitted=permitted) as api:
        target = None
        if policy['comment_preset']['enabled']:
            preset = policy['comment_preset']
            target = api.mention_target(preset['owner_uid'])
            # Bind to the exact account that sent the command: same uid, nickname and sec_uid.
            if (target['name'] != preset['owner_name']
                    or (preset.get('owner_sec_uid') and target.get('sec_uid') != preset['owner_sec_uid'])):
                raise DouyinAPIError('DOUYIN_MENTION_TARGET_MISMATCH', before_write=True)
        memory = Path(config['memory_dir'])
        service = build_service(config)
        videos = None
        registry = None
        try:
            videos = ConversationStore.from_runtime(service.settings.runtime,
                database_path=memory/'video-browser.sqlite3', account_id=api.uid)
            registry = ThreadRegistry(memory/'video-social.sqlite3')
            browser = APIVideoBrowser(api, max_videos=policy['max_videos_per_run'],
                max_source_seconds=options['max_source_seconds'],
                filter_long=policy['selection_mode'] != 'recommendation_order',
                snapshot_path=(memory/('douyin-recommendations-'+api.uid+'.json')
                    if policy['selection_mode'] == 'recommendation_order' and not args.dry_run else None))
            client = DeepSeekClient(settings.api_key, settings.deepseek)
            collector = VideoEvidenceCollector.from_settings(settings, policy=policy['deep_understanding'])
            # deep_understanding.enabled=false selects the title+frames light path (no ASR, no audit).
            if policy['deep_understanding']['enabled']:
                analyzer = DeepVideoAnalyzer(collector, client, policy['deep_understanding'], permitted=permitted)
            else:
                from ...video_light import LightVideoAnalyzer
                analyzer = LightVideoAnalyzer(collector, client, policy['deep_understanding'], permitted=permitted)
            tools = VideoAgentTools(browser, None, videos, 'douyin:'+api.uid,
                seconds=policy['analysis_seconds'], max_seconds=policy['max_runtime_seconds'], deep_analyzer=analyzer)
            # Comment page goes through the visible browser when configured; chat stays on the API.
            publisher = None
            if (target and config.get('video_social', {}).get('comment_transport') == 'browser'):
                from .browser_comment import BrowserCommentPublisher
                publisher = BrowserCommentPublisher(config, api.uid, permitted=permitted,
                    headless=bool(config.get('video_social', {}).get('browser_headless', False)))
            if publisher is not None:
                publish = lambda url, text: publisher.publish(url, text, target)
            else:
                publish = (lambda url, text: api.publish_mention(url, text, target['uid'])) if target else api.publish
            def save_report(value):
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    temporary = args.output.with_suffix('.tmp')
                    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
                    temporary.replace(args.output)
            def progress(event):
                print(json.dumps(event, ensure_ascii=False), flush=True)
                save_report(dict(success=True, job_phase='running', records=curator.records,
                    opened=len(browser.visited), attempted=len(curator.records), requested=policy['max_videos_per_run'],
                    published=sum(r.get('publication') == 'published' for r in curator.records),
                    transport='api', stage=event.get('stage')))
            curator = VideoCurator(tools, client, registry, publish, policy, account=api.uid,
                                  permitted=permitted, public_store=service.store, wait=time.sleep, progress=progress)
            if publisher is not None:
                publisher.open()
            try:
                try:
                    report = curator.run(dry_run=args.dry_run)
                except BaseException as exc:
                    save_report(dict(success=False, job_phase='failed', records=curator.records,
                        opened=len(browser.visited), attempted=len(curator.records), requested=policy['max_videos_per_run'],
                        reason=getattr(exc, 'code', type(exc).__name__), transport='api'))
                    raise
            finally:
                if publisher is not None:
                    publisher.close()
            report.update(transport='api', playback_confirmed=0, mode=policy['interaction_mode'],
                          comment_watch=dict(enabled=False, seconds=0, completed=False), job_phase='completed')
            report['success'] = bool(report['records']) and all(row.get('analysis_success') is True for row in report['records'])
            if policy['selection_mode'] == 'recommendation_order':
                report['success'] = report['success'] and report['recommendation_refresh']['success'] is True
            save_report(report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            if registry:
                registry.close()
            if videos:
                videos.close()
            service.close()

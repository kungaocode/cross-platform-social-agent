"""Bilibili/Douyin recommendation watching, sourced comments and notification replies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from social_bot.deepseek import DeepSeekClient
from social_bot.online_video import OnlineVideoAnalyzer
from social_bot.modules.video import OmniVideoAnalyzer, VideoError
from social_bot.settings import load_settings
from social_bot.service import BotService
from social_bot.store import ConversationStore
from social_bot.video_browser import VideoBrowser, VideoAgentTools
from social_bot.video_participation import ThreadRegistry
from social_bot.video_social import VideoCurator, policy_from
from social_bot.platforms.common.chat import platform_settings, SocialResponder, PollLoop
from social_bot.platforms.common.media import SocialMediaReader

ROOT = Path(__file__).resolve().parent
STARTED_AT = time.time()


def load_inputs(platform, path):
    if platform == "douyin":
        from social_bot.platforms.douyin.adapter import load_config
        config = load_config(path)
    else:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        from social_bot.platforms.bilibili.chat import validate_config
        validate_config(config)
        config["base_config"] = str((path.parent / config["base_config"]).resolve())
        directory = Path(config["memory_dir"])
        config["memory_dir"] = str(directory if directory.is_absolute() else (path.parent / directory).resolve())
    return config


def session_stamp(context):
    pairs = sorted((c["name"], c["value"]) for c in context.cookies("https://www.douyin.com/")
                   if c["name"] in ("sessionid", "sessionid_ss"))
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest() if pairs else ""


class DouyinPump:
    def __init__(self, interaction, service, config, context, permitted, *, dry_run=False):
        self.interaction, self.config, self.context, self.permitted = interaction, config, context, permitted
        self.dry_run, self.next_poll = dry_run, 0
        reader = SocialMediaReader(service.settings, "douyin",
            image_enabled=config.get("image_reading_enabled", True), video_enabled=config.get("video_reading_enabled", True),
            cookies=lambda: context.cookies("https://www.douyin.com/"))
        self.private = None
        if (config.get("peers") or config.get("all_peers")) and not dry_run:
            from social_bot.platforms.douyin.adapter import ChatLoop, DOMChat
            page = context.new_page()
            page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=90000)
            dom = DOMChat(page)
            dom.permitted = permitted
            self.private = ChatLoop(service, dom, config["peers"], permitted, media_reader=reader,
                                    all_peers=config.get("all_peers", False),
                                    group_names=config.get("group_names", ()),
                                    bot_name=config.get("bot_name", ""),
                                    max_peers=config.get("max_peers", 20),
                                    detection_mode=config.get('message_detection', 'unread'),
                                    full_scan_seconds=config.get('full_scan_seconds', 60))
        # ChatLoop 会把 service.scope 换成抖音的放行/群规则；评论作用域以它为兜底。
        responder = SocialResponder(service, "douyin", config["account_id"], config=config,
                                    reader=reader, permitted=permitted, scope_fallback=service.scope)
        source = lambda stream: interaction.poll() if stream == "mentions" else interaction.poll_replies()
        self.comments = PollLoop(responder, source, interaction.send, max_replies=config.get("max_replies_per_poll", 5))

    def poll(self):
        if self.dry_run or time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + int(self.config.get("poll_seconds", 20))
        from social_bot.platforms.douyin.adapter import BridgeError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        if self.private:
            try:
                self.private.step()
            except (BridgeError, PlaywrightTimeoutError) as exc:
                print(json.dumps(dict(stage='private_poll_unavailable', reason=type(exc).__name__)), file=sys.stderr, flush=True)
        streams = (["mentions"] if self.config.get("comments_enabled") else [])
        streams += (["replies"] if self.config.get("comment_replies_enabled") else [])
        try:
            self.comments.step(streams)
        except (BridgeError, PlaywrightTimeoutError) as exc:
            print(json.dumps(dict(stage='comment_poll_unavailable', reason=type(exc).__name__)), file=sys.stderr, flush=True)


def main():
    os.chdir(ROOT)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "login", "run"))
    parser.add_argument("platform", choices=("bilibili", "douyin"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--control-config", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cookies-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="分析并生成草稿；不发布评论或回复消息")
    parser.add_argument("--count", type=int)
    parser.add_argument('--mode', choices=('share','watch'), help='share：@主人后继续；watch：不@并跟进本次评论两小时')
    parser.add_argument("--watch-seconds", type=int)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = (args.config or ROOT / args.platform / "config.json").resolve()
    snapshot = path.read_bytes()
    control = args.control_config.resolve() if args.control_config else path
    control_snapshot = control.read_bytes()
    control_config = json.loads(control_snapshot.decode("utf-8-sig"))
    config = load_inputs(args.platform, path)
    policy = policy_from(config)
    overrides = dict(policy)
    if args.mode is not None:
        overrides['interaction_mode']=args.mode
    if args.count is not None:
        overrides["max_videos_per_run"] = args.count
    if args.watch_seconds is not None:
        if args.watch_seconds not in (0,7200):
            parser.error('--watch-seconds 仅支持 0 或 7200；请使用 --mode share/watch')
        legacy_mode='watch' if args.watch_seconds else 'share'
        if args.mode is not None and args.mode!=legacy_mode:
            parser.error('--mode 与 --watch-seconds 冲突')
        overrides['interaction_mode']=legacy_mode
    policy = policy_from({"video_social": overrides})
    settings = load_settings(config["base_config"])
    memory = Path(config["memory_dir"])
    if not memory.is_dir():
        raise ValueError("记忆目录不存在")
    creds, api, identity = {}, None, str(config.get("account_id", "default"))
    if args.platform == "bilibili":
        from social_bot.platforms.bilibili.social import BilibiliSocialClient
        credentials = path.parent / config.get("credentials_file", "credentials.json")
        creds = json.loads(credentials.read_text(encoding="utf-8-sig")) if credentials.exists() else {}
        creds = dict(SESSDATA=os.environ.get("BILIBILI_SESSDATA", creds.get("SESSDATA", "")),
                     bili_jct=os.environ.get("BILIBILI_CSRF", creds.get("bili_jct", "")))
        api = BilibiliSocialClient(creds["SESSDATA"], creds["bili_jct"])
    if args.command == "check":
        print(json.dumps(dict(platform=args.platform, transport=config.get('transport', 'browser'), enabled=config.get("enabled", False),
            video_social=policy, sessdata_present=bool(creds.get("SESSDATA")),
            csrf_present=bool(creds.get("bili_jct")), private_chat_mode=config.get("private_chat_mode", "allowlist"),
            self_user_id_present=bool(config.get("self_user_id")), network_calls=0, social_messages_sent=0),
            ensure_ascii=False, indent=2))
        return
    if args.command == "login" and args.headless:
        raise ValueError("登录需要可见浏览器")
    if args.platform == 'douyin' and config.get('transport') == 'api':
        if args.command == 'login':
            raise ValueError('后台接口模式请使用 run_douyin_api.py export 一次性导入登录态')
        if args.cookies_file:
            # API mode has its own account-bound credentials, never import browser cookies implicitly.
            raise ValueError('后台接口模式不接受临时浏览器登录态文件')
        if (config.get('enabled') is not True or policy['enabled'] is not True
                or control_config.get('enabled') is not True
                or control_config.get('video_social', {}).get('enabled') is not True):
            raise ValueError('请开启 enabled 和 video_social.enabled')
        from social_bot.platforms.douyin.api_runner import video
        video(config, path, control, args, policy, settings)
        return
    if args.command == "run":
        if (config.get("enabled") is not True or policy["enabled"] is not True
                or control_config.get("enabled") is not True
                or control_config.get("video_social", {}).get("enabled") is not True):
            raise ValueError("请先完成账号配置并开启 enabled 和 video_social.enabled")
        if api:
            account = api.account()
            if not account.get("logged_in"):
                raise ValueError("SESSDATA 已失效")
            identity = account["uid"]
            if config.get("account_uid") and config["account_uid"] != identity:
                raise ValueError("B站登录账号与 account_uid 不一致")
            if policy["post_comments"] and not args.dry_run and not creds["bili_jct"]:
                raise ValueError("发布评论需要同账号 bili_jct")
        elif not config.get("self_user_id"):
            raise ValueError("抖音刷视频评论需要 self_user_id")
    from playwright.sync_api import sync_playwright
    from social_bot.platforms.douyin.adapter import browser_args
    report = {}
    with sync_playwright() as pw:
        alias = config.get("account_id", "default")
        profile = memory / ("video-profile-" + args.platform + "-" + alias)
        with pw.chromium.launch_persistent_context(str(profile), channel=config.get("browser_channel", "msedge"),
                headless=args.headless, viewport=dict(width=1440, height=1000),
                args=browser_args(config)) as context:
            if args.cookies_file:
                cookie_path = args.cookies_file.resolve()
                if (args.platform != "douyin" or cookie_path.parent != (memory / "video-jobs").resolve()
                        or cookie_path.stat().st_size > 1_000_000):
                    raise ValueError("浏览器登录态文件范围无效")
                try:
                    cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
                finally:
                    cookie_path.unlink(missing_ok=True)
                if (not isinstance(cookies, list) or len(cookies) > 200 or any(
                        not isinstance(c, dict) or c.get("domain", "").lstrip(".") not in ("douyin.com", "www.douyin.com")
                        or not isinstance(c.get("name"), str) or not isinstance(c.get("value"), str)
                        or len(c["value"]) > 8192 for c in cookies)):
                    raise ValueError("抖音登录态字段无效")
                allowed = {"name", "value", "domain", "path", "httpOnly", "secure", "sameSite", "expires"}
                context.add_cookies([{k: v for k, v in c.items() if k in allowed} for c in cookies])
            if api and args.command == "run":
                # Only update this project's dedicated profile after checking any live account.
                old = next((c["value"] for c in context.cookies("https://www.bilibili.com/") if c["name"] == "SESSDATA"), "")
                if old and old != creds["SESSDATA"]:
                    from social_bot.platforms.bilibili.client import BilibiliClient
                    existing = BilibiliClient(old).account()
                    if existing.get("logged_in") and existing.get("uid") != identity:
                        raise ValueError("专用浏览器登录了另一个B站账号")
                values = {**creds, "DedeUserID": identity}
                context.add_cookies([dict(name=k, value=v, domain=".bilibili.com", path="/", secure=True)
                                     for k, v in values.items() if v])
            page = context.pages[0] if context.pages else context.new_page()
            home = "https://www.bilibili.com/" if api else "https://www.douyin.com/"
            page.goto(home, wait_until="domcontentloaded", timeout=90000)
            if args.command == "login":
                input("请在专用浏览器自行登录，完成后回终端按 Enter 保存关闭：")
                return
            stamp = session_stamp(context) if args.platform == "douyin" else ""
            if args.platform == "douyin" and not stamp:
                raise ValueError("抖音尚未登录，请先运行 login douyin")

            def permitted():
                return (path.read_bytes() == snapshot and control.read_bytes() == control_snapshot
                        and (args.platform != "douyin" or session_stamp(context) == stamp))

            effective = {**config, 'video_social':policy, 'comments_enabled':False,
                'comment_replies_enabled':policy['interaction_mode']=='watch' and policy['post_comments']}
            if api:
                effective['account_uid']=identity
            app = platform_settings(settings, args.platform, effective, memory / f"{args.platform}-{identity}.sqlite3",
                                    group_activation=args.platform == "douyin")
            service = BotService(app)
            if args.platform == "douyin":
                service.video_session_provider = lambda: context.cookies("https://www.douyin.com/")
            videos = ConversationStore.from_runtime(app.runtime, database_path=memory / "video-browser.sqlite3",
                                                     account_id=identity)
            registry = ThreadRegistry(memory / "video-social.sqlite3")
            try:
                browser = VideoBrowser(page, args.platform, max_videos=policy["max_videos_per_run"])
                analyzer = OnlineVideoAnalyzer(OmniVideoAnalyzer.from_settings(settings))
                from social_bot.browser_video_frames import BrowserFrameAnalyzer
                frames = BrowserFrameAnalyzer.from_settings(settings) if policy["browser_frame_fallback"] else None
                client = DeepSeekClient(settings.api_key, settings.deepseek)
                progress = lambda event: print(json.dumps(event), file=sys.stderr, flush=True)
                deep = None
                if policy["deep_understanding"]["enabled"]:
                    from social_bot.video_evidence import VideoEvidenceCollector
                    from social_bot.deep_video import DeepVideoAnalyzer
                    collector = VideoEvidenceCollector.from_settings(settings, bilibili_api=api,
                                                                    policy=policy["deep_understanding"])
                    deep = DeepVideoAnalyzer(collector, client, policy["deep_understanding"],
                                             permitted=permitted, progress=progress)
                tools = VideoAgentTools(browser, analyzer, videos, args.platform + ":" + identity,
                                        seconds=policy["analysis_seconds"], max_seconds=policy["max_runtime_seconds"],
                                        frame_analyzer=frames, deep_analyzer=deep)
                if api:
                    from social_bot.platforms.bilibili.video_social import BilibiliVideoInteraction
                    from social_bot.platforms.bilibili.browser_comments import BilibiliBrowserComments
                    preset=policy['comment_preset']
                    mention=(dict(uid=preset['owner_uid'],name=preset['owner_name']) if preset['enabled'] else None)
                    browser_comments = BilibiliBrowserComments(context, identity, creds["SESSDATA"], permitted=permitted,
                                                               mention_target=mention)
                    reader = SocialMediaReader(app, "bilibili", image_enabled=config.get("image_reading_enabled", True),
                        video_enabled=config.get("video_reading_enabled", True), cookies=lambda: context.cookies("https://www.bilibili.com/"))
                    interaction = BilibiliVideoInteraction(api, identity, registry, service=None, config=effective,
                        reader=reader, permitted=permitted, dry_run=args.dry_run,
                        cookie_provider=lambda: context.cookies("https://www.bilibili.com/"),
                        browser_comments=browser_comments, transport=policy["comment_transport"])
                    poll = lambda: None
                else:
                    from social_bot.platforms.douyin.video_social import DouyinVideoInteraction
                    notice, reply = context.new_page(), context.new_page()
                    notice.goto(home, wait_until="domcontentloaded", timeout=90000)
                    interaction = DouyinVideoInteraction(notice, reply, identity, config["self_user_id"], registry,
                        selectors={**config.get("mentions_selectors", {}), **policy.get("selectors", {})}, permitted=permitted,
                        mention_target=(dict(uid=policy['comment_preset']['owner_uid'], name=policy['comment_preset']['owner_name'])
                                        if policy['comment_preset']['enabled'] else None))
                    poll = lambda: None
                curator = VideoCurator(tools, client, registry,
                    interaction.publish, policy, account=identity, permitted=permitted, poll=poll,
                    public_store=service.store,
                    progress=progress, wait=lambda seconds:page.wait_for_timeout(max(1,int(seconds*1000))))
                try:
                    report = curator.run(dry_run=args.dry_run)
                except BaseException:
                    if args.output:
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        partial = dict(success=False, records=curator.records, opened=len(browser.visited),
                            published=sum(i.get("publication") == "published" for i in curator.records))
                        args.output.write_text(json.dumps(partial, ensure_ascii=False), encoding="utf-8")
                    raise
                from social_bot.video_watch import RunThreads,watch_comments
                roots=[row['comment_root_id'] for row in report['records'] if row.get('comment_root_id')]
                if policy['interaction_mode']=='watch' and roots and not args.dry_run:
                    scoped=RunThreads(registry,roots)
                    watch_deadline=time.monotonic()+7200
                    watch_permitted=lambda:permitted() and time.monotonic()<watch_deadline
                    responder=SocialResponder(service,args.platform,identity,config=effective,reader=None,permitted=watch_permitted)
                    if api:
                        from social_bot.platforms.bilibili.social import BilibiliSource
                        source=BilibiliSource(api,identity,scoped)
                        loop=PollLoop(responder,source.poll,source.send,max_replies=config.get('max_replies_per_poll',5),baseline_before=STARTED_AT)
                    else:
                        interaction.registry=scoped
                        loop=PollLoop(responder,lambda stream:interaction.poll_replies(),interaction.send,
                            max_replies=config.get('max_replies_per_poll',5),baseline_before=STARTED_AT)
                    def watch_poll():
                        try:
                            if api:interaction.verify_account()
                            loop.step(['replies'])
                        except Exception as exc:progress(dict(stage='comment_watch_read_failed',reason=type(exc).__name__))
                else:watch_poll=lambda:None
                report['mode']=policy['interaction_mode']
                report['comment_watch']=dict(enabled=policy['interaction_mode']=='watch' and bool(roots) and not args.dry_run,
                    seconds=7200 if policy['interaction_mode']=='watch' and roots and not args.dry_run else 0,
                    completed=False)
                report['job_phase']='watching' if report['comment_watch']['enabled'] else 'browsing_completed'
                if args.output:
                    args.output.parent.mkdir(parents=True,exist_ok=True)
                    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                report['comment_watch']=watch_comments(policy,report['records'] if not args.dry_run else [],watch_poll,permitted,
                    lambda seconds:page.wait_for_timeout(max(1,int(seconds*1000))),progress=progress)
                report["notification_watch_completed"] = report['comment_watch'].get('completed',False)
                report['job_phase']='completed'
            finally:
                registry.close()
                videos.close()
                service.close()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("已停止；已确认评论和视频记忆保留，未知发送不会自动重试。")
    except Exception as exc:
        # Browser/network exceptions can contain authenticated URLs; only safe codes leave this process.
        from social_bot.platforms.bilibili.client import BilibiliError
        from social_bot.platforms.douyin.backend import DouyinAPIError
        reason = exc.code if isinstance(exc, (BilibiliError, DouyinAPIError)) else (str(exc) if isinstance(exc, (ValueError, VideoError)) else type(exc).__name__)
        failure = dict(success=False, reason=reason)
        if "--output" in sys.argv:
            output = Path(sys.argv[sys.argv.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.is_file() and output.stat().st_mtime >= STARTED_AT and output.stat().st_size < 1_000_000:
                try:
                    failure = {**json.loads(output.read_text(encoding="utf-8")), **failure}
                except ValueError:
                    pass
            output.write_text(json.dumps(failure, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False))
        raise SystemExit(1)

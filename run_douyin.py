"""Local Douyin text-chat launcher using a dedicated browser profile."""
import argparse
import hashlib
import json
import logging
import tempfile
import traceback
from pathlib import Path

from social_bot.platforms.douyin.adapter import BridgeError, ChatLoop, DOMChat, build_service, load_config, browser_args
from social_bot.platforms.douyin.comments import DOMMentions
from social_bot.platforms.common.chat import SocialResponder, PollLoop
from social_bot.platforms.common.media import SocialMediaReader

ROOT = Path(__file__).resolve().parent


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    parser = argparse.ArgumentParser(description='抖音私信多媒体聊天与@评论回复')
    parser.add_argument('command', choices=['check', 'login', 'list', 'mentions', 'listen'])
    parser.add_argument('--config', type=Path, default=ROOT / 'douyin/config.json')
    args = parser.parse_args()
    config = load_config(args.config)
    if config.get('transport') == 'api':
        from social_bot.platforms.douyin.api_runner import chat
        chat(args.command, config, args.config)
        return
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    profile = Path(config['memory_dir']) / ('douyin-profile-' + config['account_id'])
    if args.command == 'check':
        from social_bot.settings import load_settings
        from social_bot.platforms.common.chat import platform_settings
        settings = load_settings(config['base_config'])
        platform_settings(settings, 'douyin', config, Path(config['memory_dir']) / 'check.sqlite3',
                          group_activation=True)
        with tempfile.TemporaryFile(dir=config['memory_dir']) as f:
            f.write(b'check')
        print('配置、模型密钥文件、Playwright 与记忆目录检查通过；未验证登录态或页面选择器。')
        return
    if args.command == 'listen' and (not config['enabled'] or not (
            config['peers'] or config.get('all_peers') or config.get('comments_enabled')
            or config.get('comment_replies_enabled'))):
        raise BridgeError('请先配置 peers 或 all_peers，并将 enabled 改为 true')
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile), channel=config['browser_channel'],
            headless=False, timeout=30000, viewport={'width': 1920, 'height': 1000},
            args=['--window-size=1920,1080', *browser_args(config)])
        service = None
        registry = None
        try:
            page = context.pages[0] if context.pages else context.new_page()
            start_url = ('https://www.douyin.com/chat' if args.command in ('list', 'listen')
                         else 'https://www.douyin.com/')
            page.goto(start_url, wait_until='domcontentloaded', timeout=90000)
            if args.command == 'login':
                input('请在专用浏览器中扫码登录，完成后回到此处按 Enter 保存并关闭。')
                return

            def session():
                values = sorted((c['name'], c['value']) for c in context.cookies('https://www.douyin.com/')
                                if c['name'] in ('sessionid', 'sessionid_ss'))
                return hashlib.sha256(json.dumps(values).encode()).hexdigest() if values else ''

            stamp = session()
            if not stamp:
                raise BridgeError('未发现登录会话，请先运行 login')
            dom = DOMChat(page)
            if args.command == 'list':
                print(json.dumps(dom.names(), ensure_ascii=False, indent=2))
                return
            if args.command == 'mentions':
                mentions = DOMMentions(page, page, config['account_id'], config.get('self_user_id', ''), selectors=config.get('mentions_selectors'))
                print(json.dumps([dict(id=e.id, sender=e.sender, text=e.text, thread=e.thread) for e in mentions.poll()], ensure_ascii=False, indent=2))
                return
            service = build_service(config)
            service.video_session_provider = lambda: context.cookies('https://www.douyin.com/')
            service.video_session_provider = lambda: context.cookies("https://www.douyin.com/")

            def permitted():
                current = load_config(args.config)
                return current == config and current['enabled'] and session() == stamp

            dom.permitted = permitted
            reader = SocialMediaReader(service.settings, 'douyin', image_enabled=config.get('image_reading_enabled', True),
                                       video_enabled=config.get('video_reading_enabled', True),
                                       cookies=lambda: context.cookies('https://www.douyin.com/'))
            loop = ChatLoop(service, dom, config['peers'], permitted, media_reader=reader,
                            all_peers=config.get('all_peers', False),
                            group_names=config.get('group_names', ()),
                            bot_name=config.get('bot_name', ''),
                            max_peers=config.get('max_peers', 20),
                            detection_mode=config.get('message_detection', 'unread'),
                            full_scan_seconds=config.get('full_scan_seconds', 60))
            comment_loop = None
            if config.get('comments_enabled') or config.get('comment_replies_enabled'):
                notice_page, reply_page = context.new_page(), context.new_page()
                notice_page.goto('https://www.douyin.com/', wait_until='domcontentloaded', timeout=90000)
                if config.get('comment_replies_enabled'):
                    from social_bot.video_participation import ThreadRegistry
                    from social_bot.platforms.douyin.video_social import DouyinVideoInteraction
                    registry = ThreadRegistry(Path(config['memory_dir']) / 'video-social.sqlite3')
                    mentions = DouyinVideoInteraction(notice_page, reply_page, config['account_id'], config['self_user_id'],
                        registry, selectors=config.get('mentions_selectors'), permitted=permitted)
                else:
                    mentions = DOMMentions(notice_page, reply_page, config['account_id'], config['self_user_id'],
                                           selectors=config.get('mentions_selectors'), permitted=permitted)
                responder = SocialResponder(service, 'douyin', config['account_id'], config=config, reader=reader,
                                            permitted=permitted, scope_fallback=service.scope)
                source = lambda stream: mentions.poll() if stream == 'mentions' else mentions.poll_replies()
                comment_loop = PollLoop(responder, source, mentions.send, max_replies=config.get('max_replies_per_poll', 5))
            print('开始监听；首次扫描只建立基线，之后回复新的私信和已启用的@评论。Ctrl+C 停止。')
            print('消息检测：' + ('未读提示优先、预览变化触发、当前聊天持续读取；定时复查。'
                              if config.get('message_detection', 'unread') == 'unread' else '定时轮询全部已配置会话。'))
            while permitted():
                try:
                    loop.step()
                except (BridgeError, PlaywrightTimeoutError) as exc:
                    logging.warning('本轮私信未完成，保留队列，下一轮重新核验：%s', exc)
                if comment_loop:
                    try:
                        comment_loop.step([*(['mentions'] if config.get('comments_enabled') else []),
                                           *(['replies'] if config.get('comment_replies_enabled') else [])])
                    except (BridgeError, PlaywrightTimeoutError) as exc:
                        logging.warning('本轮评论未完成：%s', exc)
                if config.get('message_detection', 'unread') == 'unread':
                    try:
                        dom.wait_for_activity(config['poll_seconds'] * 1000)
                    except (BridgeError, PlaywrightTimeoutError) as exc:
                        logging.warning('页面变化监测暂不可用：%s', exc)
                        page.wait_for_timeout(config['poll_seconds'] * 1000)
                else:
                    page.wait_for_timeout(config['poll_seconds'] * 1000)
            print('配置或登录会话变化，已停止。')
        finally:
            if registry:
                registry.close()
            if service:
                service.close()
            context.close()


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('已停止。')
    except BridgeError as exc:
        print(str(exc))
        raise SystemExit(1)
    except Exception as exc:  # noqa: BLE001 - 页面异常需要保留类型，便于校准选择器
        print(f'启动或页面操作失败：{type(exc).__name__}: {exc}')
        print('请检查依赖、配置和专用浏览器是否被占用；未自动重试发送。')
        traceback.print_exc()
        raise SystemExit(1)

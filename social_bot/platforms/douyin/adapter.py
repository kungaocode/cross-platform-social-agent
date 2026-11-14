"""Conservative DOM bridge inspired by dsh-douyin-bridge's selector registry.

No hidden API interception, remote debugging listener or model-driven browser
tools. See douyin/THIRD_PARTY_LICENSE.txt for upstream attribution.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path

from ...scope import ScopeDecision, ScopeGuard
from ...service import BotService
from ...settings import load_settings
from ..common.chat import platform_settings, SocialTools
from ..common.tools import ReadOnlyChatTools

ITEM = '[data-e2e="conversation-item"], [class*="conversationConversationItemwrapper"]'
TITLE = '[data-e2e="conversation-title"], [class*="ConversationItemtitle"]:not([class*="titleWrapper"])'
BOX = ('[data-e2e="message-item"], div[class*="messageMessageBoxselectableRow"], '
       'div[class*="messageMessageBoxcontentBox"]')
INPUT = '[data-e2e="msg-input"] [contenteditable="true"], [data-e2e="msg-input"][contenteditable="true"], textarea[data-e2e="msg-input"]'
CHAT_URL = 'https://www.douyin.com/chat'

LOGGER = logging.getLogger(__name__)

_ZERO_WIDTH = re.compile('[\u200b\u200c\u200d\ufeff]')


def _editor_text(editor):
    """Slate 编辑器的空内容只有一个零宽字符，不能当作草稿。"""
    try:
        raw = editor.evaluate("el => el.value ?? el.innerText")
    except Exception as exc:  # noqa: BLE001 - never overwrite an unreadable draft
        raise BridgeError('输入框内容读取失败，不写入或发送') from exc
    return _ZERO_WIDTH.sub('', str(raw or '')).strip()


class BridgeError(RuntimeError):
    pass


class DouyinScope(ScopeGuard):
    """测试期抖音：所有会话都算测试对象；群仍需激活词与点名。

    私聊在 ChatLoop 枚举到的会话内一律放行；群聊沿用微信的
    ``group_command``/``group_active`` 语义，激活后才允许按名触发。
    """

    def __init__(self, settings, groups=()):
        super().__init__(settings)
        self.groups = frozenset(str(item).strip() for item in groups if str(item).strip())

    def group_is_allowed(self, group_id):
        return bool(self.settings.enabled) and (
            not self.groups or str(group_id).strip() in self.groups)

    def check(self, message):
        if not self.settings.enabled:
            return ScopeDecision(False, "scope_disabled")
        text = str(message.get("text", "")).strip()
        if not text:
            return ScopeDecision(False, "empty_message")
        if len(text) > self.settings.max_input_chars:
            return ScopeDecision(False, "message_too_long")
        if str(message.get("chat_type", "")).lower() == "private":
            return ScopeDecision(True, "douyin_private_allowed")
        return super().check(message)


def load_config(path):
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    from .backend import backend_options
    backend_options(raw, path)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', raw.get('account_id', '')):
        raise ValueError('account_id 只允许 1～40 位字母、数字、下划线或连字符')
    peers = raw.get('peers')
    if not isinstance(peers, list) or any(not isinstance(p, str) or not p.strip() for p in peers) or len(set(peers)) != len(peers):
        raise ValueError('peers 必须是无重复的联系人完整名称数组')
    if not isinstance(raw.get('all_peers', False), bool):
        raise ValueError('all_peers 必须是布尔值')
    groups = raw.get('group_names', [])
    if (not isinstance(groups, list) or any(not isinstance(value, str) or not value.strip() for value in groups)
            or len(set(groups)) != len(groups)):
        raise ValueError('group_names 必须是无重复的群会话完整名称数组')
    if not isinstance(raw.get('bot_name', ''), str) or len(str(raw.get('bot_name', '')).strip()) > 40:
        raise ValueError('bot_name 必须是 40 字以内的字符串')
    bot_name = str(raw.get('bot_name', '')).strip()
    max_peers = raw.get('max_peers', 20)
    if type(max_peers) is not int or not 1 <= max_peers <= 50:
        raise ValueError('max_peers 必须是 1～50 的整数')
    memory = Path(raw['memory_dir']).resolve()
    if not memory.is_dir():
        raise ValueError(f'记忆目录不存在：{memory}（外置盘未挂载或盘符变化）')
    if raw.get('browser_channel') not in ('msedge', 'chrome'):
        raise ValueError('browser_channel 必须是 msedge 或 chrome')
    if raw.get('browser_connection', 'system') not in ('system', 'direct'):
        raise ValueError('browser_connection 必须是 system 或 direct')
    if not isinstance(raw.get('enabled'), bool):
        raise ValueError('enabled 必须是布尔值')
    if raw.get('message_detection', 'unread') not in ('unread', 'poll'):
        raise ValueError('message_detection 必须是 unread 或 poll')
    if type(raw.get('full_scan_seconds', 60)) is not int or not 10 <= raw.get('full_scan_seconds', 60) <= 300:
        raise ValueError('full_scan_seconds 必须是 10～300 的整数')
    if not isinstance(raw.get('comments_enabled', False), bool):
        raise ValueError('comments_enabled 必须是布尔值')
    if not isinstance(raw.get('comment_replies_enabled', False), bool):
        raise ValueError('comment_replies_enabled 必须是布尔值')
    if (raw.get('comments_enabled') or raw.get('comment_replies_enabled')) and (not isinstance(raw.get('self_user_id'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', raw['self_user_id'])):
        raise ValueError('启用@评论回复需要配置自己个人主页链接中的 self_user_id')
    if not isinstance(raw.get('comment_senders', []), list) or any(not isinstance(value, str) or not value for value in raw.get('comment_senders', [])):
        raise ValueError('comment_senders 必须是用户 ID 字符串数组')
    if not isinstance(raw.get('mentions_selectors', {}), dict) or any(not isinstance(value, str) or not value for value in raw.get('mentions_selectors', {}).values()):
        raise ValueError('mentions_selectors 必须为 CSS 选择器字符串字典')
    raw['poll_seconds'] = max(1, min(60, int(raw.get('poll_seconds', 3))))
    raw['all_peers'] = bool(raw.get('all_peers', False))
    raw['group_names'] = list(groups)
    raw['bot_name'] = bot_name
    raw['max_peers'] = max_peers
    raw['memory_dir'] = str(memory)
    raw['base_config'] = str((path.parent / raw['base_config']).resolve())
    return raw


def browser_args(config):
    """Apply connection routing to this dedicated browser only."""
    from ...network_policy import BROWSER_ARGS
    return list(BROWSER_ARGS)


def build_service(config, client=None):
    settings = load_settings(config['base_config'])
    settings = platform_settings(settings, 'douyin', config,
                                 Path(config['memory_dir']) / f"douyin-{config['account_id']}.sqlite3",
                                 group_activation=True)
    return BotService(settings, client=client)


def signature(message):
    # Prefer a platform DOM message ID. Without one, identical text is deliberately
    # treated conservatively; never fabricate a timestamp from polling time.
    return str(message.get('id') or hashlib.sha256(json.dumps(
        [message.get('self'), message.get('text'), message.get('type'), [(m.get('kind'), m.get('url', '').split('?')[0]) for m in message.get('media', ())]], ensure_ascii=False).encode()).hexdigest())


def _pick_incoming(messages):
    return [m for m in messages
            if m.get('self') is False and m.get('type') in {'text', 'image', 'video', 'media'}
            and (m.get('text', '').strip() or m.get('media'))]


def fresh_by_signatures(previous, current):
    """previous 为已保存的签名列表；返回对齐之后的新消息。

    无法与旧基线对齐时返回 ``None``，调用方必须保留旧基线而不是覆盖，
    否则一次渲染抖动就会把真实新消息永久吞掉。
    """
    if previous is None:
        return []
    if not previous:
        return _pick_incoming(current)
    new = [signature(m) for m in current]
    for size in range(min(len(previous), len(new)), 0, -1):
        if previous[-size:] == new[:size]:
            return _pick_incoming(current[size:])
    # 列表中间出现差异（例如表情/卡片状态变化）时，用旧基线最后一条做锚点。
    anchor = previous[-1]
    for index in range(len(new) - 1, -1, -1):
        if new[index] == anchor:
            return _pick_incoming(current[index + 1:])
    return None


def fresh_messages(previous, current):
    """用“旧列表尾部对齐新列表头部”的增量方式判断新消息。

    抖音气泡没有稳定 ID，重复文本无法靠签名区分；按位置对齐可以处理
    连续相同文字，同时列表被重排或滑动到无法对齐时只重建基线、不猜。
    """
    result = fresh_by_signatures([signature(m) for m in previous] if previous is not None else None, current)
    return [] if result is None else result


class DOMChat:
    def __init__(self, page):
        self.page = page
        self.permitted = lambda: True

    def origin(self):
        if not self.page.url.startswith('https://www.douyin.com/'):
            raise BridgeError('抖音页面已离开，停止处理')
        if re.search('验证码|安全验证|访问受限', self.page.title()):
            raise BridgeError('抖音要求人工安全验证，请在专用浏览器完成验证后继续')

    def items(self):
        self.origin()
        return self.page.locator(ITEM).filter(visible=True)

    def open_panel(self):
        """Bound missing-list waits; distinguish a verification page from an empty inbox."""
        self.origin()
        if self.items().count():
            return
        if self.page.url.split('?')[0].rstrip('/') != CHAT_URL:
            self.page.goto(CHAT_URL, wait_until='domcontentloaded', timeout=90000)
            self.page.wait_for_timeout(1500)
        for attempt in (0, 1):
            for _ in range(20):
                self.origin()
                if self.items().count():
                    return
                if self.page.get_by_text('暂无会话').filter(visible=True).count():
                    raise BridgeError('消息面板为空（暂无会话）：请先用测试账号给本账号发一条私信，再运行 list')
                self.page.wait_for_timeout(500)
            if attempt == 0:
                LOGGER.warning('消息列表 10 秒内未渲染，刷新页面后重试')
                self.page.reload(wait_until='domcontentloaded', timeout=20000)
                self.page.wait_for_timeout(500)
        if self.page.get_by_text('暂无会话').filter(visible=True).count():
            raise BridgeError('消息面板为空（暂无会话）：请先用测试账号给本账号发一条私信，再运行 list')
        raise BridgeError('消息页面未就绪（列表/SDK未加载）；不作为无新消息，不更新识别基线')

    def names(self):
        self.open_panel()
        return [text for text in self.items().evaluate_all(
            '''(items, title) => items.map(el => ((el.querySelector(title)?.innerText || '').trim()))''', TITLE)]

    def conversation_states(self):
        self.open_panel()
        script = Path(__file__).with_name('conversation_state.js').read_text(encoding='utf-8')
        return self.items().evaluate_all(script, {'title': TITLE})

    def wait_for_activity(self, timeout_ms):
        """Wake on visible conversation/message mutations, with a bounded fallback."""
        self.origin()
        return self.page.evaluate("""({item, box, timeout}) => new Promise(resolve => {
            const roots = new Set([...document.querySelectorAll(item+','+box)].map(el=>el.parentElement).filter(Boolean));
            let timer;
            const observer = new MutationObserver(() => { observer.disconnect(); clearTimeout(timer); resolve(true); });
            for (const root of roots) observer.observe(root, {subtree:true, childList:true, characterData:true,
                attributes:true, attributeFilter:['class','style','aria-label','data-unread-count']});
            timer = setTimeout(() => { observer.disconnect(); resolve(false); }, timeout);
        })""", {'item': ITEM, 'box': BOX, 'timeout': max(100, int(timeout_ms))})

    def select(self, peer):
        last_error = None
        for attempt in (0, 1):
            names = self.names()
            if names.count(peer) != 1:
                raise BridgeError('目标会话不存在或有同名联系人，停止处理')
            try:
                self.verify(peer)
                return  # Keep the current conversation open; no repeated clicks.
            except BridgeError:
                pass
            try:
                self.items().nth(names.index(peer)).click()
                self.page.wait_for_timeout(250)
                self.verify(peer)
                return
            except Exception as exc:  # noqa: BLE001 - 页面重绘时只重试一次
                last_error = exc
                if attempt == 0:
                    LOGGER.warning('选择会话未确认，重试一次 chat=%s error=%s: %s',
                                   peer, type(exc).__name__, exc)
                    self.page.wait_for_timeout(800)
        if isinstance(last_error, BridgeError):
            raise last_error
        raise BridgeError('选择会话失败：' + type(last_error).__name__) from None

    def verify(self, peer):
        self.origin()
        # 新版选中态是会话项自身的 curConversation 类。
        active = self.items().evaluate_all('''(items, title) => items.filter(el =>
          [el, el.firstElementChild].filter(Boolean).some(x =>
            x.getAttribute('aria-selected') === 'true' || /active|selected|curConversation/i.test(String(x.className))))
          .map(el => (el.querySelector(title)?.innerText || '').trim())''', TITLE)
        if active != [peer]:
            raise BridgeError('无法唯一确认当前聊天对象；不读取或发送')

    def read(self, peer):
        self.verify(peer)
        script = Path(__file__).with_name('read_messages.js').read_text(encoding='utf-8')
        return self.page.locator(BOX).filter(visible=True).evaluate_all(script)

    def resolve_inline(self, reference):
        self.origin()
        if not re.fullmatch(r'dom-image:[a-f0-9-]{36}', reference):
            raise BridgeError('图片引用无效')
        return self.page.evaluate("""async ref => {
            const images = [...document.querySelectorAll('img')].filter(img => img.dataset.socialMediaRef === ref);
            if(images.length !== 1) throw Error('image no longer visible');
            const url = images[0].currentSrc || images[0].src;
            if(url.startsWith('data:image/')) {
                if(url.length > 17*1024*1024) throw Error('image too large');
                return url;
            }
            if(!url.startsWith('blob:') || new URL(url.slice(5)).origin !== location.origin) throw Error('invalid image reference');
            const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 15000);
            try {
                const response = await fetch(url, {signal: controller.signal}); const blob = await response.blob();
                if(blob.size > 12*1024*1024) throw Error('image too large');
                return await new Promise((resolve,reject) => {const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = reject; reader.readAsDataURL(blob);});
            } finally { clearTimeout(timer); }
        }""", reference.removeprefix('dom-image:'))

    def send(self, peer, text):
        if not self.permitted():
            raise BridgeError('发送已停用')
        self.verify(peer)
        box = self.page.locator(INPUT).filter(visible=True)
        if box.count() != 1:
            raise BridgeError('输入框不唯一')
        if _editor_text(box):
            raise BridgeError('输入框已有草稿，停止发送')
        before = sum(m['self'] and m['text'] == text for m in self.read(peer))
        # 不点击输入框：直接聚焦后用键盘写入，避免被浮层拦截。
        box.focus()
        self.page.keyboard.insert_text(text)
        self.verify(peer)
        if _editor_text(box) != text:
            raise BridgeError('输入内容校验失败')
        if not self.permitted():
            raise BridgeError('发送已停用；输入内容保留为草稿')
        self.page.keyboard.press('Enter')
        if self._await_new_bubble(peer, text, before):
            return
        if _editor_text(box):
            self._click_send_button()
            if self._await_new_bubble(peer, text, before):
                return
        raise BridgeError('未确认新发送气泡；不自动重发')

    def _await_new_bubble(self, peer, text, before):
        for _ in range(5):
            self.page.wait_for_timeout(700)
            self.verify(peer)
            if sum(m['self'] and m['text'] == text for m in self.read(peer)) > before:
                return True
        return False

    def _click_send_button(self):
        """回车未生效时，用页面自带的发送箭头兜底。"""
        button = self.page.locator('.e2e-send-msg-btn').filter(visible=True)
        if button.count() != 1:
            return
        button.evaluate("el => el.dispatchEvent(new MouseEvent('click', {bubbles: true}))")


class ChatLoop:
    def __init__(self, service, dom, peers, permitted=lambda: True, *, media_reader=None,
                 all_peers=False, group_names=(), bot_name='', max_peers=20,
                 detection_mode='unread', full_scan_seconds=60, clock=time.monotonic):
        self.service, self.dom, self.permitted = service, dom, permitted
        self.peers = list(peers)
        self.all_peers = bool(all_peers)
        self.groups = frozenset(str(name).strip() for name in group_names if str(name).strip())
        self.bot_name = str(bot_name or '').strip()
        self.max_peers = max(1, min(50, int(max_peers or 20)))
        self.detection_mode, self.full_scan_seconds, self.clock = detection_mode, full_scan_seconds, clock
        self.row_hints, self.last_scan, self.retry_peers = {}, {}, set()
        self._heartbeat_at = float('-inf')
        # 私聊放行、群沿用激活词与点名：ScopeGuard 的群语义在这里直接复用。
        self.service.scope = DouyinScope(service.settings.scope, self.groups)
        runtime = service.settings.runtime
        self.shared_tools = ReadOnlyChatTools(runtime, '') if runtime.tool_calling_enabled else None
        self.media_reader = media_reader
        self.db = service.store.connection
        self.db.execute('CREATE TABLE IF NOT EXISTS douyin_deliveries (id TEXT PRIMARY KEY, state TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS douyin_seen '
                        '(peer TEXT PRIMARY KEY, signatures TEXT NOT NULL, updated_at TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS douyin_pending '
                        '(id TEXT PRIMARY KEY, peer TEXT NOT NULL, payload TEXT NOT NULL, reply TEXT)')
        self.db.commit()
        # 基线持久化：重启后从上次看到的位置继续，停机期间的新消息不会被当成历史吞掉。
        self.previous = self._load_seen()

    def _load_seen(self):
        seen = {}
        for peer, payload in self.db.execute('SELECT peer, signatures FROM douyin_seen'):
            try:
                values = json.loads(payload)
            except ValueError:
                continue
            if isinstance(values, list):
                seen[str(peer)] = [str(item) for item in values]
        return seen

    def _save_seen(self, peer, signatures):
        self.db.execute('''INSERT INTO douyin_seen(peer, signatures, updated_at)
                           VALUES (?, ?, CURRENT_TIMESTAMP)
                           ON CONFLICT(peer) DO UPDATE SET
                             signatures=excluded.signatures, updated_at=CURRENT_TIMESTAMP''',
                        (peer, json.dumps(signatures, ensure_ascii=False)))
        self.db.commit()

    def conversations(self):
        if not self.all_peers:
            return self.peers
        names = [name for name in self.dom.names() if name]
        return names[:self.max_peers]

    def targets(self):
        if self.detection_mode == 'poll':
            return self.conversations()
        snapshot = self.dom.conversation_states()
        # Older adapters have no badge metadata: keep the original safe scan path.
        if not isinstance(snapshot, list) or any(not isinstance(row, dict) or not row.get('name') for row in snapshot):
            return self.conversations()
        names = [row['name'] for row in snapshot]
        if len(names) != len(set(names)):
            raise BridgeError('会话列表出现同名联系人，不按未读提示猜测对象')
        peers = names[:self.max_peers] if self.all_peers else self.peers
        rows = {row['name']: row for row in snapshot}
        pending = {row[0] for row in self.db.execute(
            'SELECT DISTINCT p.peer FROM douyin_pending p JOIN douyin_deliveries d ON p.id=d.id '
            "WHERE d.state IN ('queued','prepared','processing')")}
        now = self.clock()
        chosen = []
        for peer in peers:
            row = rows.get(peer, {})
            if (row.get('unread') or row.get('active') or peer in pending or peer in self.retry_peers
                    or peer not in self.last_scan or now-self.last_scan[peer] >= self.full_scan_seconds
                    or row.get('hint', '') != self.row_hints.get(peer, '')):
                chosen.append(peer)
        chosen.sort(key=lambda peer: not rows.get(peer, {}).get('unread', False))
        self._attention_rows = rows
        return chosen

    def state(self, key, state):
        self.db.execute('UPDATE douyin_deliveries SET state=? WHERE id=?', (state, key))
        self.db.commit()

    @staticmethod
    def label_senders(messages):
        """群消息按昵称排序；连续气泡缺昵称时沿用上一条的发言人。"""
        labels, last = {}, ''
        for message in messages:
            sender = str(message.get('sender') or '').strip()
            if sender:
                last = sender
            labels[id(message)] = last
        return labels

    def step(self):
        peers = self.targets()
        if peers:
            LOGGER.debug('检查 %d 个会话: %s', len(peers), peers)
        for peer in peers:
            if not self.permitted():
                raise BridgeError('配置已停用或账号登录状态变化')
            self.retry_peers.add(peer)
            row = getattr(self, '_attention_rows', {}).get(peer, {})
            if row.get('unread'):
                LOGGER.info('未读提示触发 chat=%s count=%s', peer, row.get('unread_count', 0))
            self.dom.select(peer)
            current = self.dom.read(peer)
            fresh = fresh_by_signatures(self.previous.get(peer), current)
            aligned = fresh is not None
            if fresh is None:
                LOGGER.warning('消息列表与基线对不上，保留旧基线等下一轮 chat=%s', peer)
                fresh = []
            else:
                signatures = [signature(m) for m in current]
                labels = self.label_senders(current)
                # Queue and cursor must commit together; a crash cannot swallow later bubbles.
                with self.db:
                    for message in fresh:
                        key = json.dumps(['douyin', peer, message.get('id') or uuid.uuid4().hex], ensure_ascii=False)
                        self.db.execute('INSERT OR IGNORE INTO douyin_deliveries VALUES (?, ?)', (key, 'queued'))
                        self.db.execute('INSERT OR IGNORE INTO douyin_pending VALUES (?, ?, ?, NULL)',
                                        (key, peer, json.dumps({**message, 'sender': labels.get(id(message)) or ''}, ensure_ascii=False)))
                    if signatures != self.previous.get(peer):
                        self.db.execute('INSERT INTO douyin_seen VALUES (?, ?, CURRENT_TIMESTAMP) '
                                        'ON CONFLICT(peer) DO UPDATE SET signatures=excluded.signatures, updated_at=CURRENT_TIMESTAMP',
                                        (peer, json.dumps(signatures)))
                self.previous[peer] = signatures
            labels = self.label_senders(current)
            group = peer in self.groups
            pending = self.db.execute('SELECT p.id,p.payload,p.reply FROM douyin_pending p '
                                     'JOIN douyin_deliveries d ON p.id=d.id '
                                     "WHERE p.peer=? AND d.state IN ('queued','prepared','processing') ORDER BY p.rowid", (peer,)).fetchall()
            for key, payload_json, prepared_reply in pending:
                message = json.loads(payload_json)
                text = message.get('text', '').strip() or '[图片或视频消息，请读取后回复]'
                if len(text) > self.service.settings.scope.max_input_chars:
                    self.state(key, 'failed')
                    continue
                sender = peer
                if group:
                    # Carry the inherited sender into the durable queue too.
                    sender = message.get('sender') or peer
                LOGGER.info('新消息 chat=%s group=%s sender=%s text=%r', peer, group, sender, text[:40])
                self.state(key, 'processing')
                if self.service.paused:
                    self.state(key, 'cancelled')
                    continue
                payload = dict(platform='douyin', chat_type='group' if group else 'private',
                               chat_id=peer, sender_id=sender,
                               sender_name=sender if group else '',
                               message_id=key, text=text, memory_text=message.get('text') or '[媒体消息]',
                               is_mentioned=bool(self.bot_name) and self.bot_name in text)
                if prepared_reply:
                    result = {'accepted': True, 'reply': prepared_reply}
                elif message.get('media') and not self.service.settings.runtime.tool_calling_enabled:
                    result = {'accepted': True, 'reply': '图片和视频读取当前已关闭，暂时无法确认内容。'}
                elif self.shared_tools:
                    tools = SocialTools(self.service.settings.runtime, text, reader=self.media_reader,
                                        media=message.get('media', ()), resolve_inline=self.dom.resolve_inline, shared=self.shared_tools)
                    result = self.service.handle(payload, tool_executor=tools) if tools.definitions else self.service.handle(payload)
                else:
                    result = self.service.handle(payload)
                if not result.get('accepted') or not result.get('reply'):
                    LOGGER.info('未回复 chat=%s reason=%s', peer, result.get('reason'))
                    self.state(key, 'failed')
                    continue
                reply = result['reply']
                if len(reply) > 1000:
                    reply = reply[:980] + '\n（回复已截断）'
                with self.db:
                    self.db.execute('UPDATE douyin_pending SET reply=? WHERE id=?', (reply, key))
                    self.db.execute("UPDATE douyin_deliveries SET state='prepared' WHERE id=?", (key,))
                if not self.permitted():
                    self.state(key, 'cancelled')
                    raise BridgeError('发送前已停用或账号变化')
                self.state(key, 'sending')
                try:
                    self.dom.send(peer, reply)
                except BaseException:
                    self.state(key, 'unknown')
                    raise
                self.state(key, 'sent')
                LOGGER.info('已发送 chat=%s chars=%d', peer, len(reply))
            self.last_scan[peer] = self.clock()
            self.row_hints[peer] = row.get('hint', '')
            if aligned:
                self.retry_peers.discard(peer)
        if self.clock()-self._heartbeat_at >= 60:
            rows = getattr(self, '_attention_rows', {})
            LOGGER.info('消息识别运行 mode=%s visible=%d checked=%d unread=%d fallback_seconds=%d',
                        self.detection_mode, len(rows) if rows else len(peers), len(peers),
                        sum(bool(row.get('unread')) for row in rows.values()), self.full_scan_seconds)
            self._heartbeat_at = self.clock()

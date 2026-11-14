"""Visible web comment publishing for the API transport.

Private chat stays on the backend API; only the comment page is driven through a real
browser session, so a platform verification challenge can be seen (and is reported)
instead of being hit by an anonymous HTTP request.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from .adapter import BridgeError, browser_args
from .comments import DEFAULT_SELECTORS

VIDEO_URL = re.compile(r'https://www\.douyin\.com/video/([1-9]\d{0,24})')
VERIFY_TEXT = re.compile('请完成验证|请输入验证码|拖动滑块|安全验证')
PUBLISH_NAMES = re.compile(r'^(?:发送|发布|发表)$')


class BrowserCommentPublisher:
    def __init__(self, config, account, *, permitted=lambda: True, selectors=None, headless=False):
        self.config, self.account = config, str(account)
        self.permitted = permitted
        self.selectors = {**DEFAULT_SELECTORS, **(selectors or {})}
        self.profile = Path(config['memory_dir']) / ('douyin-profile-' + config['account_id'])
        self.headless = bool(headless)
        self._pw = self.context = self.page = None
        self._avatar = {}

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    def open(self):
        from playwright.sync_api import sync_playwright
        if not self.profile.is_dir():
            raise BridgeError('抖音评论浏览器资料目录不存在，未发布')
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(str(self.profile),
            channel=self.config.get('browser_channel', 'msedge'), headless=self.headless,
            viewport={'width': 1440, 'height': 1000},
            args=['--window-size=1440,1000', *browser_args(self.config)])
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def close(self):
        try:
            if self.context:
                self.context.close()
        finally:
            if self._pw:
                self._pw.stop()
            self.context = self.page = self._pw = None

    def _guard(self):
        if not self.permitted():
            raise BridgeError('评论发布已停用')
        if urlsplit(self.page.url).hostname != 'www.douyin.com':
            raise BridgeError('评论页面已离开抖音')

    def _check_login(self):
        cookies = self.context.cookies('https://www.douyin.com/')
        if not any(c['name'] in ('sessionid', 'sessionid_ss') and c.get('value') for c in cookies):
            raise BridgeError('抖音评论浏览器尚未登录，不发布')

    @staticmethod
    def _avatar_key(url):
        name = urlsplit(url or '').path.rsplit('/', 1)[-1].split('~')[0].rsplit('.', 1)[0]
        return name if re.fullmatch(r'[A-Za-z0-9_-]{10,200}', name) else ''

    def _avatar_keys(self, target):
        handle = str(target.get('sec_uid') or target.get('uid') or '')
        if handle in self._avatar:
            return self._avatar[handle]
        page = self.context.new_page()
        try:
            page.goto('https://www.douyin.com/user/' + handle, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(3000)
            info = page.locator('[data-e2e="user-info"]')
            info.wait_for(state='visible', timeout=15000)
            if info.inner_text().strip().splitlines()[0].strip() != target['name']:
                raise BridgeError('@目标主页昵称与绑定账号不一致，不发布')
            sources = page.locator('img[src*="aweme-avatar"]').evaluate_all(
                'els=>els.map(e=>e.currentSrc||e.src)')
            keys = {self._avatar_key(source) for source in sources} - {''}
        finally:
            page.close()
        if not keys:
            raise BridgeError('@目标头像无法核验，不发布')
        self._avatar[handle] = keys
        return keys

    def _editor(self):
        scope = self.page.locator(self.selectors['input']).filter(visible=True)
        if scope.count() == 0:
            icon = self.page.locator('[data-e2e="feed-comment-icon"]').filter(visible=True)
            if icon.count() == 1:
                icon.click()
                self.page.wait_for_timeout(1200)
                scope = self.page.locator(self.selectors['input']).filter(visible=True)
        if scope.count() != 1:
            raise BridgeError('顶层评论输入区不唯一')
        editor = scope.locator(self.selectors['editor']).filter(visible=True)
        if editor.count() == 0:
            scope.click(position={'x': 5, 'y': 5})
            self.page.wait_for_timeout(600)
            editor = scope.locator(self.selectors['editor']).filter(visible=True)
        if editor.count() != 1:
            raise BridgeError('顶层评论编辑框不可用')
        if editor.evaluate('el=>(el.value??el.innerText).trim()'):
            raise BridgeError('评论框已有草稿，不覆盖')
        return scope, editor

    def _choose_mention(self, editor, target):
        name = target['name']
        editor.click()
        self.page.wait_for_timeout(200)
        self.page.keyboard.insert_text('@' + name)
        self.page.wait_for_timeout(1800)
        keys = self._avatar_keys(target)
        rows = self.page.locator('div.dTwsCivh').filter(visible=True)
        chosen = None
        exact = 0
        for index in range(rows.count()):
            row = rows.nth(index)
            if row.inner_text().strip() != name:
                continue
            exact += 1
            image = row.locator('img')
            source = image.first.get_attribute('src') if image.count() else ''
            if self._avatar_key(source) in keys:
                chosen = row
                break
        if chosen is None and exact == 1:
            chosen = next(rows.nth(i) for i in range(rows.count())
                          if rows.nth(i).inner_text().strip() == name)
        if chosen is None:
            raise BridgeError('未唯一识别到@候选，保留草稿不发布')
        chosen.click()
        self.page.wait_for_timeout(600)
        token = editor.locator('span.douyin_mention_word').filter(has_text='@' + name)
        if token.count() != 1 or token.first.inner_text().strip() != '@' + name:
            raise BridgeError('编辑器未生成可核验的@实体，不发布')

    def _own_comment_ids(self, text):
        return self.page.locator(self.selectors['comment']).filter(visible=True).evaluate_all("""(items, value) => items.filter(el => {
            const content = el.querySelector('[data-e2e="comment-content"]') || el.querySelector('.comment-item-info-wrap')?.nextElementSibling;
            const same = (content?.innerText || '').replace(/\\s+/g,' ').trim() === value;
            const authored = [...el.querySelectorAll('a[href]')].some(a => {
                try { return new URL(a.href).pathname.startsWith('/user/'); } catch (e) { return false; }
            });
            return same && authored;
        }).map(el => el.dataset.commentId || el.dataset.cid || el.querySelector('[id^="tooltip_"]')?.id.replace(/^tooltip_/,'') || el.id).filter(Boolean)""",
            re.sub(r'\s+', ' ', text).strip())

    def publish(self, url, text, target, *, dry_run=False):
        self._submitted = False
        try:
            return self._publish(url, text, target, dry_run=dry_run)
        except Exception as exc:
            exc.before_write = not self._submitted
            raise

    def _publish(self, url, text, target, *, dry_run=False):
        match = VIDEO_URL.fullmatch(str(url))
        if not match:
            raise BridgeError('评论视频地址无效')
        name = str(target.get('name') or '')
        prefix = '@' + name + ' '
        if not name or not isinstance(text, str) or not text.startswith(prefix):
            raise BridgeError('评论正文缺少绑定的@前缀')
        if len(text) > 180:
            raise BridgeError('评论正文超过平台长度限制')
        if not self.permitted():
            raise BridgeError('评论发布已停用')
        page = self.page
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(2500)
        self._guard()
        if not re.fullmatch(r'/video/[1-9]\d{0,24}/?', urlsplit(page.url).path):
            raise BridgeError('评论页不是视频页（可能是直播或跳转页），不发布')
        self._check_login()
        scope, editor = self._editor()
        self._choose_mention(editor, target)
        editor.evaluate("""el => { const range=document.createRange(); range.selectNodeContents(el);
            range.collapse(false); const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range); }""")
        page.keyboard.insert_text(text[len(prefix):])
        content = editor.evaluate("el=>(el.value??el.innerText).replace(/\\s+/g,' ').trim()")
        if content != re.sub(r'\s+', ' ', text).strip():
            raise BridgeError('含@的评论正文校验失败')
        if dry_run:
            editor.evaluate("el=>{el.focus(); const r=document.createRange(); r.selectNodeContents(el); const s=getSelection(); s.removeAllRanges(); s.addRange(r);}")
            page.keyboard.press('Backspace')
            return dict(dry_run=True, video_id=match[1], content=content)
        before = set(self._own_comment_ids(text))
        # The visible arrow is layered over a red circle; click the wrapping control with a real
        # mouse event and skip actionability so the white path cannot intercept the click.
        control = scope.locator('span:has(svg path[fill="#FE2C55"])').filter(visible=True)
        if control.count() == 1:
            self._submitted = True
            control.click(force=True)
        else:
            button = scope.get_by_role('button', name=PUBLISH_NAMES).filter(visible=True)
            if button.count() != 1:
                raise BridgeError('评论发布按钮不唯一')
            self._submitted = True
            button.click(force=True)
        for _ in range(30):
            page.wait_for_timeout(500)
            self._guard()
            if page.get_by_text(VERIFY_TEXT).filter(visible=True).count():
                self._snapshot('verify')
                raise BridgeError('评论发布遇到平台验证，需要人工处理；不自动重发')
            created = {item for item in self._own_comment_ids(text)} - before
            if len(created) == 1:
                root = created.pop()
                root = re.sub(r'^(?:comment_)?', '', root)
                if not re.fullmatch(r'[1-9]\d*', root):
                    raise BridgeError('新评论 ID 无法确认')
                return dict(root_id=root, video_id=match[1], type=1, transport='browser',
                            mention_uid=target.get('uid'), mention_name=name, mention_confirmed=True)
            if len(created) > 1:
                raise BridgeError('出现多条匹配评论，结果未知，不自动重发')
        self._snapshot('timeout')
        raise BridgeError('未确认新评论已出现，不自动重发')

    def _snapshot(self, tag):
        try:
            Path('reports').mkdir(exist_ok=True)
            self.page.screenshot(path=f'reports/douyin-comment-{tag}.png')
        except Exception:
            pass

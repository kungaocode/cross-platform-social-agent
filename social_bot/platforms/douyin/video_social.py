"""Visible recommendation comments and reply notifications; no hidden Douyin API."""
from __future__ import annotations

import re
import hashlib
from dataclasses import replace
from urllib.parse import urlsplit, urljoin

from .adapter import BridgeError
from .comments import DOMMentions, normalize_mention


class DouyinVideoInteraction(DOMMentions):
    def __init__(self, notice_page, reply_page, account, self_user_id, registry, *,
                 selectors=None, permitted=lambda: True, mention_target=None):
        super().__init__(notice_page, reply_page, account, self_user_id, selectors=selectors, permitted=permitted)
        self.registry = registry
        self.mention_target = mention_target
        self._publishing = False
        self._identity_stamp = None
        self._identity_page = None
        self._owner_avatar = None
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", self_user_id):
            raise BridgeError("刷视频评论需要配置自己的 self_user_id")
        self.selectors.setdefault("self_profile", 'header a[href*="/user/{uid}"], [data-e2e="user-avatar"] a[href*="/user/{uid}"], a[href*="/user/self"]:has([data-e2e="live-avatar"])')

    def _verify_own_account(self):
        self.origin(self.reply_page)
        cookies = self.reply_page.context.cookies("https://www.douyin.com/")
        if not any(c["name"] in ("sessionid", "sessionid_ss") and c.get("value") for c in cookies):
            raise BridgeError("抖音聊天/评论浏览器尚未登录")
        profile = self.reply_page.locator(self.selectors["self_profile"].replace("{uid}", self.self_user_id)).filter(visible=True)
        if profile.count() == 0:
            try:
                profile.first.wait_for(state='visible',timeout=5000)
            except Exception:
                pass
        if profile.count() == 0:
            raise BridgeError("未唯一确认当前个人主页入口，请校准 self_profile；不发布")
        urls = [urlsplit(urljoin(self.reply_page.url, href)) for href in profile.evaluate_all("els=>els.map(e=>e.getAttribute('href')||'')")]
        if any(url.hostname!='www.douyin.com' or url.path.rstrip('/') not in ('/user/self','/user/'+self.self_user_id) for url in urls):
            raise BridgeError('当前个人主页账号不匹配')
        actual = next((url for url in urls if url.path.rstrip('/')=='/user/self'),urls[0])
        if actual.hostname == 'www.douyin.com' and actual.path.rstrip('/') == '/user/self':
            # New navigation uses /user/self. Compare the rendered unique Douyin IDs
            # of that signed-in profile and the configured canonical profile.
            stamp = hashlib.sha256(repr(sorted((c['name'],c['value']) for c in cookies
                if c['name'] in ('sessionid','sessionid_ss'))).encode()).hexdigest()
            if self._identity_stamp != stamp:
                identities = []
                for uid in ('self', self.self_user_id):
                    page = self._profile_page(uid)
                    info = page.locator('[data-e2e="user-info"]')
                    info.wait_for(state='visible',timeout=15000)
                    page.wait_for_function("()=>/抖音号[：:]\\s*\\S+/.test(document.querySelector('[data-e2e=\"user-info\"]')?.innerText||'')",timeout=15000)
                    match = re.search(r'抖音号[：:]\s*(\S+)',info.inner_text())
                    identities.append(match[1] if match else '')
                if not identities[0] or identities[0] != identities[1]:
                    raise BridgeError('当前登录账号与配置的评论账号不一致')
                self._identity_stamp = stamp
            return
        if actual.hostname != "www.douyin.com" or actual.path.rstrip("/") != "/user/" + self.self_user_id:
            raise BridgeError("当前个人主页账号不匹配")

    def _profile_page(self, uid):
        if self._identity_page is None or self._identity_page.is_closed():
            self._identity_page = self.reply_page.context.new_page()
        self._identity_page.goto('https://www.douyin.com/user/'+uid,wait_until='domcontentloaded',timeout=30000)
        return self._identity_page

    def _owner_avatar_key(self):
        if self._owner_avatar:
            return self._owner_avatar
        page = self._profile_page(self.mention_target['uid'])
        info = page.locator('[data-e2e="user-info"]')
        info.wait_for(state='visible',timeout=15000)
        info.get_by_text(self.mention_target['name'],exact=True).first.wait_for(state='visible',timeout=15000)
        if info.inner_text().strip().splitlines()[0] != self.mention_target['name']:
            raise BridgeError('主人主页昵称与绑定账号不一致')
        source = info.evaluate("el=>{for(let n=el,i=0;n&&i<4;n=n.parentElement,i++){const img=n.querySelector('img');if(img)return img.currentSrc||img.src;}return ''}")
        key = urlsplit(source).path.rsplit('/',1)[-1].split('~')[0].rsplit('.',1)[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,200}',key):
            raise BridgeError('主人主页头像无法核验，不按同名候选猜测')
        self._owner_avatar = key
        return key

    def _target(self, event):
        self._verify_own_account()
        target = super()._target(event)
        if event.reply_to_own:
            root = event.target.get("root_id", "")
            if not self.registry.owns("douyin", self.account, root):
                raise BridgeError("该评论楼不属于本程序")
            actual = target.evaluate("el=>el.getAttribute('data-root-id')||el.closest('[data-root-id]')?.getAttribute('data-root-id')||''")
            if actual != root:
                raise BridgeError("原评论所属楼无法确认，不回复")
        return target

    def publish(self, url, text):
        self._submission_attempted = False
        try:
            return self._publish(url, text)
        except Exception as exc:
            exc.before_write = not self._submission_attempted
            raise

    def _publish(self, url, text):
        parsed = urlsplit(url)
        match = re.fullmatch(r"/video/([1-9]\d*)/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname != "www.douyin.com" or not match:
            raise BridgeError("评论视频地址无效")
        if not self.permitted() or not isinstance(text, str) or not 0 < len(text.strip()) <= 180:
            raise BridgeError("视频评论未授权或正文无效")
        page = self.reply_page
        # Refuse to discard a draft on the previous video.
        existing = page.locator(self.selectors["editor"]).filter(visible=True)
        if any(existing.nth(i).evaluate("el=>(el.value??el.innerText).trim()") for i in range(existing.count())):
            raise BridgeError("已有评论草稿，不切换或覆盖")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        self._verify_own_account()
        if self.mention_target and self.mention_target['uid'].startswith('MS4'):
            self._owner_avatar_key()
        scope = page.locator(self.selectors["input"]).filter(visible=True)
        if not scope.count():
            icon = page.locator('[data-e2e="feed-comment-icon"]').filter(visible=True)
            if icon.count() == 1:
                icon.click()
                page.wait_for_timeout(500)
            scope = page.locator(self.selectors["input"]).filter(visible=True)
        if scope.count() != 1:
            raise BridgeError("顶层评论输入区不唯一")
        editor = scope.locator(self.selectors["editor"]).filter(visible=True)
        if not editor.count():
            scope.click(position={"x": 5, "y": 5})
            editor = scope.locator(self.selectors["editor"]).filter(visible=True)
        if editor.count() != 1 or editor.evaluate("el=>(el.value??el.innerText).trim()"):
            raise BridgeError("顶层评论编辑框不可用或已有草稿")

        def verify():
            self._verify_own_account()
            if (page.url.split("?", 1)[0].rstrip("/") != url.split("?", 1)[0].rstrip("/")
                    or scope.get_attribute("data-reply-to") not in (None, "", "0")
                    or (editor.get_attribute("placeholder") or editor.get_attribute("data-placeholder") or "").startswith("回复")
                    or scope.get_by_text("取消回复", exact=True).filter(visible=True).count()):
                raise BridgeError("输入框绑定了回复对象，不发顶层评论")

        verify()
        self._publishing = True
        try:
            root = self._submit_text(scope, editor, text, set(self._own_replies(text)), verify)
        finally:
            self._publishing = False
        root_match = re.fullmatch(r"(?:comment_)?([1-9]\d*)", str(root))
        if not root_match:
            raise BridgeError("新评论 ID 无法确认")
        receipt = dict(root_id=root_match[1], video_id=match[1], type=1)
        if self.mention_target:
            receipt.update(mention_uid=self.mention_target['uid'], mention_name=self.mention_target['name'],
                           mention_confirmed=self._visible_mention(root_match[1]))
        return receipt

    def _visible_mention(self, root):
        uid = self.mention_target['uid']
        row = self.reply_page.locator(self.selectors['comment']).filter(visible=True).and_(
            self.reply_page.locator(f'[data-comment-id="{root}"], [data-cid="{root}"], [id="comment_{root}"]'))
        if row.count() == 0:
            row = self.reply_page.locator(self.selectors['comment']).filter(visible=True).filter(
                has=self.reply_page.locator(f'[id="tooltip_{root}"]'))
        return row.count() == 1 and row.locator(f'a[href*="/user/{uid}"]').count() > 0

    def _fill_text(self, editor, text):
        if not self._publishing or not self.mention_target:
            return super()._fill_text(editor, text)
        uid, name = self.mention_target['uid'], self.mention_target['name']
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}', uid) or not text.startswith('@' + name + ' '):
            raise BridgeError('主人账号与分享正文不匹配')
        editor.focus()
        self.reply_page.keyboard.insert_text('@' + name)
        # Select a real platform mention suggestion, never substitute plain @ text.
        selector = self.selectors.get('mention_candidate', '[role="listbox"] a[href*="/user/{uid}"], [data-e2e="mention-item"] a[href*="/user/{uid}"]')
        candidate = self.reply_page.locator(selector.replace('{uid}', uid)).filter(visible=True)
        try:
            candidate.wait_for(state='visible', timeout=2500)
        except Exception:
            key = self._owner_avatar_key()
            candidate = self.reply_page.locator(self.selectors.get('mention_candidate_row','div.dTwsCivh')).filter(
                has=self.reply_page.locator(f'img[src*="{key}"]')).filter(visible=True)
            candidate.wait_for(state='visible',timeout=5000)
        if candidate.count()!=1 or candidate.inner_text().strip()!=name:
            raise BridgeError('未唯一识别到主人的@候选，保留草稿，不发布')
        href = candidate.get_attribute('href')
        if href:
            actual = urlsplit(urljoin(self.reply_page.url, href))
            if actual.hostname != 'www.douyin.com' or actual.path.rstrip('/') != '/user/' + uid:
                raise BridgeError('主人候选账号校验失败')
        candidate.click()
        token = editor.locator(f'[data-user-id="{uid}"], [data-uid="{uid}"], a[href*="/user/{uid}"]')
        if token.count() == 0:
            token = editor.locator('span.douyin_mention_word').filter(has_text='@'+name)
        if token.count() != 1:
            raise BridgeError('编辑器未生成可核验的@账号实体，不发布')
        if token.inner_text().strip() != '@'+name:
            raise BridgeError('编辑器中的@对象与已选账号不一致')
        editor.focus()
        editor.evaluate("el => { const range=document.createRange(); range.selectNodeContents(el); range.collapse(false); const selection=getSelection(); selection.removeAllRanges(); selection.addRange(range); }")
        self.reply_page.keyboard.insert_text(' ' + text[len('@' + name + ' '):])
        content = editor.evaluate("el=>(el.value??el.innerText).replace(/\\s+/g,' ').trim()")
        if content != re.sub(r'\s+', ' ', text).strip():
            raise BridgeError('含@的评论正文校验失败')

    def poll_replies(self):
        self.origin(self.page)
        tab = self.page.get_by_role("tab", name=re.compile(r"^(?:回复我的|评论和@|评论我的|评论)$")).filter(visible=True)
        if tab.count() != 1:
            entry = self.page.locator(self.selectors["entry"]).filter(visible=True)
            if entry.count() != 1:
                entry = self.page.get_by_text("通知", exact=True).filter(visible=True)
            if entry.count() != 1:
                raise BridgeError("未找到评论回复通知入口")
            entry.click()
            self.page.wait_for_timeout(500)
            tab = self.page.get_by_role("tab", name=re.compile(r"^(?:回复我的|评论和@|评论我的|评论)$")).filter(visible=True)
        if tab.count() != 1:
            raise BridgeError("评论回复通知标签不唯一，需要校准页面")
        tab.click()
        self.page.wait_for_timeout(200)
        if not tab.evaluate("el=>el.getAttribute('aria-selected')==='true'||/active|selected/i.test(String(el.className))"):
            raise BridgeError("评论通知标签未选中")
        panel = self.page.locator(self.selectors["panel"]).filter(visible=True)
        if panel.count() != 1:
            raise BridgeError("评论通知面板不唯一")
        rows = panel.locator(self.selectors["item"]).filter(visible=True).evaluate_all("""(items, selectors) =>
            items.slice(0,100).map(el=>{
                const author=el.querySelector('a[href*="/user/"]');
                const video=[...el.querySelectorAll('a[href]')].find(a=>/\\/video\\/|modal_id=/.test(a.href));
                const u=new URL(video?.href||location.href);
                return {url:video?.href||'',author_url:author?.href||'',author:(author?.innerText||'').trim(),
                    text:(el.querySelector(selectors.content)?.innerText||'').trim(),
                    comment_id:el.dataset.commentId||el.dataset.cid||'',
                    root_id:el.dataset.rootId||u.searchParams.get('root_id')||'',mentioned:true};
            })""", self.selectors)
        events = []
        for row in reversed(rows):
            event = normalize_mention(row, self.account, self.self_user_id)
            root = str(row.get("root_id") or "")
            if not event or not self.registry.owns("douyin", self.account, root):
                continue
            thread = self.registry.get("douyin", self.account, root)
            if event.source_video != thread["video_url"]:
                continue
            events.append(replace(event, mentioned=False, reply_to_own=True,
                                  thread=f"{self.account}:comment:{thread['video_id']}:{root}",
                                  target={**event.target, "root_id": root}))
        return events

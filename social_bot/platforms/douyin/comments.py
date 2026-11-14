"""可见“@我的”通知与精确评论回复；选择器失效时停止，不退回顶层发评。"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, parse_qs

from .adapter import BridgeError
from ..common.chat import SocialEvent


DEFAULT_SELECTORS = {
    "entry": '[data-e2e="notification-entry"], [data-e2e="notice-entry"]',
    "panel": '[data-e2e="mention-list"], [data-e2e="notification-list"], [role="tabpanel"]',
    "item": '[data-e2e="notification-item"], [data-e2e="notice-item"], [data-e2e="mention-item"]',
    "content": '[data-e2e="notification-content"], [data-e2e="comment-content"]',
    "comment": '[data-e2e="comment-item"]',
    "input": '.comment-input-inner-container, [data-e2e="comment-input-container"]',
    "editor": '.public-DraftEditor-content[contenteditable="true"], [data-e2e="comment-input"][contenteditable="true"], textarea[data-e2e="comment-input"]',
}


def normalize_mention(row, account, self_user_id):
    try:
        url = urlsplit(row["url"])
        query = parse_qs(url.query)
        match = re.fullmatch(r"/video/(\d+)/?", url.path)
        video_id = match[1] if match else query.get("modal_id", [""])[0]
        cid = str(row.get("comment_id") or query.get("comment_id", query.get("cid", [""]))[0])
        author_url = urlsplit(row["author_url"])
        sender = re.fullmatch(r"/user/([\w.-]+)/?", author_url.path)
        text = row.get("text", "").strip()
        if (url.scheme != "https" or url.hostname != "www.douyin.com" or url.username or url.password or url.port not in (None, 443)
                or not video_id.isdigit() or not cid.isdigit()
                or int(cid) <= 0 or author_url.scheme != "https" or author_url.hostname != "www.douyin.com"
                or author_url.username or author_url.password or author_url.port not in (None, 443)
                or not sender or sender[1] == self_user_id or not text or row.get("mentioned") is not True):
            return None
        root = str(row.get("root_id") or cid)
        if not root.isdigit():
            root = cid
        return SocialEvent("douyin", account, "comment", f"{video_id}:{cid}", sender[1], text,
                           f"{account}:comment:{video_id}:{root}", mentioned=True,
                           target={"url": f"https://www.douyin.com/video/{video_id}", "open_url": row["url"], "comment_id": cid},
                           sender_name=str(row.get("author") or ""), source_video=f"https://www.douyin.com/video/{video_id}")
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


class DOMMentions:
    def __init__(self, page, reply_page, account, self_user_id, *, selectors=None, permitted=lambda: True):
        self.page, self.reply_page = page, reply_page
        self.account, self.self_user_id = account, self_user_id
        self.selectors = {**DEFAULT_SELECTORS, **(selectors or {})}
        self.permitted = permitted

    @staticmethod
    def origin(page):
        if urlsplit(page.url).scheme != "https" or urlsplit(page.url).hostname != "www.douyin.com":
            raise BridgeError("评论页面已离开抖音，停止处理")

    def open_panel(self):
        self.origin(self.page)
        tab = self.page.get_by_role("tab", name=re.compile(r"^(?:@我的|提及我的|提及)$")).filter(visible=True)
        if tab.count() != 1:
            entry = self.page.locator(self.selectors["entry"]).filter(visible=True)
            if entry.count() != 1:
                entry = self.page.get_by_text("通知", exact=True).filter(visible=True)
            if entry.count() != 1:
                raise BridgeError("未找到唯一的通知入口；请打开@我的通知或校准 mentions_selectors")
            entry.click()
            self.page.wait_for_timeout(700)
            tab = self.page.get_by_role("tab", name=re.compile(r"^(?:@我的|提及我的|提及)$")).filter(visible=True)
        if tab.count() != 1:
            raise BridgeError("无法唯一识别@我的标签，不处理其它通知")
        tab.click()
        self.page.wait_for_timeout(200)
        if not tab.evaluate("el => el.getAttribute('aria-selected') === 'true' || /active|selected/i.test(String(el.className))"):
            raise BridgeError("未确认@我的标签已选中")
        panel = self.page.locator(self.selectors["panel"]).filter(visible=True)
        if panel.count() != 1:
            raise BridgeError("@通知列表不唯一或选择器失效")
        return panel

    def poll(self, stream="mentions"):
        panel = self.open_panel()
        rows = panel.locator(self.selectors["item"]).filter(visible=True).evaluate_all("""(items, selectors) => items.slice(0,100).map(el => {
            const author = el.querySelector('a[href*="/user/"]');
            const video = [...el.querySelectorAll('a[href]')].find(a => /\\/video\\/|modal_id=/.test(a.href));
            const content = el.querySelector(selectors.content);
            return {url:video?.href||'', author_url:author?.href||'', author:(author?.innerText||'').trim(),
                text:(content?.innerText||'').trim(), comment_id:el.dataset.commentId||el.dataset.cid||'',
                root_id:el.dataset.rootId||'', mentioned:true};
        })""", self.selectors)
        events = [normalize_mention(row, self.account, self.self_user_id) for row in reversed(rows)]
        return [event for event in events if event is not None]

    def _target(self, event):
        page = self.reply_page
        self.origin(page)
        if page.url.split("?", 1)[0].rstrip("/") != event.target["url"]:
            raise BridgeError("视频位置变化，不能回复")
        cid = event.target["comment_id"]
        if not re.fullmatch(r"[1-9]\d*", cid):
            raise BridgeError("目标评论 ID 无效")
        target = page.locator(self.selectors["comment"]).filter(visible=True).filter(
            has=page.locator(f'[data-comment-id="{cid}"], [id="tooltip_{cid}"]'))
        # 页面可能把 ID 放在评论元素本身，而非其后代。
        direct = page.locator(self.selectors["comment"]).and_(page.locator(
            f'[data-comment-id="{cid}"], [data-cid="{cid}"], [id="comment_{cid}"]')).filter(visible=True)
        if direct.count() == 1:
            target = direct
        if target.count() != 1:
            raise BridgeError("未找到唯一的原评论，不改发顶层评论")
        author = target.locator(f'a[href*="/user/{event.sender}"]').filter(visible=True)
        if author.count() < 1:
            raise BridgeError("原评论作者校验失败")
        return target

    def _own_replies(self, text):
        return self.reply_page.locator(self.selectors["comment"]).filter(visible=True).evaluate_all("""(items, value) => items.filter(el => {
            const content = el.querySelector('[data-e2e="comment-content"]') || el.querySelector('.comment-item-info-wrap')?.nextElementSibling;
            const own = [...el.querySelectorAll('a[href]')].some(a => !content?.contains(a) && new URL(a.href).pathname.replace(/\\/$/,'') === '/user/'+value.uid);
            return own && (content?.innerText||'').replace(/\\s+/g,' ').trim() === value.text.replace(/\\s+/g,' ').trim();
        }).map(el=>el.dataset.commentId||el.dataset.cid||el.querySelector('[id^="tooltip_"]')?.id.replace(/^tooltip_/,'')||el.id).filter(Boolean)""", {"uid": self.self_user_id, "text": text})

    def send(self, event, text):
        if not self.permitted():
            raise BridgeError("评论回复已停用")
        page = self.reply_page
        # 已有草稿不覆盖，即使目标视频不同也不切走该草稿。
        existing = page.locator(self.selectors["editor"]).filter(visible=True)
        if existing.count() and any(existing.nth(i).evaluate("el=>(el.value??el.innerText).trim()") for i in range(existing.count())):
            raise BridgeError("评论输入框已有草稿")
        page.goto(event.target.get("open_url") or event.target["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(1500)
        if page.locator(self.selectors["comment"]).filter(visible=True).count() == 0:
            icon = page.locator('[data-e2e="feed-comment-icon"]').filter(visible=True)
            if icon.count() == 1:
                icon.click()
                page.wait_for_timeout(700)
        target = self._target(event)
        input_scope = page.locator(self.selectors["input"]).filter(visible=True)
        if input_scope.count() != 1:
            raise BridgeError("评论输入区域不唯一")
        existing = input_scope.locator(self.selectors["editor"]).filter(visible=True)
        if existing.count() and existing.first.evaluate("el=>(el.value??el.innerText).trim()"):
            raise BridgeError("评论输入框已有草稿")
        reply = target.get_by_text("回复", exact=True).filter(visible=True)
        if reply.count() != 1:
            raise BridgeError("原评论回复按钮不唯一")
        reply.evaluate("el=>el.click()")
        self.reply_page.wait_for_timeout(100)
        editor = input_scope.locator(self.selectors["editor"]).filter(visible=True)
        if editor.count() == 0:
            input_scope.click(position={"x": 5, "y": 5})
        editor.wait_for(state="visible", timeout=3000)
        if editor.count() != 1 or not event.sender_name:
            raise BridgeError("无法确认评论回复输入框或作者")
        context = input_scope.inner_text().replace(" ", "").replace("@", "")
        binding = input_scope.get_attribute("data-reply-to")
        if binding != event.target["comment_id"] and "回复" + event.sender_name.replace(" ", "") not in context:
            raise BridgeError("输入框未确认绑定原评论")
        before = set(self._own_replies(text))
        def verify():
            self._target(event)
            if input_scope.get_attribute("data-reply-to") not in (None, "", event.target["comment_id"]):
                raise BridgeError("输入框回复对象变化，正文保留为草稿")
        return self._submit_text(input_scope, editor, text, before, verify=verify)

    def _fill_text(self, editor, text):
        editor.focus()
        self.reply_page.keyboard.insert_text(text)
        if editor.evaluate("el=>(el.value??el.innerText).trim()") != text:
            raise BridgeError("评论正文未正确写入")

    def _submit_text(self, input_scope, editor, text, before, verify=lambda: None):
        page = self.reply_page
        self._fill_text(editor, text)
        verify()
        if not self.permitted():
            raise BridgeError("发送前已停用，正文保留为草稿")
        button = input_scope.get_by_role("button", name=re.compile(r"^(?:发送|发布|发表)$")).filter(visible=True)
        if button.count() == 1:
            self._submission_attempted = True
            button.click()
        else:
            arrows = input_scope.locator('svg path[d^="M17.5 30C23.851 30"]').filter(visible=True)
            if arrows.count() != 1:
                raise BridgeError("评论发布按钮不唯一")
            self._submission_attempted = True
            # The real page binds publication to the SVG, not its wrapping span.
            arrows.click()
        for _ in range(30):
            page.wait_for_timeout(500)
            self.origin(page)
            if page.get_by_text(re.compile("请完成验证|请输入验证码|拖动滑块")).filter(visible=True).count():
                raise BridgeError("评论发布遇到平台验证，需要人工处理；不自动重发")
            created = set(self._own_replies(text)) - before
            if len(created) == 1:
                return created.pop()
            if len(created) > 1:
                raise BridgeError("出现多条匹配回复，结果未知，不自动重发")
        raise BridgeError("未确认新回复已出现，不自动重发")

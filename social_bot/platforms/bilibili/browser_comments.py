"""Publish through the visible below-video composer and observe its own receipt."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit, parse_qs

from ...modules.video import VideoError
from .client import video_id
from .social import positive


class BrowserCommentError(VideoError):
    def __init__(self, code, *, before_write=True, fallback_safe=False):
        super().__init__(code)
        self.code, self.before_write, self.fallback_safe = code, before_write, fallback_safe


class BilibiliBrowserComments:
    def __init__(self, context, account, sessdata, *, permitted=lambda: True, page=None, response_timeout=20000,
                 mention_target=None):
        self.context, self.account, self.sessdata = context, str(account), sessdata
        self.page, self.permitted = page, permitted
        self.response_timeout = response_timeout
        self.mention_target=mention_target

    @staticmethod
    def content(editor):
        return editor.evaluate("el=>(el.value??el.innerText??'').trim()")

    def check_account(self):
        values = self.context.cookies("https://www.bilibili.com/")
        sessions = [c["value"] for c in values if c["name"] == "SESSDATA"]
        identities = [c["value"] for c in values if c["name"] == "DedeUserID"]
        if not sessions or any(value != self.sessdata for value in sessions):
            raise BrowserCommentError("BILIBILI_BROWSER_ACCOUNT_MISMATCH")
        if identities and any(value != self.account for value in identities):
            raise BrowserCommentError("BILIBILI_BROWSER_ACCOUNT_MISMATCH")

    def root_box(self, main):
        boxes = main.locator("bili-comment-box")
        selected = []
        for index in range(boxes.count()):
            box = boxes.nth(index)
            root = box.evaluate("""el=>{
                for(let n=el;n;n=n.parentNode||n.host){
                    if(['bili-comment-renderer','bili-comment-thread-renderer','bili-comment-replies-renderer'].includes(n.localName))return false;
                    if(n.localName==='bili-comments')return true;
                }return false;
            }""")
            editors = box.locator('[contenteditable="true"], textarea')
            if root and any(editors.nth(i).is_visible() for i in range(editors.count())):
                selected.append(box)
        if len(selected) != 1:
            raise BrowserCommentError("BILIBILI_ROOT_COMPOSER_NOT_UNIQUE", fallback_safe=True)
        return selected[0]

    def prepare(self, url):
        bvid = video_id(url)
        self.check_account()
        if self.page is None:
            self.page = self.context.new_page()
        page = self.page
        # Preserve drafts before navigating to another video.
        editors = page.locator('bili-comment-box [contenteditable="true"], bili-comment-box textarea')
        if any(self.content(editors.nth(i)) for i in range(editors.count())):
            raise BrowserCommentError("BILIBILI_COMMENT_DRAFT_EXISTS")
        target = "https://www.bilibili.com/video/" + bvid
        page.goto(target, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2000)
        for _ in range(4):
            if page.locator("bili-comments").count():
                break
            page.mouse.wheel(0, 700)
            page.wait_for_timeout(700)
        main = page.locator("bili-comments")
        main.wait_for(state="attached", timeout=10000)
        if main.count() != 1:
            raise BrowserCommentError("BILIBILI_COMMENT_SECTION_NOT_UNIQUE")
        main.evaluate("el=>el.scrollIntoView({block:'start'})")
        page.wait_for_timeout(1200)
        main.locator("bili-comment-box").first.wait_for(state="attached", timeout=10000)
        if main.get_by_text(re.compile(r"^(?:评论区已关闭|评论已关闭|当前视频禁止评论)$")).filter(visible=True).count():
            raise BrowserCommentError("BILIBILI_COMMENTS_CLOSED")
        params = (main.get_attribute("data-params") or "").split(",")
        if len(params) < 2 or params[0] != "1" or not positive(params[1]):
            raise BrowserCommentError("BILIBILI_COMMENT_VIDEO_BINDING_MISSING")
        box = self.root_box(main)
        editor = box.locator('[contenteditable="true"], textarea')
        if editor.count() != 1:
            raise BrowserCommentError("BILIBILI_COMMENT_EDITOR_NOT_UNIQUE", fallback_safe=True)
        editor.scroll_into_view_if_needed()
        editor.wait_for(state="visible", timeout=5000)
        if self.content(editor):
            raise BrowserCommentError("BILIBILI_COMMENT_DRAFT_EXISTS")
        return bvid, params[1], main, box, editor

    def disable_dynamic_share(self, box):
        checkboxes = box.locator('input[type="checkbox"]')
        roles = box.get_by_role("checkbox")
        for index in range(checkboxes.count()):
            item = checkboxes.nth(index)
            if item.is_checked():
                item.uncheck()
            if item.is_checked():
                raise BrowserCommentError("BILIBILI_DYNAMIC_SHARE_NOT_DISABLED")
        if not checkboxes.count():
            for index in range(roles.count()):
                item = roles.nth(index)
                if item.get_attribute("aria-checked") == "true":
                    item.click()
                if item.get_attribute("aria-checked") not in ("false", None):
                    raise BrowserCommentError("BILIBILI_DYNAMIC_SHARE_NOT_DISABLED")
            if not roles.count() and box.get_by_text(re.compile("同时.*动态")).filter(visible=True).count():
                raise BrowserCommentError("BILIBILI_DYNAMIC_SHARE_STATE_UNKNOWN")

    def publish(self, url, text):
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 180:
            raise BrowserCommentError("BILIBILI_COMMENT_TEXT_INVALID")
        if self.mention_target and (not positive(self.mention_target.get('uid'))
                or not text.startswith('@'+self.mention_target.get('name','')+' ')):
            raise BrowserCommentError('BILIBILI_MENTION_TARGET_NOT_BOUND')
        clicked = False
        try:
            bvid, oid, main, box, editor = self.prepare(url)
            self.disable_dynamic_share(box)
            editor.focus()
            self.page.keyboard.insert_text(text)
            if self.content(editor) != text:
                raise BrowserCommentError("BILIBILI_COMMENT_TEXT_NOT_CONFIRMED")
            self.check_account()
            if not self.permitted() or video_id(self.page.url) != bvid or main.get_attribute("data-params") != "1," + oid:
                raise BrowserCommentError("BILIBILI_COMMENT_CONTEXT_CHANGED")
            button = box.get_by_role("button", name="发布", exact=True)
            if button.count() != 1 or not button.is_enabled():
                raise BrowserCommentError("BILIBILI_COMMENT_PUBLISH_BUTTON_UNAVAILABLE")

            def own_response(response):
                parsed = urlsplit(response.url)
                if (parsed.scheme != "https" or parsed.hostname != "api.bilibili.com"
                        or parsed.path != "/x/v2/reply/add" or response.request.method != "POST"):
                    return False
                body = response.request.post_data or ""
                try:
                    data = json.loads(body) if body.lstrip().startswith("{") else {
                        key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
                    return (str(data.get("oid")) == oid and str(data.get("type")) == "1"
                            and str(data.get("root") or "0") == "0" and str(data.get("parent") or "0") == "0"
                            and data.get("message") == text)
                except (ValueError, TypeError):
                    return False

            with self.page.expect_response(own_response, timeout=self.response_timeout) as pending:
                clicked = True
                button.click()
            response = pending.value
            if response.status != 200:
                raise BrowserCommentError(f"BILIBILI_BROWSER_HTTP_{response.status}", before_write=False)
            envelope = response.json()
            if envelope.get("code") != 0:
                error = BrowserCommentError("BILIBILI_BROWSER_COMMENT_REJECTED", before_write=False)
                error.api_code = envelope.get("code") if type(envelope.get("code")) is int else None
                raise error
            data = envelope.get("data") or {}
            reply = data.get("reply") or {}
            root = positive(data.get("rpid") or reply.get("rpid"))
            mid = positive((reply.get("member") or {}).get("mid") or reply.get("mid"))
            if not root or (mid and mid != self.account):
                raise BrowserCommentError("BILIBILI_BROWSER_RECEIPT_UNCONFIRMED", before_write=False)
            visible = False
            for _ in range(8):
                self.page.wait_for_timeout(500)
                if self.content(editor) == "" and main.get_by_text(text, exact=True).filter(visible=True).count():
                    visible = True
                    break
            receipt=dict(root_id=root, video_id=oid, type=1, transport="browser", visible=visible)
            if self.mention_target:
                uid,name=self.mention_target['uid'],self.mention_target['name']
                content=reply.get('content') or {}
                members=content.get('members') or []
                if isinstance(members,dict):members=list(members.values())
                confirmed=any(isinstance(m,dict) and str(m.get('mid'))==uid for m in members)
                matching=main.locator('bili-comment-renderer').filter(has_text=text)
                if matching.count():
                    links=matching.locator('a[href*="space.bilibili.com/'+uid+'"]')
                    confirmed=confirmed or any(name in links.nth(i).inner_text() for i in range(links.count()))
                receipt.update(mention_uid=uid,mention_name=name,mention_confirmed=confirmed)
            return receipt
        except BrowserCommentError as exc:
            exc.before_write = not clicked
            raise
        except Exception:
            raise BrowserCommentError("BILIBILI_BROWSER_COMMENT_RESULT_UNKNOWN" if clicked else
                                      "BILIBILI_BROWSER_COMMENT_UI_UNAVAILABLE", before_write=not clicked) from None

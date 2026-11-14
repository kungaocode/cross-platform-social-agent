"""Headless Edge on intercepted fixture pages; never connects to Douyin."""
import importlib.util
import unittest
from dataclasses import replace
from unittest.mock import Mock

from social_bot.platforms.douyin.adapter import BridgeError, DOMChat, fresh_messages
from social_bot.platforms.douyin.comments import DOMMentions, normalize_mention


NOTICE = '''<html><style>div,button {min-height:30px;min-width:100px}</style>
<button role="tab" aria-selected="true">@我的</button>
<div role="tabpanel" data-e2e="mention-list">
 <div data-e2e="notification-item" data-comment-id="101">
  <a href="https://www.douyin.com/user/friend-id">朋友</a>
  <a href="https://www.douyin.com/video/123456?comment_id=101">查看</a>
  <div data-e2e="notification-content">@助手 今天天气怎么样？</div>
 </div>
</div></html>'''

VIDEO = '''<html><style>div,section,textarea,button {min-height:30px;min-width:150px}</style>
<div data-e2e="comment-list">
 <div data-e2e="comment-item" data-comment-id="101">
  <a href="https://www.douyin.com/user/friend-id">朋友</a>
  <div data-e2e="comment-content">@助手 今天天气怎么样？</div>
  <button id="reply">回复</button>
 </div>
</div>
<section data-e2e="comment-input-container"><span id="context"></span>
 <textarea data-e2e="comment-input"></textarea><button id="send">发送</button>
</section>
<script>
document.querySelector('#reply').onclick=()=>{
 document.querySelector('section').dataset.replyTo='101';
 document.querySelector('#context').textContent='回复朋友';
};
document.querySelector('#send').onclick=()=>{
 const text=document.querySelector('textarea').value;
 if(!text.trim())return;
 const el=document.createElement('div');el.dataset.e2e='comment-item';el.dataset.commentId='900';
 const a=document.createElement('a');a.href='https://www.douyin.com/user/my-id';a.textContent='我';el.append(a);
 const c=document.createElement('div');c.dataset.e2e='comment-content';c.textContent=text;el.append(c);
 document.querySelector('[data-e2e="comment-list"]').append(el);
 document.querySelector('textarea').value='';
};
</script></html>'''


class NormalizationTests(unittest.TestCase):
    def test_only_mentions_with_real_comment_id_and_author_are_routable(self):
        row = dict(url="https://www.douyin.com/video/123456?comment_id=101", author_url="https://www.douyin.com/user/friend-id",
                   author="朋友", text="@助手 你好", mentioned=True)
        event = normalize_mention(row, "default", "my-id")
        self.assertEqual(event.target["comment_id"], "101")
        self.assertIsNone(normalize_mention({**row, "mentioned": False}, "default", "my-id"))
        self.assertIsNone(normalize_mention({**row, "author_url": "https://www.douyin.com/user/my-id"}, "default", "my-id"))
        self.assertIsNone(normalize_mention({**row, "url": "https://evil.com/video/123456?comment_id=101"}, "default", "my-id"))
        self.assertIsNone(normalize_mention({**row, "url": "https://www.douyin.com:8443/video/123456?comment_id=101"}, "default", "my-id"))
        self.assertIsNone(normalize_mention({**row, "url": "https://www.douyin.com/video/123456"}, "default", "my-id"))

    def test_media_messages_are_new_events_but_history_is_not_replayed(self):
        old = [{"id": "1", "self": False, "text": "你好", "type": "text"}]
        new = {"id": "2", "self": False, "text": "", "type": "media", "media": [{"kind": "image", "url": "data:image/png;base64,test"}]}
        self.assertEqual(fresh_messages(old, old + [new]), [new])
        self.assertEqual(fresh_messages(None, old + [new]), [])


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright required")
class CommentDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(channel="msedge", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8",
                           body=VIDEO if "/video/" in route.request.url else NOTICE))
        self.notice, self.reply = self.context.new_page(), self.context.new_page()
        self.notice.goto("https://www.douyin.com/")
        self.dom = DOMMentions(self.notice, self.reply, "default", "my-id")

    def tearDown(self):
        self.context.close()

    def test_reads_at_tab_and_replies_to_exact_comment_with_emoji(self):
        events = self.dom.poll()
        self.assertEqual(len(events), 1)
        self.dom.send(events[0], "今天晴朗 😊")
        self.assertEqual(self.reply.locator('[data-comment-id="900"] [data-e2e="comment-content"]').inner_text(), "今天晴朗 😊")
        self.assertEqual(self.reply.locator("section").get_attribute("data-reply-to"), "101")

    def test_not_selected_tab_is_not_treated_as_at_notifications(self):
        self.notice.get_by_role("tab").evaluate("el=>el.setAttribute('aria-selected','false')")
        with self.assertRaises(BridgeError):
            self.dom.poll()

    def test_missing_original_comment_does_not_post_top_level(self):
        event = self.dom.poll()[0]
        event = replace(event, target={**event.target, "comment_id": "999"})
        with self.assertRaises(BridgeError):
            self.dom.send(event, "不应发送")
        self.assertEqual(self.reply.locator('[data-e2e="comment-item"]').count(), 1)

    def test_existing_draft_survives_and_disable_before_send_prevents_post(self):
        event = self.dom.poll()[0]
        self.reply.goto(event.target["url"])
        self.reply.locator("textarea").fill("尚未完成的草稿")
        with self.assertRaises(BridgeError):
            self.dom.send(event, "新回复")
        self.assertEqual(self.reply.locator("textarea").input_value(), "尚未完成的草稿")
        self.reply.locator("textarea").fill("")
        self.dom.permitted = Mock(side_effect=[True, False])
        with self.assertRaises(BridgeError):
            self.dom.send(event, "新回复")
        self.assertEqual(self.reply.locator('[data-e2e="comment-item"]').count(), 1)

    def test_dm_inline_image_and_video_card_are_extracted_without_model_data(self):
        self.notice.set_content('''<div data-e2e="conversation-item" class="active"><span data-e2e="conversation-title">朋友</span></div>
          <div data-e2e="message-item" data-message-id="1"><div data-e2e="msg-item-content"><img src="data:image/png;base64,aGVsbG8="></div></div>
          <div data-e2e="message-item" data-message-id="2"><div data-e2e="share-aweme"><a href="https://www.douyin.com/video/123456">视频</a></div></div>''')
        dom = DOMChat(self.notice)
        messages = dom.read("朋友")
        self.assertEqual(messages[0]["media"][0]["kind"], "image")
        self.assertTrue(messages[0]["media"][0]["url"].startswith("dom-image:"))
        self.assertNotIn("aGVsbG8", str(messages))
        self.assertEqual(dom.resolve_inline(messages[0]["media"][0]["url"]), "data:image/png;base64,aGVsbG8=")
        self.assertEqual(messages[1]["media"][0], {"kind": "video", "url": "https://www.douyin.com/video/123456"})


if __name__ == "__main__":
    unittest.main()

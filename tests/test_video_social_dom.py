"""Real headless Edge against intercepted fixtures; no platform writes."""
import importlib.util
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from social_bot.platforms.douyin.adapter import BridgeError
from social_bot.platforms.douyin.video_social import DouyinVideoInteraction
from social_bot.video_browser import VideoBrowser
from social_bot.video_participation import ThreadRegistry

URL = "https://www.douyin.com/video/1234567"
BILI = "https://www.bilibili.com/video/BV1xx411c7mD"
STYLE = "<style>div,section,textarea,button,a {min-height:30px;min-width:130px;display:block}</style>"
NOTICE = STYLE + """<button role="tab" aria-selected="false">@我的</button>
<button role="tab" aria-selected="false" id="replies">评论和@</button>
<div data-e2e="mention-list" role="tabpanel">
 <div data-e2e="notification-item" data-comment-id="101" data-root-id="400">
  <a href="https://www.douyin.com/user/friend-id">朋友</a>
  <a href="https://www.douyin.com/video/1234567?comment_id=101&root_id=400">视频</a>
  <div data-e2e="notification-content">我也觉得这段有趣</div>
 </div>
 <div data-e2e="notification-item" data-comment-id="102" data-root-id="999">
  <a href="https://www.douyin.com/user/other-id">路人</a>
  <a href="https://www.douyin.com/video/1234567?comment_id=102&root_id=999">视频</a>
  <div data-e2e="notification-content">其它评论楼</div>
 </div>
</div><script>document.querySelector('#replies').onclick=e=>e.currentTarget.setAttribute('aria-selected','true');</script>"""
VIDEO = STYLE + """<header><a href="https://www.douyin.com/user/my-id">个人主页</a></header>
<div data-e2e="comment-list">
 <div data-e2e="comment-item" data-comment-id="400" data-root-id="400">
  <a href="https://www.douyin.com/user/my-id">我</a>
  <div data-e2e="comment-content">原来发布的顶层评论</div>
 </div>
 <div data-e2e="comment-item" data-comment-id="101" data-root-id="400">
  <a href="https://www.douyin.com/user/friend-id">朋友</a>
  <div data-e2e="comment-content">我也觉得这段有趣</div>
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
 const text=document.querySelector('textarea').value;if(!text.trim())return;
 const el=document.createElement('div');el.dataset.e2e='comment-item';el.dataset.commentId='900';
 el.dataset.rootId=document.querySelector('section').dataset.replyTo?'400':'900';
 const a=document.createElement('a');a.href='https://www.douyin.com/user/my-id';a.textContent='我';el.append(a);
 const c=document.createElement('div');c.dataset.e2e='comment-content';c.textContent=text;el.append(c);
 document.querySelector('[data-e2e="comment-list"]').append(el);document.querySelector('textarea').value='';
};
</script>"""


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright required")
class VideoSocialDOMTests(unittest.TestCase):
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
        self.temp = tempfile.TemporaryDirectory()
        self.registry = ThreadRegistry(Path(self.temp.name) / "threads.sqlite3")
        self.context = self.browser.new_context()
        self.context.add_cookies([dict(name="sessionid", value="fixture-only", domain=".douyin.com", path="/", secure=True)])
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8",
            body=VIDEO if "/video/" in route.request.url else NOTICE))
        self.notice, self.reply = self.context.new_page(), self.context.new_page()
        self.notice.goto("https://www.douyin.com/")
        self.interaction = DouyinVideoInteraction(self.notice, self.reply, "default", "my-id", self.registry)
        self.registry.claim("douyin", "default", URL, "之前的评论")
        self.registry.finish("douyin", "default", URL, dict(root_id="400", video_id="1234567"))

    def tearDown(self):
        self.context.close()
        self.registry.close()
        self.temp.cleanup()

    def test_top_level_comment_returns_a_visible_own_root_receipt(self):
        receipt = self.interaction.publish(URL, "这段反转挺有趣 😊")
        self.assertEqual(receipt, dict(root_id="900", video_id="1234567", type=1))
        self.assertEqual(self.reply.locator('[data-comment-id="900"]').get_attribute("data-root-id"), "900")

    def test_owner_mention_selects_bound_account_entity_before_publish(self):
        extra = '''<div role="listbox"><a href="https://www.douyin.com/user/owner-id">主人账号</a></div>
        <script>
        const old=document.querySelector('textarea');
        const ed=document.createElement('div');ed.contentEditable='true';ed.dataset.e2e='comment-input';old.replaceWith(ed);
        document.querySelector('[role="listbox"] a').onclick=e=>{e.preventDefault();ed.innerHTML='<a contenteditable="false" data-user-id="owner-id" href="/user/owner-id">@主人账号</a>&nbsp;';ed.focus();const r=document.createRange();r.selectNodeContents(ed);r.collapse(false);const s=getSelection();s.removeAllRanges();s.addRange(r);};
        document.querySelector('#send').onclick=()=>{const row=document.querySelector('[data-comment-id="400"]').cloneNode(true);row.dataset.commentId='901';row.querySelector('[data-e2e="comment-content"]').innerHTML=ed.innerHTML;document.querySelector('[data-e2e="comment-list"]').append(row);ed.innerHTML='';};
        </script>'''
        self.context.unroute('**/*')
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=VIDEO+extra))
        self.interaction.mention_target = dict(uid='owner-id', name='主人账号')
        receipt = self.interaction.publish(URL, '@主人账号 主人，这段猫咪很有趣')
        self.assertTrue(receipt['mention_confirmed'])

    def test_unbound_owner_mention_does_not_publish_plain_text(self):
        self.interaction.mention_target = dict(uid='owner-id', name='主人账号')
        with self.assertRaises(Exception):
            self.interaction.publish(URL, '@主人账号 主人，这段猫咪很有趣')
        self.assertEqual(self.reply.locator('[data-comment-id="900"]').count(), 0)

    def test_reply_notification_only_routes_the_owned_thread_without_an_at(self):
        events = self.interaction.poll_replies()
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].reply_to_own)
        self.assertFalse(events[0].mentioned)
        self.interaction.send(events[0], "对，这个细节很有意思 😊")
        self.assertEqual(self.reply.locator('[data-comment-id="900"]').get_attribute("data-root-id"), "400")
        self.assertEqual(self.reply.locator("section").get_attribute("data-reply-to"), "101")

    def test_wrong_thread_or_wrong_account_never_posts(self):
        event = self.interaction.poll_replies()[0]
        event = replace(event, target={**event.target, "root_id": "999"})
        with self.assertRaises(BridgeError):
            self.interaction.send(event, "不应该发送")
        self.assertEqual(self.reply.locator('[data-comment-id="900"]').count(), 0)
        self.interaction.self_user_id = "another-id"
        with self.assertRaises(BridgeError):
            self.interaction.publish(URL, "账号不匹配")
        self.assertEqual(self.reply.locator('[data-comment-id="900"]').count(), 0)

    def test_existing_draft_is_preserved(self):
        self.reply.goto(URL)
        self.reply.locator("textarea").fill("还没有写完的草稿")
        with self.assertRaises(BridgeError):
            self.interaction.publish(URL, "另一条评论")
        self.assertEqual(self.reply.locator("textarea").input_value(), "还没有写完的草稿")

    def test_profile_id_prefix_does_not_count_as_the_same_account(self):
        self.reply.goto(URL)
        self.reply.locator("header a").evaluate("a=>a.href='https://www.douyin.com/user/my-id-other'")
        with self.assertRaisesRegex(BridgeError, "账号不匹配"):
            self.interaction._verify_own_account()

    def test_recommend_refresh_clears_stale_candidates_and_reads_visible_cards(self):
        self.context.unroute("**/*")
        html = STYLE + '<title>首页推荐</title><button id="refresh">换一换</button><a id="video" href="' + BILI + '">二创片段</a>'
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=html))
        page = self.context.new_page()
        browser = VideoBrowser(page, "bilibili")
        browser.candidates["https://www.bilibili.com/video/BV1yy411c7mD"] = "旧推荐"
        observation = browser.recommend()
        self.assertEqual([item["url"] for item in observation["candidates"]], [BILI])
        self.assertNotIn("旧推荐", str(observation))


if __name__ == "__main__":
    unittest.main()

"""Below-video publication in real Edge with open shadow DOM and local API fixtures."""
import importlib.util
import json
import unittest
from unittest.mock import Mock

from social_bot.platforms.bilibili.browser_comments import BilibiliBrowserComments, BrowserCommentError
from social_bot.platforms.bilibili.video_social import BilibiliVideoInteraction

URL = "https://www.bilibili.com/video/BV1xx411c7mD"
HTML = """<style>bili-comments,bili-comment-box,bili-comment-thread-renderer{display:block;min-height:40px}</style>
<bili-comments data-params="1,123"></bili-comments>
<script>
const main=document.querySelector('bili-comments').attachShadow({mode:'open'});
const box=document.createElement('bili-comment-box');main.append(box);
const editor=box.attachShadow({mode:'open'});
editor.innerHTML='<style>.brt-editor{min-height:40px;min-width:200px}</style><div class="brt-editor" contenteditable="true"></div><label><input type="checkbox" checked>同时转发到我的动态</label><button>发布</button>';
const thread=document.createElement('bili-comment-thread-renderer');main.append(thread);
const nested=thread.attachShadow({mode:'open'});const reply=document.createElement('bili-comment-box');nested.append(reply);
reply.attachShadow({mode:'open'}).innerHTML='<textarea></textarea><button>发布</button>';
editor.querySelector('button').onclick=async()=>{
 const text=editor.querySelector('[contenteditable]').innerText;
 const response=await fetch('https://api.bilibili.com/x/v2/reply/add',{
  method:'POST',body:new URLSearchParams({oid:'123',type:'1',root:'0',parent:'0',message:text,csrf:'fixture-only'})});
 const result=await response.json();if(result.code!==0)return;
 editor.querySelector('[contenteditable]').innerText='';
 const item=document.createElement('bili-comment-renderer');main.append(item);
 const r=item.attachShadow({mode:'open'});const p=document.createElement('p');p.textContent=text;r.append(p);
};
</script>"""


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright required")
class BilibiliBrowserCommentTests(unittest.TestCase):
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
        self.context.add_cookies([dict(name="SESSDATA",value="fixture-session",domain=".bilibili.com",path="/"),
                                 dict(name="DedeUserID",value="100",domain=".bilibili.com",path="/")])
        self.requests, self.status = [], 200
        self.members=[]
        def route(request):
            if "/x/v2/reply/add" in request.request.url:
                self.requests.append(request.request.post_data)
                request.fulfill(status=self.status, content_type="application/json",
                    headers={"Access-Control-Allow-Origin":"https://www.bilibili.com"},
                    body=json.dumps({'code':0,'data':{'rpid':900,'reply':{'rpid':900,'member':{'mid':100},
                                                                       'content':{'members':self.members}}}}))
            else:
                request.fulfill(content_type="text/html; charset=utf-8",body=HTML)
        self.context.route("**/*", route)
        self.page = self.context.new_page()
        self.publisher = BilibiliBrowserComments(self.context, "100", "fixture-session", page=self.page, response_timeout=2000)

    def tearDown(self):
        self.context.close()

    def test_posts_once_in_the_root_composer_and_confirms_visible_own_receipt(self):
        result = self.publisher.publish(URL, "这段画面的反差挺有意思 😊")
        self.assertEqual((result["root_id"],result["video_id"],result["transport"]),("900","123","browser"))
        self.assertTrue(result["visible"])
        self.assertEqual(len(self.requests),1)
        self.assertFalse(self.page.locator('bili-comments').locator('input[type="checkbox"]').is_checked())
        self.assertNotIn("fixture-session",str(result))

    def test_existing_draft_or_wrong_account_prevents_any_request(self):
        self.page.goto(URL)
        self.page.locator('.brt-editor').fill("原有草稿")
        with self.assertRaisesRegex(BrowserCommentError,"DRAFT_EXISTS"):
            self.publisher.publish(URL,"新评论")
        self.assertEqual(self.page.locator('.brt-editor').inner_text(),"原有草稿")
        self.assertEqual(self.requests,[])
        self.publisher.account="999"
        with self.assertRaisesRegex(BrowserCommentError,"ACCOUNT_MISMATCH"):
            self.publisher.publish(URL,"新评论")
        self.assertEqual(self.requests,[])

    def test_owner_mention_is_bound_and_confirmed_from_server_members(self):
        self.publisher.mention_target=dict(uid='456',name='指定主人')
        self.members=[dict(mid=456,uname='指定主人')]
        result=self.publisher.publish(URL,'@指定主人 主人，我看见一只戴帽子的猫。')
        self.assertTrue(result['mention_confirmed'])
        self.assertEqual(result['mention_uid'],'456')
        self.assertEqual(len(self.requests),1)

    def test_wrong_mention_prefix_does_not_open_or_post(self):
        self.publisher.mention_target=dict(uid='456',name='指定主人')
        with self.assertRaisesRegex(BrowserCommentError,'MENTION_TARGET_NOT_BOUND'):
            self.publisher.publish(URL,'@其他人 主人，视频分享。')
        self.assertEqual(self.requests,[])

    def test_unknown_result_after_click_does_not_fall_back_to_an_api_post(self):
        self.status=500
        api=Mock()
        interaction=BilibiliVideoInteraction(api,"100",None,browser_comments=self.publisher,transport="auto")
        with self.assertRaises(BrowserCommentError) as caught:
            interaction.publish(URL,"真实内容")
        self.assertFalse(caught.exception.before_write)
        self.assertEqual(len(self.requests),1)
        api.post_video_comment.assert_not_called()

    def test_disabled_before_send_preserves_generated_draft(self):
        self.publisher.permitted=lambda:False
        with self.assertRaisesRegex(BrowserCommentError,"CONTEXT_CHANGED"):
            self.publisher.publish(URL,"本次生成的正文")
        self.assertEqual(self.requests,[])
        self.assertEqual(self.page.locator('.brt-editor').inner_text(),"本次生成的正文")


if __name__=="__main__":
    unittest.main()

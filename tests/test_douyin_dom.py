"""Actual Edge DOM tests on intercepted local fixtures; no Douyin network calls."""
import importlib.util
import unittest
from social_bot.platforms.douyin.adapter import DOMChat, BridgeError

FIXTURE = '''<html><style>div,textarea {min-width:200px;min-height:30px}</style>
<div data-e2e="conversation-item" class="active"><span data-e2e="conversation-title">朋友</span></div>
<div data-e2e="message-item" data-message-id="1"><div data-e2e="msg-item-content">你好</div></div>
<textarea data-e2e="msg-input"></textarea>
<script>document.querySelector('textarea').addEventListener('keydown', e => {
 if(e.key !== 'Enter') return;
 e.preventDefault();
 const box = document.createElement('div'); box.setAttribute('data-e2e','message-item');
 box.className = 'isFromMe';
 const text = document.createElement('div'); text.setAttribute('data-e2e','msg-item-content');
 text.textContent = e.target.value; box.append(text); document.body.append(box); e.target.value = '';
});</script></html>'''

# 抖音实际使用 column-reverse 渲染：DOM 顺序是最新在前，视觉顺序是最旧在上。
REVERSED = '''<html><style>div[data-e2e="message-item"] {min-height:40px}</style>
<div data-e2e="conversation-item" class="active"><span data-e2e="conversation-title">朋友</span></div>
<div style="display:flex;flex-direction:column-reverse">
  <div data-e2e="message-item"><div data-e2e="msg-item-content">最新</div></div>
  <div data-e2e="message-item"><div data-e2e="msg-item-content">较早</div></div>
</div></html>'''


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Install douyin/requirements.txt for browser tests')
class DOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel='msedge', headless=True)
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html; charset=utf-8', body=FIXTURE))
        self.page = self.context.new_page()
        self.page.goto('https://www.douyin.com/')
        self.dom = DOMChat(self.page)

    def tearDown(self):
        self.context.close()

    def test_read_and_verified_send(self):
        self.assertEqual(self.dom.names(), ['朋友'])
        self.dom.select('朋友')
        self.assertEqual(self.dom.read('朋友')[0]['text'], '你好')
        self.dom.send('朋友', '测试回复')
        self.assertTrue(self.dom.read('朋友')[-1]['self'])

    def test_wrong_target_or_missing_selection_fails(self):
        with self.assertRaises(BridgeError):
            self.dom.verify('其他人')
        self.page.locator('[data-e2e="conversation-item"]').evaluate("el => el.className = ''")
        with self.assertRaises(BridgeError):
            self.dom.verify('朋友')

    def test_existing_draft_not_overwritten(self):
        box = self.page.locator('textarea')
        box.fill('未完成的草稿')
        with self.assertRaises(BridgeError):
            self.dom.send('朋友', '新回复')
        self.assertEqual(box.input_value(), '未完成的草稿')

    def test_visible_badge_and_zero_hidden_badges(self):
        row=self.page.locator('[data-e2e="conversation-item"]')
        row.evaluate("el=>el.insertAdjacentHTML('beforeend','<span data-e2e=unread-count>3</span>')")
        self.assertTrue(self.dom.conversation_states()[0]['unread'])
        self.assertEqual(self.dom.conversation_states()[0]['unread_count'],3)
        self.page.locator('[data-e2e="unread-count"]').evaluate("el=>el.style.display='none'")
        self.assertFalse(self.dom.conversation_states()[0]['unread'])
        self.page.locator('[data-e2e="unread-count"]').evaluate("el=>{el.style.display='';el.innerText='0'}")
        self.assertFalse(self.dom.conversation_states()[0]['unread'])

    def test_small_red_dot_is_detected_but_red_preview_and_avatar_are_not(self):
        row=self.page.locator('[data-e2e="conversation-item"]')
        row.evaluate("el=>el.insertAdjacentHTML('beforeend','<span style=\"color:red\">红色文字</span><div style=\"background:red;border-radius:50%;width:50px;height:50px;min-width:0;min-height:0\"></div>')")
        self.assertFalse(self.dom.conversation_states()[0]['unread'])
        row.evaluate("el=>el.insertAdjacentHTML('beforeend','<i style=\"display:block;background:rgb(254,44,85);border-radius:50%;width:8px;height:8px\"></i>')")
        self.assertTrue(self.dom.conversation_states()[0]['unread'])

    def test_selecting_current_chat_does_not_click_or_clear_its_badge(self):
        self.page.locator('[data-e2e="conversation-item"]').evaluate("el=>{el.dataset.clicks='0';el.onclick=()=>el.dataset.clicks=String(Number(el.dataset.clicks)+1)}")
        self.dom.select('朋友')
        self.assertEqual(self.page.locator('[data-e2e="conversation-item"]').get_attribute('data-clicks'),'0')

    def test_dom_activity_wakes_before_fallback_timeout(self):
        self.page.evaluate("()=>setTimeout(()=>document.querySelector('[data-e2e=conversation-item]').insertAdjacentHTML('beforeend','<span data-e2e=unread-count>1</span>'),150)")
        self.assertTrue(self.dom.wait_for_activity(3000))
        self.assertTrue(self.dom.conversation_states()[0]['unread'])

    def test_reverse_stacked_dom_is_returned_chronologically(self):
        context = self.browser.new_context()
        try:
            context.route('**/*', lambda route: route.fulfill(
                content_type='text/html; charset=utf-8', body=REVERSED))
            page = context.new_page()
            page.goto('https://www.douyin.com/')
            dom = DOMChat(page)
            dom.select('朋友')
            self.assertEqual([m['text'] for m in dom.read('朋友')], ['较早', '最新'])
        finally:
            context.close()


if __name__ == '__main__':
    unittest.main()

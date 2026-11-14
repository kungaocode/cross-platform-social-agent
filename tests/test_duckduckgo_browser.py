import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

from social_bot.modules.research import ResearchModule
from social_bot.modules.research.duckduckgo import DuckDuckGoError
from social_bot.modules.research.duckduckgo_browser import DuckDuckGoBrowserSearch, profile_lock, search_page

HTML = '<div class="result"><a class="result__a" href="https://www.python.org/">Python</a><a class="result__snippet">Official</a></div>'


class BrowserDeadlineTests(unittest.TestCase):
    def test_blocked_worker_is_killed_and_call_returns_within_budget(self):
        original_popen = subprocess.Popen
        children = []

        def spawn(args, **kwargs):
            if '--worker' in args:
                process = original_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
                children.append(process)
                return process
            return original_popen(args, **kwargs)

        with tempfile.TemporaryDirectory() as folder:
            client = DuckDuckGoBrowserSearch(folder, timeout_seconds=.65)
            client._ensure_browser = Mock(return_value=('http://127.0.0.1:1234', '/devtools/browser/test'))
            with patch('social_bot.modules.research.duckduckgo_browser.subprocess.Popen', side_effect=spawn):
                start = time.monotonic()
                with self.assertRaisesRegex(DuckDuckGoError, 'duckduckgo_timeout'):
                    client.search('Python', 3)
                elapsed = time.monotonic()-start
            self.assertLess(elapsed, 1.2)
            for process in children:
                process.wait(timeout=3)

    def test_timeout_also_counts_browser_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            client = DuckDuckGoBrowserSearch(folder, timeout_seconds=.1)

            def startup(deadline, **kwargs):
                time.sleep(.15)
                return 'http://127.0.0.1:1234', '/devtools/browser/test'

            client._ensure_browser = startup
            with patch('social_bot.modules.research.duckduckgo_browser.subprocess.Popen') as spawn:
                with self.assertRaisesRegex(DuckDuckGoError, 'duckduckgo_timeout'):
                    client.search('Python', 3)
            spawn.assert_not_called()

    def test_shared_profile_lock_has_bounded_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder)/'lock'
            with profile_lock(lock, time.monotonic()+1):
                with self.assertRaisesRegex(DuckDuckGoError, 'duckduckgo_timeout'):
                    with profile_lock(lock, time.monotonic()+.1):
                        self.fail('same profile lock acquired concurrently')

    def test_stale_port_cannot_attach_to_a_different_browser_session(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'DevToolsActivePort').write_text('1234\n/devtools/browser/old-session\n', encoding='utf-8')
            client = DuckDuckGoBrowserSearch(folder)
            with patch('social_bot.modules.research.duckduckgo_browser.http.client.HTTPConnection') as connection:
                connection.return_value.getresponse.return_value.read.return_value = json.dumps(
                    {'webSocketDebuggerUrl': 'ws://127.0.0.1:1234/devtools/browser/other-session'}).encode()
                self.assertIsNone(client._endpoint(time.monotonic()+1))


class BrowserPageTests(unittest.IsolatedAsyncioTestCase):
    def page(self, html):
        page = Mock()
        page.url = 'https://html.duckduckgo.com/html/'
        page.content = AsyncMock(return_value=html)
        page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
        return page

    async def test_verified_page_searches_in_same_page_and_returns_sources(self):
        page = self.page(HTML)
        query = 'Python 文档 & 测试'
        rows = await search_page(page, query, 3, time.time()+1)
        self.assertEqual(rows[0]['href'], 'https://www.python.org/')
        target = page.goto.call_args.args[0]
        self.assertEqual(parse_qs(urlsplit(target).query)['q'], [query])

    async def test_captcha_page_is_kept_for_human_without_navigation(self):
        page = self.page('<form id="challenge-form">manual verification</form>')
        with self.assertRaisesRegex(DuckDuckGoError, 'captcha_required'):
            await search_page(page, 'Python', 3, time.time()+1)
        page.goto.assert_not_called()

    async def test_empty_results_return_immediately(self):
        page = self.page('<div class="no-results">No matches</div>')
        start = time.monotonic()
        self.assertEqual(await search_page(page, 'Python', 3, time.time()+5), [])
        self.assertLess(time.monotonic()-start, 1)

    async def test_page_without_returned_content_expires(self):
        page = self.page('<html><body></body></html>')
        start = time.monotonic()
        with self.assertRaisesRegex(DuckDuckGoError, 'duckduckgo_timeout'):
            await search_page(page, 'Python', 3, time.time()+.15)
        self.assertLess(time.monotonic()-start, .6)


class BrowserFallbackTests(unittest.TestCase):
    def module(self, folder):
        module = ResearchModule(backends=('ddgs_duckduckgo', 'baidu_api', 'bocha_api'), duckduckgo_mode='browser',
                                duckduckgo_profile=folder)
        module._duck_browser = Mock()
        module._duck_browser.recovery_token.return_value = 'old-token'
        return module

    def test_duck_timeout_then_baidu_error_falls_back_to_bocha(self):
        with tempfile.TemporaryDirectory() as folder:
            module = self.module(folder)
            module._duck_browser.search.side_effect = DuckDuckGoError('duckduckgo_timeout')
            module._post_json = Mock(side_effect=[TimeoutError(), {'data': {'webPages': {'value': [
                {'name': 'Python', 'url': 'https://www.python.org/', 'summary': 'Official'}]}}}])
            with patch.dict(os.environ, BAIDU_SEARCH_API_KEY='test-baidu', BOCHA_API_KEY='test-bocha'):
                result = module.search('Python')
            self.assertEqual(result['provider'], 'bocha_api')
            self.assertEqual([x['status'] for x in result['attempts']], ['error', 'error', 'ok'])
            self.assertEqual(result['attempts'][0]['reason'], 'duckduckgo_timeout')

    def test_empty_or_contentless_duck_rows_immediately_fall_back(self):
        for rows in ([], [{'url': 'https://www.python.org/', 'title': ' ', 'body': ''}]):
            with tempfile.TemporaryDirectory() as folder:
                module = self.module(folder)
                module._duck_browser.search.return_value = rows
                module._post_json = Mock(return_value={'references': [{'title': 'Python', 'url': 'https://www.python.org/'}]})
                with patch.dict(os.environ, BAIDU_SEARCH_API_KEY='test-baidu'):
                    result = module.search('Python')
                self.assertEqual(result['provider'], 'baidu_api')
                self.assertEqual(result['attempts'][0]['status'], 'empty')

    def test_manual_verification_status_refresh_resumes_cooling_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            module = self.module(folder)
            module._duck_browser.search.side_effect = DuckDuckGoError('duckduckgo_captcha_required', cooldown_seconds=600)
            module._post_json = Mock(return_value={'references': [{'title': 'Python', 'url': 'https://www.python.org/'}]})
            with patch.dict(os.environ, BAIDU_SEARCH_API_KEY='test-baidu'):
                self.assertEqual(module.search('first')['provider'], 'baidu_api')
                module._duck_browser.recovery_token.return_value = 'new-verified-token'
                module._duck_browser.search.side_effect = None
                module._duck_browser.search.return_value = [{'title': 'Python', 'url': 'https://www.python.org/'}]
                result = module.search('after manual verification')
            self.assertEqual(result['provider'], 'ddgs_duckduckgo')
            self.assertEqual(result['attempts'], [{'provider': 'ddgs_duckduckgo', 'status': 'ok'}])


if __name__ == '__main__':
    unittest.main()

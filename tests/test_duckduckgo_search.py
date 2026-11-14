import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from social_bot.modules.research import ResearchModule
from social_bot.modules.research.duckduckgo import DuckDuckGoError, DuckDuckGoSearch

HTML = '''<html><div class="result results_links">
  <h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F">Python 文档</a></h2>
  <a class="result__snippet">官方文档，不包含第三方教程。</a></div></html>'''
LITE = '''<html><table><tr><td><a class="result-link" href="https://www.python.org/">Python</a></td></tr>
  <tr><td class="result-snippet">Python 官网</td></tr></table></html>'''


def response(text=HTML, status=200):
    return SimpleNamespace(status_code=status, text=text)


class DuckDuckGoSearchTests(unittest.TestCase):
    def test_get_search_extracts_sourced_html_and_decodes_redirects(self):
        client = Mock()
        client.request.return_value = response()
        rows = DuckDuckGoSearch(client=client).search('Python 文档', 3)
        self.assertEqual(rows, [{'title': 'Python 文档', 'href': 'https://docs.python.org/3/', 'body': '官方文档，不包含第三方教程。'}])
        self.assertEqual(client.request.call_args.args, ('GET', DuckDuckGoSearch.ENDPOINTS[0]))
        self.assertEqual(client.request.call_args.kwargs['params']['q'], 'Python 文档')

    def test_changed_html_uses_official_lite_get_once(self):
        client = Mock()
        client.request.side_effect = [response('<html><main>changed structure</main></html>'), response(LITE)]
        rows = DuckDuckGoSearch(client=client).search('Python', 3)
        self.assertEqual(rows[0]['href'], 'https://www.python.org/')
        self.assertEqual(rows[0]['body'], 'Python 官网')
        self.assertEqual(client.request.call_args_list[1].args, ('GET', DuckDuckGoSearch.ENDPOINTS[1]))

    def test_captcha_is_explicit_and_never_retried_on_another_entrypoint(self):
        client = Mock()
        client.request.return_value = response('<form id="challenge-form">verification</form>', 202)
        with self.assertRaises(DuckDuckGoError) as error:
            DuckDuckGoSearch(client=client).search('Python', 3)
        self.assertEqual(error.exception.reason, 'duckduckgo_captcha_required')
        self.assertEqual(error.exception.http_status, 202)
        self.assertEqual(error.exception.cooldown_seconds, 600)
        client.request.assert_called_once()

    def test_captcha_word_in_normal_results_is_not_a_challenge(self):
        client = Mock()
        client.request.return_value = response(HTML.replace('官方文档', 'captcha documentation'))
        self.assertEqual(len(DuckDuckGoSearch(client=client).search('captcha', 3)), 1)

    def test_rate_limit_and_denial_are_not_reported_as_empty_results(self):
        for status, reason in [(429, 'duckduckgo_rate_limited'), (403, 'duckduckgo_access_denied')]:
            client = Mock()
            client.request.return_value = response('unavailable', status)
            with self.assertRaises(DuckDuckGoError) as error:
                DuckDuckGoSearch(client=client).search('Python', 3)
            self.assertEqual(error.exception.reason, reason)
            self.assertEqual(error.exception.http_status, status)
            client.request.assert_called_once()

    def test_genuine_empty_results_are_returned_without_retry(self):
        client = Mock()
        client.request.return_value = response('<html><div class="no-results">No matches</div></html>')
        self.assertEqual(DuckDuckGoSearch(client=client).search('unusual query', 3), [])
        client.request.assert_called_once()

    def test_request_budget_stops_second_request_after_timeout(self):
        client = Mock()
        client.request.side_effect = TimeoutError('sensitive network text')
        clock = Mock(side_effect=[0, 0, 2])
        with self.assertRaises(DuckDuckGoError) as error:
            DuckDuckGoSearch(timeout_seconds=1, client=client, clock=clock).search('Python', 3)
        self.assertEqual(error.exception.reason, 'duckduckgo_timeout')
        self.assertNotIn('sensitive', str(error.exception))
        client.request.assert_called_once()

    def test_invalid_and_ad_links_are_excluded_and_duplicates_collapsed(self):
        html = HTML + HTML + '''<a class="result__a" href="javascript:alert(1)">Invalid</a>
          <a class="result__a" href="/y.js?ad=1">Ad</a><a class="result__a">Missing URL</a>'''
        self.assertEqual(len(DuckDuckGoSearch.parse(html, DuckDuckGoSearch.ENDPOINTS[0], 8)), 1)

    def test_empty_document_is_reported_as_page_change(self):
        with self.assertRaisesRegex(DuckDuckGoError, 'page_changed'):
            DuckDuckGoSearch.parse('', DuckDuckGoSearch.ENDPOINTS[0], 3)

    def test_challenge_falls_back_and_cools_down_only_duckduckgo(self):
        clock = [10]
        module = ResearchModule(backends=('ddgs_duckduckgo', 'baidu_api', 'bocha_api'), clock=lambda: clock[0])
        with patch.dict(os.environ, BAIDU_SEARCH_API_KEY='test-key'), \
             patch('social_bot.modules.research.duckduckgo.DuckDuckGoSearch') as duck:
            duck.return_value.search.side_effect = DuckDuckGoError('duckduckgo_captcha_required', http_status=202, cooldown_seconds=600)
            module._post_json = Mock(return_value={'references': [{'title': 'Python', 'url': 'https://www.python.org/'}]})
            first = module.search('Python')
            self.assertEqual(first['provider'], 'baidu_api')
            self.assertEqual(first['attempts'][0]['reason'], 'duckduckgo_captcha_required')
            self.assertEqual(first['attempts'][0]['http_status'], 202)
            clock[0] += 61
            second = module.search('Python')
            self.assertEqual(second['attempts'][0]['status'], 'cooldown')
            self.assertEqual(second['provider'], 'baidu_api')
            duck.return_value.search.assert_called_once()


if __name__ == '__main__':
    unittest.main()

import os
import unittest
from unittest.mock import Mock, patch

from social_bot.modules.research import ResearchModule
from social_bot.modules.research.module import BACKENDS


ROWS = [{"title": "Python", "href": "https://www.python.org/", "body": "Official"}]


class ResearchFallbackTests(unittest.TestCase):
    def test_errors_and_empty_results_fall_back_and_report_actual_provider(self):
        research = ResearchModule(backends=("bocha_api", "baidu_api", "ddgs_duckduckgo"))
        research._search_backend = Mock(side_effect=[TimeoutError("sensitive token"), [], ROWS])
        with patch.dict(os.environ, BOCHA_API_KEY='test-bocha', BAIDU_SEARCH_API_KEY='test-baidu'):
            result = research.search("Python")
        self.assertTrue(result["success"])
        self.assertEqual(result["provider"], "ddgs_duckduckgo")
        self.assertEqual([a["status"] for a in result["attempts"]], ["error", "empty", "ok"])
        self.assertNotIn("sensitive", str(result))

    def test_backend_recovers_after_cooldown(self):
        clock = [100]
        research = ResearchModule(backends=("bocha_api", "ddgs_duckduckgo"), clock=lambda: clock[0])
        research._search_backend = Mock(side_effect=[RuntimeError(), ROWS, ROWS, ROWS])
        with patch.dict(os.environ, BOCHA_API_KEY='test-bocha'):
            self.assertEqual(research.search("one")["provider"], "ddgs_duckduckgo")
            self.assertEqual(research.search("two")["attempts"][0]["status"], "cooldown")
            clock[0] += 61
            self.assertEqual(research.search("three")["provider"], "bocha_api")

    def test_empty_query_or_all_empty_never_claims_success(self):
        research = ResearchModule(backends=("bocha_api", "ddgs_duckduckgo"))
        research._search_backend = Mock(return_value=[])
        self.assertFalse(research.search("")["success"])
        research._search_backend.assert_not_called()
        self.assertEqual(research.search("query")["reason"], "all_search_backends_unavailable")
        self.assertFalse(research._failed_until)  # 空结果不应让其它查询也熔断。

    def test_optional_providers_skip_missing_credentials(self):
        research = ResearchModule(backends=("bocha_api", "baidu_api", "ddgs_duckduckgo"))
        research._search_backend = Mock(return_value=ROWS)
        with patch.dict(os.environ, {}, clear=True):
            result = research.search("Python")
        self.assertEqual([a["status"] for a in result["attempts"]], ["not_configured", "not_configured", "ok"])
        research._search_backend.assert_called_once_with("ddgs_duckduckgo", "Python", 5)

    def test_only_three_providers_are_supported_and_removed_names_are_rejected(self):
        self.assertEqual(BACKENDS, {'bocha_api', 'baidu_api', 'ddgs_duckduckgo'})
        for removed in ('brave_api', 'searxng', 'ddgs_bing', 'ddgs_brave'):
            with self.assertRaises(ValueError):
                ResearchModule(backends=(removed,))

    def test_duckduckgo_uses_own_adapter_without_auto_switching_engines(self):
        with patch('social_bot.modules.research.duckduckgo.DuckDuckGoSearch') as duck:
            duck.return_value.search.return_value = ROWS
            result = ResearchModule(backends=('ddgs_duckduckgo',)).search('Python')
        self.assertTrue(result['success'])
        duck.return_value.search.assert_called_once_with('Python', 5)

    def test_normalizer_filters_duplicates_and_invalid_urls(self):
        research = ResearchModule(searcher=lambda *_: ROWS + ROWS + [{"url": "javascript:alert(1)"}, "invalid"])
        result = research.search("test", 8)
        self.assertTrue(result["success"])
        self.assertEqual(len(result["results"]), 1)


if __name__ == "__main__":
    unittest.main()

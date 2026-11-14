import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler
from unittest.mock import Mock, patch

from social_bot.modules.research import ResearchModule
from social_bot.modules.research.module import DOMESTIC_ENDPOINTS, SearchProviderError


class DomesticSearchTests(unittest.TestCase):
    def test_two_domestic_formats_requests_and_normalized_sources(self):
        module = ResearchModule(backends=('bocha_api', 'baidu_api'))
        module._post_json = Mock(side_effect=[
            {'code': 200, 'data': {'webPages': {'value': [{'name': '博查标题', 'url': 'https://example.com', 'summary': '博查摘要'}]}}},
            {'references': [{'title': '百度标题', 'url': 'https://example.org', 'content': '百度摘要'}]}])
        with patch.dict(os.environ, BOCHA_API_KEY='test-bocha', BAIDU_SEARCH_API_KEY='test-baidu'):
            bocha = module._search_backend('bocha_api', '珊瑚礁', 3)
            baidu = module._search_backend('baidu_api', '珊瑚礁', 3)
        self.assertEqual(module._normalize(bocha, 3)[0]['title'], '博查标题')
        self.assertEqual(module._normalize(bocha, 3)[0]['snippet'], '博查摘要')
        self.assertEqual(module._normalize(baidu, 3)[0]['snippet'], '百度摘要')
        self.assertEqual(module._post_json.call_args_list[0].args[1]['count'], 3)
        self.assertEqual(module._post_json.call_args_list[1].args[1]['resource_type_filter'], [{'type': 'web', 'top_k': 3}])
        self.assertEqual(module._post_json.call_args_list[1].args[0], DOMESTIC_ENDPOINTS['baidu_api'])

    def test_keys_from_file_and_environment_priority_without_echo(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'search.json'
            path.write_text(json.dumps({'BOCHA_API_KEY': 'file-sensitive', 'BAIDU_SEARCH_API_KEY': ''}), encoding='utf-8')
            with patch.dict(os.environ, {'BOCHA_API_KEY': 'env-sensitive'}, clear=True):
                module = ResearchModule(backends=('bocha_api', 'baidu_api'), credentials_file=path)
                self.assertEqual(module._key('BOCHA_API_KEY'), 'env-sensitive')
                self.assertNotIn('sensitive', str(module.status()))
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(module._key('BOCHA_API_KEY'), 'file-sensitive')
                module._post_json = Mock(return_value={'references': []})
                result = module.search('test')
                self.assertEqual(result['attempts'][1]['status'], 'not_configured')

    def test_missing_credentials_skip_without_network(self):
        module = ResearchModule(backends=('bocha_api', 'baidu_api'))
        module._post_json = Mock()
        with patch.dict(os.environ, {}, clear=True):
            result = module.search('test')
        self.assertFalse(result['success'])
        self.assertEqual([x['status'] for x in result['attempts']], ['not_configured', 'not_configured'])
        module._post_json.assert_not_called()

    def test_invalid_credentials_file_does_not_break_other_search_backends(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'keys.json'
            path.write_text('{broken', encoding='utf-8')
            module = ResearchModule(backends=('bocha_api', 'ddgs_duckduckgo'), credentials_file=path)
            module._search_backend = Mock(return_value=[{'title': 'ok', 'url': 'https://example.com'}])
            with patch.dict(os.environ, {}, clear=True):
                result = module.search('test')
            self.assertEqual(module.credentials_status, 'invalid')
            self.assertEqual(result['provider'], 'ddgs_duckduckgo')

    def test_direct_post_bypasses_system_proxy_and_sets_bearer_header(self):
        module = ResearchModule()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"references": []}'
        with patch('social_bot.modules.research.module.build_opener') as factory:
            factory.return_value.open.return_value = response
            module._post_json(DOMESTIC_ENDPOINTS['baidu_api'], {'messages': []}, 'test-key')
        handlers = factory.call_args.args
        self.assertTrue(any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in handlers))
        request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
        self.assertEqual(request.method, 'POST')

    def test_http_or_business_error_falls_back_without_exposing_provider_body(self):
        module = ResearchModule(backends=('bocha_api', 'baidu_api'))
        module._post_json = Mock(side_effect=[HTTPError('https://api.bochaai.com', 401, 'sensitive', {}, None),
            {'references': [{'title': 'ok', 'url': 'https://example.com', 'content': 'found'}]}])
        with patch.dict(os.environ, BOCHA_API_KEY='sensitive', BAIDU_SEARCH_API_KEY='sensitive'):
            result = module.search('test')
        self.assertTrue(result['success'])
        self.assertEqual(result['provider'], 'baidu_api')
        self.assertEqual(result['attempts'][0]['http_status'], 401)
        self.assertNotIn('sensitive', str(result))

    def test_success_http_with_error_body_is_not_accepted(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"code":401,"message":"sensitive"}'
        with patch('social_bot.modules.research.module.build_opener') as factory:
            factory.return_value.open.return_value = response
            with self.assertRaises(SearchProviderError) as error:
                ResearchModule()._post_json(DOMESTIC_ENDPOINTS['bocha_api'], {}, 'test-key')
        self.assertNotIn('sensitive', str(error.exception))


if __name__ == '__main__':
    unittest.main()

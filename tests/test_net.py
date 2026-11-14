from __future__ import annotations

import os
import unittest
import urllib.error
import urllib.request
from unittest import mock
from social_bot import net
from social_bot.network_policy import direct_environment, enforce_direct_networking


class DirectNetworkTests(unittest.TestCase):
    def test_all_requests_ignore_environment_and_windows_proxy_detection(self):
        request=urllib.request.Request('https://example.com/api')
        opener=mock.Mock();opener.open.return_value='OK'
        with mock.patch.object(urllib.request,'getproxies',return_value={'https':'http://127.0.0.1:9'}), \
             mock.patch.object(urllib.request,'urlopen') as default_open, \
             mock.patch.object(urllib.request,'build_opener',return_value=opener) as build:
            self.assertEqual(net.urlopen(request,timeout=5),'OK')
        default_open.assert_not_called()
        self.assertEqual(build.call_args.args[0].proxies,{})
        self.assertEqual(opener.open.call_args.args[0].host,'example.com')
        self.assertEqual(request.host,'example.com')

    def test_transport_failure_never_falls_back_to_proxy_or_repeats_write(self):
        for prefer_direct in (False,True):
            request=urllib.request.Request('https://example.com/api',data=b'{}')
            error=TimeoutError('direct failed')
            with mock.patch.object(net,'_direct',side_effect=error) as direct, \
                 mock.patch.object(urllib.request,'urlopen') as default_open:
                with self.assertRaises(TimeoutError):
                    net.urlopen(request,timeout=5,prefer_direct=prefer_direct)
            direct.assert_called_once();default_open.assert_not_called()

    def test_http_rejection_is_not_retried(self):
        request=urllib.request.Request('https://example.com/api')
        error=urllib.error.HTTPError(request.full_url,403,'Forbidden',{},None)
        with mock.patch.object(net,'_direct',side_effect=error) as direct:
            with self.assertRaises(urllib.error.HTTPError):net.urlopen(request,timeout=5)
        direct.assert_called_once()

    def test_model_request_retains_shared_pool_and_releases_lease_on_error(self):
        request=urllib.request.Request('https://example.com/v1/chat/completions',data=b'{}')
        for fail in (False,True):
            pool=mock.Mock();response=mock.Mock()
            with mock.patch('social_bot.scheduler.configured_pool',return_value=pool), \
                 mock.patch.object(net,'_direct',side_effect=TimeoutError() if fail else None,return_value=response) as direct:
                if fail:
                    with self.assertRaises(TimeoutError):net.urlopen(request,timeout=5)
                else:
                    with net.urlopen(request,timeout=5):pass
            direct.assert_called_once();pool.acquire.assert_called_once()
            pool.acquire.return_value.close.assert_called_once()

    def test_worker_environment_strips_proxy_and_disables_registry_fallback(self):
        original={'HTTP_PROXY':'http://127.0.0.1:9','https_proxy':'http://127.0.0.1:9',
                  'ALL_PROXY':'socks5://127.0.0.1:9','DDGS_PROXY':'http://127.0.0.1:9','KEEP':'yes'}
        direct=direct_environment(original)
        self.assertEqual(original['HTTP_PROXY'],'http://127.0.0.1:9')
        self.assertEqual(direct,{'KEEP':'yes','NO_PROXY':'*','no_proxy':'*','NODE_USE_ENV_PROXY':'0'})
        with mock.patch.dict(os.environ,original,clear=True):
            enforce_direct_networking()
            self.assertFalse(any(k in urllib.request.getproxies() for k in ('http','https','all')))
            self.assertTrue(urllib.request.proxy_bypass('example.com'))


if __name__=='__main__':unittest.main()

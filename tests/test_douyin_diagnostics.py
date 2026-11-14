import json
from types import SimpleNamespace
import unittest

from social_bot.platforms.douyin.backend_worker import response_diagnostic, response_error, classify


class DiagnosticTests(unittest.TestCase):
    def diagnostic(self,status=200,headers=None,body=b''):
        return response_diagnostic('POST','https://www.douyin.com/aweme/v1/web/comment/publish?token=SECRET',
            {'Cookie':'sessionid=SECRET','x-tt-session-dtrait':'SECRET'},
            SimpleNamespace(status_code=status,headers=headers or {},content=body))

    def test_http_rejections_do_not_masquerade_as_captcha(self):
        for status,code in [(401,'AUTH_EXPIRED'),(403,'FORBIDDEN'),(412,'PRECONDITION_FAILED'),(429,'RATE_LIMITED')]:
            self.assertEqual(response_error(self.diagnostic(status)), 'DOUYIN_'+code)

    def test_captcha_and_identity_require_explicit_response_markers(self):
        self.assertEqual(response_error(self.diagnostic(403,{'X-Vc-Bdturing-Parameters':'SECRET'})),
                         'DOUYIN_CAPTCHA_REQUIRED')
        self.assertEqual(response_error(self.diagnostic(200,{'X-Tt-Verify-Passport-Decision':'SECRET'})),
                         'DOUYIN_IDENTITY_VERIFICATION_REQUIRED')

    def test_business_rejection_and_identity_code_are_recorded_without_secrets(self):
        d=self.diagnostic(body=b'{"status_code":8,"data":{"error_code":3,"token":"SECRET"}}')
        self.assertEqual(d['business_status'],8)
        self.assertEqual(d['identity_error_code'],3)
        self.assertIsNone(response_error(d))
        self.assertNotIn('SECRET',json.dumps(d))
        self.assertNotIn('token=',json.dumps(d))
        self.assertEqual(d['request_guard']['x-tt-session-dtrait'],True)

    def test_device_material_error_is_not_captcha(self):
        self.assertEqual(classify(RuntimeError('dtrait 构造失败，验证数据无效 token=SECRET')),
                         'DOUYIN_DEVICE_MATERIAL_INVALID')
        self.assertEqual(classify(RuntimeError('参数验证失败 secret=SECRET')),'DOUYIN_BACKEND_REQUEST_FAILED')


if __name__=='__main__':
    unittest.main()

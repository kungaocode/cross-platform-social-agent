import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from social_bot.platforms.qq.media import QQMediaReader, MediaError, NoRedirect, safe_url
from social_bot.settings import AppSettings, DeepSeekSettings, RuntimeSettings, ScopeSettings, WeChatSettings


class MediaTests(unittest.TestCase):
    def setUp(self):
        settings = AppSettings(api_key='fake', deepseek=DeepSeekSettings(), scope=ScopeSettings(),
                               runtime=RuntimeSettings(), wechat=WeChatSettings())
        self.reader = QQMediaReader(settings)
        self.reader.vision = Mock(return_value='看到测试图片')
        self.reader.asr = Mock(return_value='周六下午集合')

    def test_rejects_unsafe_hosts_ports_and_private_dns(self):
        for value in ('http://gchat.qpic.cn/a', 'https://localhost/a', 'https://qq.com.evil.test/a',
                      'https://a.qq.com:444/a', 'https://u:p@a.qq.com/a', 'file:///C:/secret'):
            with self.subTest(value=value), self.assertRaises(MediaError): safe_url(value)
        with patch('socket.getaddrinfo', return_value=[(0,0,0,'',('127.0.0.1',443))]), self.assertRaises(MediaError):
            safe_url('https://gchat.qpic.cn/a')
        with patch('socket.getaddrinfo', return_value=[(0,0,0,'',('1.1.1.1',443))]):
            self.assertEqual(safe_url('//gchat.qpic.cn/a'), 'https://gchat.qpic.cn/a')
            self.assertEqual(safe_url('https://multimedia.nt.qq.com.cn/download'), 'https://multimedia.nt.qq.com.cn/download')
            self.assertEqual(safe_url('https://qqbot.ugcimg.cn/a.wav'), 'https://qqbot.ugcimg.cn/a.wav')

    def test_redirect_is_refused(self):
        with self.assertRaises(MediaError): NoRedirect().redirect_request(None,None,302,'',{},'https://localhost')

    def test_native_qq_transcript_does_not_download_or_call_asr(self):
        with patch.object(self.reader, '_download') as download:
            result = self.reader.read([{'content_type':'voice', 'asr_refer_text':'你好世界'}])
        self.assertIn('你好世界', result)
        download.assert_not_called()
        self.reader.asr.assert_not_called()

    def test_wav_url_is_preferred_and_temporary_file_is_removed(self):
        observed = []
        def download(url, folder, **kwargs):
            self.assertEqual(url, 'https://a.qq.com/converted.wav')
            self.assertTrue(kwargs['wav'])
            p=Path(folder)/'audio.wav'; p.write_bytes(b'RIFFtestWAVEdata'); observed.append(p); return p
        with patch.object(self.reader,'_download',side_effect=download):
            result=self.reader.read([{'content_type':'voice','url':'https://a.qq.com/raw.silk','voice_wav_url':'https://a.qq.com/converted.wav'}])
        self.assertIn('周六下午集合',result)
        self.assertTrue(observed[0].exists())

    def test_cleanup_after_image_model_failure(self):
        observed=[]
        def download(url,folder,**kwargs):
            p=Path(folder)/'image.png';p.touch();observed.append(p);return p
        self.reader.vision.side_effect=RuntimeError('provider error')
        with patch.object(self.reader,'_download',side_effect=download),self.assertRaises(RuntimeError):
            self.reader.read([{'content_type':'image/png','url':'https://gchat.qpic.cn/p'}])
        self.assertTrue(observed[0].exists())

    def test_disabled_media_unsupported_types_and_excess_attachments(self):
        self.reader.image_enabled=False
        for attachments in ([{'content_type':'image/png'}], [{'content_type':'application/pdf'}], [{},{},{}]):
            with self.subTest(attachments=attachments),self.assertRaises(MediaError): self.reader.read(attachments)

    def test_download_checks_payload_size_and_image_signature(self):
        with tempfile.TemporaryDirectory() as folder,patch('social_bot.platforms.qq.media.safe_url',side_effect=lambda x:x),patch('social_bot.platforms.qq.media.build_opener') as opener:
            response=Mock();response.headers={};response.read.return_value=b'<html>not an image</html>'
            opener.return_value.open.return_value.__enter__.return_value=response
            with self.assertRaises(MediaError):self.reader._download('https://gchat.qpic.cn/p',folder,image=True)
            response.headers={'Content-Length':str(13*1024*1024)}
            with self.assertRaises(MediaError):self.reader._download('https://gchat.qpic.cn/p',folder,image=True)

    def test_mp4_pipeline_and_temporary_cleanup(self):
        paths=[]
        def download(url,folder,**kwargs):
            self.assertTrue(kwargs['video'])
            p=Path(folder)/'test.mp4';p.touch();paths.append(p);return p
        self.reader.video=Mock()
        self.reader.video.analyze_path.return_value={'text':'视频中一辆小车驶过街道。','note':'采样分析。'}
        with patch.object(self.reader,'_download',side_effect=download):
            answer=self.reader.read([{'content_type':'video/mp4','url':'https://a.qq.com/a.mp4'}])
        self.assertIn('小车',answer)
        self.assertIn('MP4',answer)
        self.assertTrue(paths[0].exists())

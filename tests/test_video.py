import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from social_bot.modules.video import OmniVideoAnalyzer, VideoModule, VideoError
from social_bot.message_types import infer_message_type
from social_bot.tools import WeChatToolExecutor


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / 'test.mp4'
        subprocess.run([OmniVideoAnalyzer._ffmpeg(), '-nostdin', '-v', 'error', '-f', 'lavfi',
            '-i', 'color=c=blue:s=64x64:r=2', '-t', '3', '-c:v', 'libx264', str(self.path)],
            check=True, capture_output=True, timeout=20)
        self.analyzer = OmniVideoAnalyzer('secret', 'https://example.test/api/v1/services/aigc/multimodal-generation/generation')

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_mp4_probe_and_input_kept_intact(self):
        original = self.path.read_bytes()
        with patch.object(self.analyzer, '_request', return_value='蓝色画面，没有声音。') as request:
            result = self.analyzer.analyze_path(self.path)
        self.assertTrue(result['success'])
        self.assertEqual(result['coverage_seconds'], 3)
        self.assertEqual(result['segments'], 1)
        self.assertFalse(result['has_audio'])
        self.assertTrue(request.call_args.args[0][0]['video_url']['url'].startswith('data:video/mp4;base64,'))
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_mp4_and_oversize_are_rejected_before_request(self):
        bad = self.root / 'bad.mp4'; bad.write_bytes(b'not a video')
        with self.assertRaises(VideoError): self.analyzer.analyze_path(bad)
        with patch.object(self.analyzer, 'MAX_SOURCE_BYTES', 1), self.assertRaises(VideoError):
            self.analyzer.analyze_path(self.path)

    def test_video_facts_are_organized_by_task_model(self):
        self.analyzer.summarizer=Mock(return_value='Flash整理的资料')
        self.analyzer.summary_model='deepseek-flash'
        with patch.object(self.analyzer,'_request',return_value='Omni视觉证据') as perception:
            result=self.analyzer.analyze_path(self.path)
        perception.assert_called_once()
        self.analyzer.summarizer.assert_called_once()
        self.assertIn('Omni视觉证据',str(self.analyzer.summarizer.call_args))
        self.assertEqual(result['text'],'Flash整理的资料')
        self.assertEqual(result['summary_model'],'deepseek-flash')

    def test_stream_payload_and_response_validation(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.__iter__ = Mock(return_value=iter([
            b'data: {"choices":[{"delta":{"content":"answer"}}]}\n', b'data: [DONE]\n']))
        with patch('social_bot.modules.video.module.net.urlopen', return_value=response) as opened:
            self.assertEqual(self.analyzer._request([{'type':'text','text':'hello'}]), 'answer')
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url,'https://example.test/compatible-mode/v1/chat/completions')
        data = json.loads(request.data)
        self.assertEqual(data['model'],'qwen3.8-omni-flash')
        self.assertEqual(data['modalities'],['text'])
        response.__iter__ = Mock(return_value=iter([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n']))
        with patch('social_bot.modules.video.module.net.urlopen',return_value=response),self.assertRaises(VideoError):
            self.analyzer._request([])

    def test_wechat_downloaded_temp_is_cleaned_even_after_failure(self):
        paths=[]
        def download(**kwargs):
            p=Path(kwargs['dir_path'])/'test.mp4'; p.write_bytes(self.path.read_bytes()); paths.append(p); return str(p)
        analyzer=Mock();analyzer.analyze_path.side_effect=RuntimeError('private-provider-detail')
        result=VideoModule(analyzer).read_message(SimpleNamespace(download=download))
        self.assertFalse(result['success'])
        self.assertNotIn('private',str(result))
        self.assertTrue(paths[0].exists())
        self.assertTrue(self.path.exists())

    def test_wechat_video_tool_switch_and_modes(self):
        message=SimpleNamespace(type='file',content='clip.mp4',file_path=str(self.path))
        self.assertEqual(infer_message_type(message),'video')
        module=VideoModule(Mock())
        module.analyzer.analyze_path.return_value={'success':True,'text':'蓝色画面','action':'video'}
        executor=WeChatToolExecutor(Mock(),message,Mock(),allow_video=True,video_module=module)
        self.assertIn('wechat_read_video',{x['function']['name'] for x in executor.definitions})
        self.assertTrue(executor.execute('wechat_read_video',{})['success'])
        self.assertTrue(self.path.exists())
        disabled=WeChatToolExecutor(Mock(),message,Mock(),allow_video=False,video_module=module)
        self.assertFalse(disabled.execute('wechat_read_video',{})['success'])
        mention=WeChatToolExecutor(Mock(),message,Mock(),allow_video=True,video_module=module,multimodal_mode='mention',is_mentioned=False)
        self.assertNotIn('wechat_read_video',{x['function']['name'] for x in mention.definitions})

    def test_absent_model_does_not_download(self):
        message=Mock()
        self.assertFalse(VideoModule().read_message(message)['success'])
        message.download.assert_not_called()

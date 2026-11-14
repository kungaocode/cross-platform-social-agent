import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from social_bot.cache import cache_task
from social_bot.modules.document import DocumentModule
from social_bot.platforms.qq.media import QQMediaReader, MediaError, attachment_kind, document_suffix
from social_bot.settings import AppSettings, DeepSeekSettings, RuntimeSettings, ScopeSettings, WeChatSettings


class QQDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        settings = AppSettings(api_key='offline', deepseek=DeepSeekSettings(), scope=ScopeSettings(),
                               runtime=RuntimeSettings(), wechat=WeChatSettings())
        self.reader = QQMediaReader(settings)
        self.reader.documents = DocumentModule(cache_dir=self.root/'cache',
            text_analyzer=lambda messages: '分析：' + messages[0]['content'])
        self.cache = patch('social_bot.platforms.qq.media.cache_task',
                           side_effect=lambda category: cache_task(category, root=self.root/'downloads'))
        self.cache.start()

    def tearDown(self):
        self.cache.stop()
        self.temp.cleanup()

    def read_bytes(self, suffix, data, question='总结文件'):
        response = Mock()
        response.headers = {'Content-Length': str(len(data))}
        response.read.side_effect = io.BytesIO(data).read
        with patch('social_bot.platforms.qq.media.safe_url', side_effect=lambda url: url), \
                patch('social_bot.platforms.qq.media.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = response
            return self.reader.read([{'filename':'资料'+suffix, 'url':'https://a.qq.com/download'}], question)

    def test_recognizes_extensions_mime_fallback_and_rejects_unsupported_named_files(self):
        for suffix in ('.PDF', '.docx', '.xlsx', '.xls', '.pptx', '.txt', '.md', '.csv', '.json', '.xml', '.html'):
            self.assertEqual(attachment_kind({'filename':'报告'+suffix}), 'document')
        self.assertEqual(document_suffix({'content_type':'application/pdf', 'url':'https://a.qq.com/opaque.bin'}), '.pdf')
        self.assertEqual(document_suffix({'url':'https://a.qq.com/report%2Epdf?key=secret'}), '.pdf')
        self.assertIsNone(attachment_kind({'filename':'bad.exe', 'content_type':'application/pdf'}))
        self.assertIsNone(attachment_kind({'filename':'old.doc'}))
        self.assertEqual(attachment_kind({'filename':'movie.mp4'}), 'video')

    def test_real_docx_xlsx_and_pptx_download_parse_and_analyze(self):
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation
        doc = Document(); doc.add_paragraph('项目代号为北极星，预算73万元。')
        book = Workbook(); book.active.append(['北极星', 73])
        deck = Presentation(); deck.slides.add_slide(deck.slide_layouts[0]).shapes.title.text = '北极星计划'
        for suffix, artifact in (('.docx', doc), ('.xlsx', book), ('.pptx', deck)):
            data = io.BytesIO(); artifact.save(data)
            with self.subTest(suffix=suffix):
                answer = self.read_bytes(suffix, data.getvalue())
                self.assertIn('北极星', answer)
                self.assertIn('分析', answer)
        self.assertTrue(list((self.root/'downloads').rglob('*.docx')))

    def test_scanned_pdf_uses_same_file_analyzer_as_wechat(self):
        from pypdf import PdfWriter
        data = io.BytesIO(); writer = PdfWriter(); writer.add_blank_page(width=200, height=200); writer.write(data)
        perception = Mock()
        perception.analyze_file.return_value = '扫描页识别到代号：北极星'
        self.reader.documents.analyzer = perception
        answer = self.read_bytes('.pdf', data.getvalue())
        self.assertIn('扫描页识别', answer)
        path, question = perception.analyze_file.call_args.args
        self.assertTrue(Path(path).exists())
        self.assertEqual(question, '总结文件')

    def test_office_visual_route_uses_shared_conversion(self):
        from docx import Document
        from PIL import Image
        image = io.BytesIO(); Image.new('RGB', (3, 3), 'blue').save(image, format='PNG'); image.seek(0)
        doc = Document(); doc.add_paragraph('图表说明'); doc.add_picture(image)
        data = io.BytesIO(); doc.save(data)
        perception = Mock(); perception.analyze_file.return_value = '图中为蓝色方块'
        self.reader.documents.analyzer = perception
        converted = self.root/'converted.pdf'; converted.write_bytes(b'pdf fixture')
        with patch.object(self.reader.documents, '_convert_to_pdf', return_value=converted) as convert:
            self.assertIn('蓝色方块', self.read_bytes('.docx', data.getvalue()))
        convert.assert_called_once()
        perception.analyze_file.assert_called_once_with(str(converted), '总结文件')

    def test_disabled_corrupt_and_empty_documents_do_not_reach_analyzer(self):
        self.reader.document_enabled = False
        with patch.object(self.reader, '_download') as download, self.assertRaises(MediaError):
            self.reader.read([{'filename':'report.pdf'}])
        download.assert_not_called()
        self.reader.document_enabled = True
        self.reader.documents.text_analyzer = Mock()
        with self.assertRaises(MediaError):
            self.read_bytes('.docx', b'not a document')
        self.reader.documents.text_analyzer.assert_not_called()
        with self.assertRaises(MediaError):
            self.read_bytes('.txt', b'')

    def test_model_failure_falls_back_to_text_and_reports_truncation(self):
        self.reader.documents.text_analyzer = Mock(side_effect=RuntimeError('private-provider-token'))
        answer = self.read_bytes('.txt', ('北极星'*5000).encode())
        self.assertIn('仅为提取的文字', answer)
        self.assertIn('截断', answer)
        self.assertNotIn('private-provider-token', answer)
        self.assertLess(len(answer), 3100)

    def test_download_limits_with_and_without_content_length(self):
        for suffix, mb in (('.pdf', 150), ('.docx', 30)):
            response = Mock(); response.headers = {'Content-Length': str((mb+1)*1024*1024)}
            with patch('social_bot.platforms.qq.media.safe_url', side_effect=lambda url: url), \
                    patch('social_bot.platforms.qq.media.build_opener') as opener:
                opener.return_value.open.return_value.__enter__.return_value = response
                with self.assertRaises(MediaError):
                    self.reader._download('https://a.qq.com/file', self.root, suffix=suffix, document=True)
                response.read.assert_not_called()
                response.headers = {}
                response.read.side_effect = [b'a'*(1024*1024)]*(mb+1)
                with self.assertRaises(MediaError):
                    self.reader._download('https://a.qq.com/file', self.root, suffix=suffix, document=True)


if __name__ == '__main__':
    unittest.main()

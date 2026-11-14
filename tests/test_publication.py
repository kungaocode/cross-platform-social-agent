import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_publication as publication


class PublicationTests(unittest.TestCase):
    def test_index_guard_checks_paths_known_values_and_does_not_print_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            secret = 'synthetic-' + 'A'*32
            (root/'private').mkdir()
            (root/'private/accounts').mkdir()
            (root/'private/accounts/test.json').write_text(json.dumps({'provider_specific_field':secret}))
            (root/'README.md').write_text('Example ' + secret)
            (root/'config.json').write_text('{}')
            subprocess.run(['git','-C',str(root),'add','README.md','config.json'], check=True)
            output = io.StringIO()
            with patch.object(publication, 'ROOT', root), patch('sys.argv', ['check']), contextlib.redirect_stdout(output):
                self.assertEqual(publication.main(), 1)
            self.assertIn('known_local_value', output.getvalue())
            self.assertIn('private_or_runtime_path', output.getvalue())
            self.assertNotIn(secret, output.getvalue())
            (root/'README.md').write_text('Sanitized documentation')
            subprocess.run(['git','-C',str(root),'rm','--cached','-q','config.json'], check=True)
            subprocess.run(['git','-C',str(root),'add','README.md'], check=True)
            with patch.object(publication, 'ROOT', root), patch('sys.argv', ['check']), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(publication.main(), 0)

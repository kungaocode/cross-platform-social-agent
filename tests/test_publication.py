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
    def test_history_guard_detects_removed_logs_and_past_exposure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root/'private/accounts').mkdir(parents=True)
            secret = 'synthetic-' + 'B'*32
            (root/'private/accounts/test.json').write_text(json.dumps({'token':secret}))
            (root/'logs').mkdir()
            (root/'logs/debug.txt').write_text('Captured session: '+secret)
            (root/'README.md').write_text('Public documentation')
            def commit(message):
                subprocess.run(['git','-C',str(root),'-c','user.name=Audit Fixture','-c','user.email=fixture@example.invalid',
                                'commit','-qm',message], check=True)
            subprocess.run(['git','-C',str(root),'add','README.md','logs/debug.txt'], check=True)
            commit('Initial fixture')
            subprocess.run(['git','-C',str(root),'rm','-q','logs/debug.txt'], check=True)
            commit('Remove log')
            with patch.object(publication, 'ROOT', root), patch('sys.argv', ['check','--head']), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(publication.main(), 0)
            output = io.StringIO()
            with patch.object(publication, 'ROOT', root), patch('sys.argv', ['check','--history']), contextlib.redirect_stdout(output):
                self.assertEqual(publication.main(), 1)
            self.assertIn('logs/debug.txt', output.getvalue())
            self.assertIn('known_local_value', output.getvalue())
            self.assertNotIn(secret, output.getvalue())

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

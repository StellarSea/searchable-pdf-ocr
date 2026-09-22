"""Publication guard must catch tracked private data, including deleted history."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from check_publication import LEGACY_ENV, broken_links, check_blob, check_history, check_tree


class PublicationTests(unittest.TestCase):
    def test_settings_and_user_data_are_private_but_template_is_public(self):
        for name in ('compose/.env', '.env.production', 'reviews/layout.json',
                     'docs/fixtures/book.json', 'custom/result.pdf', 'cache.sqlite3-wal',
                     'input/book.json', 'tools/build_toc_review.py'):
            self.assertTrue(check_blob(name, b'{}'), name)
        self.assertEqual(check_blob('compose/.env.example', b'KEY=example'), [])
        old = b'\n'.join(sorted(LEGACY_ENV))
        self.assertEqual(check_blob('compose/.env', old, historical=True), [])
        self.assertTrue(check_blob('compose/.env', old + b'\nTOKEN=value', historical=True))

    def test_secret_value_is_not_printed(self):
        token = b'ghp_' + b'A' * 36
        issues = check_blob('config.py', token)
        self.assertTrue(issues)
        self.assertNotIn(token.decode(), ' '.join(issues))

    def test_links_cannot_depend_on_private_local_files(self):
        candidates = {'README.md', 'docs/README.md'}
        self.assertEqual(broken_links('docs/README.md', '[home](../README.md)', candidates), [])
        self.assertTrue(broken_links('README.md', '[private](reviews/book.json)', candidates))
        self.assertEqual(broken_links('README.md', '[web](https://example.org)\n'
                                     '```\n[example](not-a-file)\n```', candidates), [])

    def test_deleted_credential_remains_detectable_in_git_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(['git', *args], cwd=root, check=True,
                                      capture_output=True)
            git('init')
            git('config', 'user.name', 'Test Author')
            git('config', 'user.email', 'test@example.invalid')
            path = root / 'config.txt'
            path.write_bytes(b'ghp_' + b'A' * 36)
            git('add', 'config.txt')
            git('commit', '-m', 'fixture')
            path.unlink()
            git('add', '-u')
            git('commit', '-m', 'remove fixture')
            self.assertEqual(check_tree(root)[1], [])
            commits, _, issues = check_history(root)
            self.assertEqual(commits, 2)
            self.assertTrue(any('possible credential' in issue for issue in issues))


if __name__ == '__main__':
    unittest.main()

"""Installation diagnosis must remain read-only and usable without OCR packages."""
from importlib import metadata
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'compose'))
from ocr_doctor import diagnose, LINE_MODELS, MODEL_FILES


class DoctorTests(unittest.TestCase):
    def test_host_missing_package_is_actionable_without_service_calls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'requirements.txt').write_text('requests\npymupdf\n', encoding='utf-8')

            def version(package):
                if package == 'pymupdf':
                    raise metadata.PackageNotFoundError(package)
                return 'tested'

            def forbidden(*args, **kwargs):
                self.fail('Host diagnosis must not access services')

            report = diagnose(root, package_version=version, command=forbidden,
                              which=forbidden, fetch_health=forbidden)
            self.assertFalse(report['ok'])
            missing = next(check for check in report['checks'] if check['name'] == 'pymupdf')
            self.assertIn(sys.executable, missing['action'])
            self.assertEqual(sorted(path.name for path in root.iterdir()), ['requirements.txt'])

    def test_services_only_probe_health_and_read_only_commands(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'requirements.txt').write_text('', encoding='utf-8')
            for model in LINE_MODELS:
                directory = root / 'models' / 'official_models' / model
                directory.mkdir(parents=True)
                for name in MODEL_FILES:
                    (directory / name).write_bytes(b'model-fixture')
            before = {path.relative_to(root): path.read_bytes()
                      for path in root.rglob('*') if path.is_file()}
            commands, requests = [], []

            def command(argv, **kwargs):
                commands.append(argv)
                self.assertLessEqual(kwargs['timeout'], 5)
                return SimpleNamespace(returncode=0)

            def health(base):
                requests.append(base)
                return dict(status='ok', models=dict(zip(('default', 'korean'), LINE_MODELS)))

            report = diagnose(root, services=True, command=command,
                              which=lambda name: 'docker', fetch_health=health)
            self.assertTrue(report['ok'])
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][-2:], ['config', '--quiet'])
            self.assertEqual(commands[1][1], 'info')
            self.assertEqual(requests, ['http://127.0.0.1:8080', 'http://127.0.0.1:8081'])
            self.assertEqual(before, {path.relative_to(root): path.read_bytes()
                                     for path in root.rglob('*') if path.is_file()})

    def test_missing_models_timeout_loading_and_invalid_health_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'requirements.txt').write_text('', encoding='utf-8')

            def command(argv, **kwargs):
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'])

            for response in ({'status': 'loading'}, {'errorCode': 1}, [], {},
                             {'anything': 'else'},
                             {'status': 'ok', 'models': {'default': 'wrong'}}):
                with self.subTest(response=response):
                    report = diagnose(root, services=True, command=command,
                                      which=lambda name: 'docker', fetch_health=lambda base: response)
                    failed = {check['name'] for check in report['checks'] if check['status'] == 'error'}
                    self.assertFalse(report['ok'])
                    self.assertTrue(set(LINE_MODELS) | {'compose', 'docker', 'line-api'} <= failed)
                    if response != {'status': 'ok', 'models': {'default': 'wrong'}}:
                        self.assertIn('document-api', failed)

    def test_dispatcher_diagnosis_works_without_site_packages(self):
        result = subprocess.run([sys.executable, '-S', str(ROOT / 'run.py'), 'doctor', '--json'],
                                capture_output=True, text=True, timeout=10)
        import json
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertFalse(report['ok'])
        self.assertTrue(any(check['status'] == 'error' for check in report['checks']))


if __name__ == '__main__':
    unittest.main()

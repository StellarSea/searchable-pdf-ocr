"""Read-only installation checks, usable before host dependencies are installed."""
import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
LINE_MODELS = ('PP-OCRv5_mobile_rec', 'korean_PP-OCRv5_mobile_rec')
MODEL_FILES = ('inference.json', 'inference.pdiparams', 'inference.yml')


def health_json(base):
    """Probe health only; never submit inference or start a service."""
    with urlopen(base.rstrip('/') + '/health', timeout=3) as response:
        return json.load(response)


def diagnose(root=ROOT, *, services=False, api='http://127.0.0.1:8080',
             line_api='http://127.0.0.1:8081', package_version=metadata.version,
             command=subprocess.run, which=shutil.which, fetch_health=health_json):
    """Return actionable checks. Callbacks isolate metadata, processes and HTTP."""
    root = Path(root)
    checks = []
    python_command = ("& '" + sys.executable.replace("'", "''") + "'"
                      if os.name == 'nt' else shlex.quote(sys.executable))

    def add(name, status, detail, action=''):
        checks.append(dict(name=name, status=status, detail=detail, action=action))

    tested = sys.version_info[:2] == (3, 13) and sys.platform == 'win32'
    add('python', 'ok' if tested else 'warning',
        f'{sys.version.split()[0]} / {sys.platform}',
        '' if tested else 'The verified host is Windows / Python 3.13; see docs/SETUP.md.')
    for line in (root / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith(('#', '-')):
            continue
        package = re.split(r'[<>=!~\[;\s]', line.strip(), maxsplit=1)[0]
        try:
            add(package, 'ok', package_version(package))
        except metadata.PackageNotFoundError:
            add(package, 'error', 'Package is not installed in this Python.',
                f'{python_command} -m pip install -r requirements.txt -c constraints-tested.txt')

    if services:
        for model in LINE_MODELS:
            directory = root / 'models' / 'official_models' / model
            missing = [name for name in MODEL_FILES
                       if not (directory / name).is_file() or (directory / name).stat().st_size == 0]
            add(model, 'error' if missing else 'ok',
                'Missing/empty: ' + ', '.join(missing) if missing else 'Required model files present.',
                'Download the official revision using docs/SETUP.md.' if missing else '')

        docker = which('docker')
        if not docker:
            add('docker', 'error', 'Docker CLI was not found.', 'Install GPU-enabled Docker Desktop.')
        else:
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            probes = (
                ('compose', [docker, 'compose', '--project-directory', str(root / 'compose'),
                             '-f', str(root / 'compose' / 'compose.yaml'), 'config', '--quiet'],
                 'Prepare compose/.env and review image selectors in docs/SETUP.md.'),
                ('docker', [docker, 'info', '--format', '{{.ServerVersion}}'],
                 'Start Docker Desktop with Linux containers and GPU support.'),
            )
            for name, argv, action in probes:
                try:
                    result = command(argv, capture_output=True, text=True, timeout=5,
                                     creationflags=flags)
                    ok = result.returncode == 0
                    add(name, 'ok' if ok else 'error',
                        'Read-only probe passed.' if ok else 'Read-only probe failed.',
                        '' if ok else action)
                except (OSError, subprocess.TimeoutExpired):
                    add(name, 'error', 'Read-only probe could not complete within 5 seconds.', action)

        for name, base in (('document-api', api), ('line-api', line_api)):
            try:
                health = fetch_health(base)
                ok = isinstance(health, dict) and health.get('errorCode', 0) == 0
                if name == 'line-api':
                    models = health.get('models') if isinstance(health, dict) else None
                    ok = (ok and health.get('status') == 'ok' and isinstance(models, dict)
                          and models.get('default') == LINE_MODELS[0]
                          and models.get('korean') == LINE_MODELS[1])
                elif isinstance(health, dict):
                    status = health.get('status')
                    ok = ok and (status == 'ok' or
                                 (status is None and health.get('errorCode') == 0))
                add(name, 'ok' if ok else 'error',
                    'Health is ready.' if ok else 'Health response is not ready or models differ.',
                    '' if ok else 'Check service startup/model logs using docs/SETUP.md.')
            except (URLError, OSError, ValueError):
                add(name, 'error', 'Health endpoint is unavailable or returned invalid JSON.',
                    'Check service address and startup logs using docs/SETUP.md.')

    return dict(mode='services' if services else 'host',
                ok=all(check['status'] != 'error' for check in checks), checks=checks)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--services', action='store_true',
                        help='also check local models, Compose, Docker and health endpoints')
    parser.add_argument('--api', default='http://127.0.0.1:8080')
    parser.add_argument('--line-api', default='http://127.0.0.1:8081')
    parser.add_argument('--json', action='store_true', help='print a structured diagnostic report')
    args = parser.parse_args(argv)
    report = diagnose(services=args.services, api=args.api, line_api=args.line_api)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report['checks']:
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
            if check['action']:
                print('  ' + check['action'])
        print('Host metadata checked; run offline regressions to verify imports and behavior.'
              if not args.services else
              'Read-only checks finished; health does not establish OCR accuracy or GPU compatibility.')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

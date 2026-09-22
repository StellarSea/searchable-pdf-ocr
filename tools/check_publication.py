"""Check Git publication candidates without reading ignored user data.

This is a small repository guard, not a complete secret or copyright scanner.
History checks inspect reachable blobs; no values from a match are printed.
"""
import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {'input', 'output', 'models', 'scans', 'reviews', 'tmp',
                '.ocr', '.ocr_cache', 'ocr_output', '.venv', 'venv', '__pycache__'}
PRIVATE_SUFFIXES = {'.pdf', '.tif', '.tiff', '.sqlite3', '.pem', '.key', '.pyc', '.log'}
SECRET_PATTERNS = (
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    re.compile(rb'\bAKIA[0-9A-Z]{16}\b'),
    re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b'),
)
# This exact historical configuration contains image selectors, not credentials.
# Keep old commits intact, but prohibit .env in current publication candidates.
LEGACY_ENV = {
    b'API_IMAGE_TAG_SUFFIX=latest-nvidia-gpu-sm120-offline',
    b'VLM_BACKEND=vllm',
    b'VLM_IMAGE_TAG_SUFFIX=latest-nvidia-gpu-sm120-offline',
}


def git(*args, root=ROOT):
    return subprocess.check_output(['git', *args], cwd=root)


def private_path(name):
    path = PurePosixPath(name)
    return (bool(set(path.parts) & PRIVATE_DIRS)
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or '.sqlite3-' in path.name
            or (path.name.startswith('.env') and path.name != '.env.example')
            or name.startswith('docs/fixtures/')
            or name == 'tools/build_toc_review.py')


def check_blob(name, data, *, historical=False):
    issues = []
    legacy = (historical and name == 'compose/.env'
              and set(data.splitlines()) == LEGACY_ENV)
    if private_path(name) and not legacy:
        issues.append(f'{name}: private/generated path')
    if len(data) > 5 * 1024 * 1024:
        issues.append(f'{name}: exceeds 5 MiB source-file limit')
    if any(pattern.search(data) for pattern in SECRET_PATTERNS):
        issues.append(f'{name}: possible credential (value suppressed)')
    return issues


def broken_links(name, text, candidates):
    """Check inline relative Markdown file links, excluding fenced examples."""
    text = re.sub(r'```.*?```', '', text, flags=re.S)
    issues = []
    for raw in re.findall(r'\]\(([^\n)]+)\)', text):
        target = raw.strip().split(' "', 1)[0].strip('<>')
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path:
            continue
        # Pure path normalization, without depending on ignored local files.
        parts = list(PurePosixPath(name).parent.parts)
        for part in unquote(url.path).split('/'):
            if part == '..':
                if parts:
                    parts.pop()
                else:
                    parts.append('..')
            elif part not in ('', '.'):
                parts.append(part)
        resolved = '/'.join(parts)
        if resolved not in candidates:
            issues.append(f'{name}: unpublished link target {target}')
    return issues


def check_tree(root):
    names = {n.decode('utf-8') for n in git('ls-files', '-z', '--cached', '--others',
                                          '--exclude-standard', root=root).split(b'\0') if n}
    names = {n for n in names if (root / n).is_file()}
    issues = []
    for name in sorted(names):
        data = (root / name).read_bytes()
        issues.extend(check_blob(name, data))
        if name.endswith('.md'):
            issues.extend(broken_links(name, data.decode('utf-8'), names))
    return len(names), issues


def check_history(root):
    seen = set()
    issues = []
    commits = git('rev-list', '--all', root=root).splitlines()
    for commit in commits:
        entries = git('ls-tree', '-rz', commit.decode('ascii'), root=root).split(b'\0')
        for entry in entries:
            if not entry:
                continue
            header, raw_name = entry.split(b'\t', 1)
            _, kind, oid = header.split()
            name = raw_name.decode('utf-8')
            identity = (name, oid)
            if kind != b'blob' or identity in seen:
                continue
            seen.add(identity)
            data = git('cat-file', 'blob', oid.decode('ascii'), root=root)
            issues.extend(f'{commit.decode("ascii")[:12]}: {issue}'
                          for issue in check_blob(name, data, historical=True))
    return len(commits), len(seen), issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='also inspect all reachable commits')
    args = parser.parse_args(argv)
    count, issues = check_tree(ROOT)
    print(f'Checked {count} publication candidate files and relative Markdown links.')
    if args.history:
        commits, blobs, history_issues = check_history(ROOT)
        issues.extend(history_issues)
        print(f'Checked {commits} reachable commits / {blobs} unique path/blob pairs.')
    for issue in issues:
        print(issue)
    print('FAILED' if issues else 'OK: repository publication guard passed.')
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())

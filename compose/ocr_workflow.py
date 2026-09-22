"""Service readiness and output verification for unattended OCR jobs."""
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager, ExitStack
from pathlib import Path

import requests
from ocr_artifacts import TextArtifact, resolve_text, import_sidecars
from ocr_verify import GLYPH_EQUIVALENTS, fold_equivalents, verify


def artifact_path(folder, name):
    """Only final PDFs and the normal human-readable report belong at top level."""
    if Path(name).name != name:
        raise ValueError('Artifact name must be a filename')
    public = name.endswith('_searchable.pdf') or name.endswith('_auto_report.md')
    return Path(folder) / name if public else Path(folder) / '.ocr' / name


def find_artifact(folder, name):
    canonical = artifact_path(folder, name)
    legacy = Path(folder) / name
    return resolve_text(canonical if canonical.exists() or not legacy.exists() else legacy)


def document_artifact(folder, stem, name):
    """PDFs stay on disk; JSON and Markdown use the document's single database."""
    if name.endswith(('.json', '.md')):
        return TextArtifact(Path(folder)/'.ocr'/f'{stem}_line_ocr.sqlite3', name)
    return artifact_path(folder, name)


def compact_document(folder, stem):
    """Caller holds output_lock. Import old text files, then remove verified copies."""
    folder = Path(folder).resolve()
    internal = folder/'.ocr'
    names = set(document_artifacts(stem)) | {f'{stem}_layout_review.json', f'{stem}_line_ocr_review.json'}
    files = []
    directories = set()
    for base in (folder, internal):
        for path in base.iterdir():
            if path.is_file() and path.suffix in ('.json', '.md') and (
                    path.name in names or (path.name.startswith(stem+'_') and '_layout_' in path.name)):
                files.append((path, path.name))
        shards = base/f'{stem}_boundary_cache.json.pages'
        if shards.is_dir():
            for path in shards.rglob('*.json'):
                files.append((path, path.relative_to(base).as_posix()))
                directories.update(parent for parent in path.parents
                                   if parent == shards or parent.is_relative_to(shards))
    # Source-isolated caches referenced by this document's own reports also
    # belong in its database. Never sweep another book's isolated cache.
    fingerprints = set()
    for path, key in files:
        if key.endswith('_report.json'):
            record = json.loads(path.read_text(encoding='utf-8'))
            if record.get('source_identity'):
                import hashlib
                fingerprints.add(hashlib.sha256(json.dumps(record['source_identity'],
                                     sort_keys=True).encode()).hexdigest()[:20])
    for fingerprint in fingerprints:
        for base in (folder, internal):
            isolated = base/'.ocr_cache'/fingerprint
            if isolated.is_dir():
                for path in isolated.iterdir():
                    if path.is_file() and path.name in {'pruned.json', 'text.md', 'meta.json', 'partial.json'}:
                        files.append((path, f'.ocr_cache/{fingerprint}/{path.name}'))
                directories.add(isolated)
    removed = import_sidecars(internal/f'{stem}_line_ocr.sqlite3', files, folder)
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        if directory.is_symlink() or not directory.resolve().is_relative_to(folder):
            raise ValueError(f'Unsafe migration directory: {directory}')
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()  # Empty, verified leaf directories only.
    return removed


def compact_output(folder):
    folder = Path(folder).resolve()
    stems = set()
    for base in (folder, folder/'.ocr'):
        if not base.is_dir():
            continue
        for path in base.iterdir():
            for suffix in ('_line_ocr.sqlite3', '_pruned.json', '_partial.json', '.lock'):
                if path.name.endswith(suffix):
                    stems.add(path.name[:-len(suffix)])
                    break
    removed = []
    for stem in sorted(stems):
        with output_lock(folder, stem):
            removed.extend(compact_document(folder, stem))
    return removed


def document_artifacts(stem):
    names = [stem + suffix for suffix in
             ('.md', '_pruned.json', '_cache_meta.json', '_partial.json',
              '_line_ocr.json', '_line_ocr.sqlite3', '_structure.json',
              '_boundary_cache.json', '_boundary_cache.json.pages')]
    for mode in ('auto', 'line', 'fast', 'paragraph', 'structure'):
        for debug in ('', '_debug'):
            for suffix in ('_status.json', '_report.json', '_report.md',
                           '_failed_report.json', '_failed_report.md'):
                names.append(f'{stem}_{mode}{debug}{suffix}')
    for mode in ('', '_auto', '_line', '_paragraph'):
        names.append(f'{stem}{mode}_debug.pdf')
        for suffix in ('_debug', '_searchable'):
            names.append(f'{stem}{mode}{suffix}.partial.pdf')
    return names


@contextmanager
def output_lock(folder, stem):
    """Keep old jobs locked while migrating their sidecars, without overwriting."""
    folder = Path(folder).resolve()
    internal = folder / '.ocr'
    internal.mkdir(parents=True, exist_ok=True)
    legacy_lock = folder / f'{stem}.lock'
    with job_lock(internal / f'{stem}.lock'):
        with ExitStack() as locks:
            if legacy_lock.exists():
                locks.enter_context(job_lock(legacy_lock))
            moves = [(folder / name, artifact_path(folder, name))
                     for name in document_artifacts(stem)
                     if (folder / name).exists() and artifact_path(folder, name) != folder / name]
            for old, new in moves:
                if new.exists():
                    raise RuntimeError(f'Both old and new artifacts exist; preserved both: {old}, {new}')
            for old, new in moves:
                old.rename(new)
        legacy_lock.unlink(missing_ok=True)
        yield


def organize_output(folder):
    """Organize an existing output folder; never remove OCR data or overwrite files."""
    folder = Path(folder).resolve()
    stems = {p.name[:-len('_pruned.json')] for p in folder.glob('*_pruned.json')}
    stems.update(p.name[:-len('_auto_searchable.pdf')] for p in folder.glob('*_auto_searchable.pdf'))
    with ExitStack() as locks:
        for stem in sorted(stems):
            locks.enter_context(output_lock(folder, stem))
        for old in list(folder.glob('batch_status*.json')) + [folder/'quality_audit.json',
                                                            folder/'batch_logs', folder/'.ocr_cache']:
            if not old.exists():
                continue
            new = artifact_path(folder, old.name)
            if new.exists():
                raise RuntimeError(f'Both old and new artifacts exist; preserved both: {old}, {new}')
            old.rename(new)


@contextmanager
def job_lock(path):
    """An OS lock releases after a crash; the small lock file may remain."""
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as ex:
            raise RuntimeError('This PDF output is already being processed') from ex
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Service:
    def __init__(self, base, compose_dir, compose_service='paddleocr-vl-api'):
        self.base, self.compose_dir = base, compose_dir
        self.compose_service = compose_service
        self.ready = False

    def healthy(self):
        try:
            r = requests.get(self.base+'/health', timeout=3)
            return r.ok and r.json().get('errorCode', 0) == 0
        except (requests.RequestException, ValueError):
            return False

    def ensure(self):
        if self.ready:
            return
        if self.healthy():
            self.ready = True
            return
        docker = shutil.which('docker')
        if not docker:
            raise RuntimeError('OCR server unavailable; Docker Desktop must be installed first')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        desktop = Path(os.environ.get('ProgramFiles', r'C:\Program Files'))/'Docker/Docker/Docker Desktop.exe'
        if os.name == 'nt' and desktop.exists():
            quoted = str(desktop).replace("'", "''")
            subprocess.run(['powershell', '-NoProfile', '-Command',
                            f"Start-Process -FilePath '{quoted}' -WindowStyle Hidden"],
                           creationflags=flags, timeout=30, check=True, capture_output=True)
        deadline = time.monotonic()+300
        started = False
        while time.monotonic() < deadline:
            if self.healthy():
                self.ready = True
                return
            if not started:
                try:
                    probe = subprocess.run([docker,'info','--format','{{.ServerVersion}}'],
                                           capture_output=True, timeout=20, creationflags=flags)
                except subprocess.TimeoutExpired:
                    # Docker Desktop can accept a CLI connection before its
                    # engine is ready. Keep the existing bounded startup wait
                    # instead of failing the book at the first slow probe.
                    print('[service] Docker engine is still starting...', flush=True)
                    time.sleep(10)
                    continue
                if probe.returncode == 0:
                    running = subprocess.run(
                        [docker, 'compose', 'ps', '--status', 'running', '--services'],
                        cwd=self.compose_dir, capture_output=True, text=True,
                        timeout=20, creationflags=flags)
                    if (running.returncode == 0 and
                            self.compose_service in running.stdout.splitlines()):
                        self.recover()
                        return
                    subprocess.run([docker,'compose','up','-d','--no-build','--pull','never',
                                    self.compose_service],
                                   cwd=self.compose_dir, check=True, timeout=90,
                                   capture_output=True, creationflags=flags)
                    started = True
            print('[service] waiting for OCR server...', flush=True)
            time.sleep(10)
        raise RuntimeError('OCR server did not become ready within 5 minutes; rerun to resume')

    def recover(self):
        """Restart only the pipeline API when it stops answering.

        The VLM container keeps the model loaded on the GPU, so recovery is much
        faster than restarting the entire OCR stack.
        """
        self.ready = False
        if self.healthy():
            self.ready = True
            return
        docker = shutil.which('docker')
        if not docker:
            raise RuntimeError('OCR server unavailable; Docker Desktop must be installed first')
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        print(f'[service] restarting unresponsive {self.compose_service}...', flush=True)
        subprocess.run([docker, 'compose', 'restart', self.compose_service],
                       cwd=self.compose_dir, check=True, timeout=90,
                       capture_output=True, creationflags=flags)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.healthy():
                self.ready = True
                return
            print(f'[service] waiting for {self.compose_service}...', flush=True)
            time.sleep(5)
        raise RuntimeError('OCR pipeline did not recover within 3 minutes; rerun to resume')


def write_summary(path, report):
    lines = ['# OCR 처리 결과', '', f"상태: {report['status']}",
             f"페이지: {len(report.get('pages', []))}",
             f"확인할 페이지: {', '.join(map(str,report.get('review_pages', []))) or '없음'}", '',
             '경고가 없어도 모든 글자의 인식 정확성을 보장하지는 않습니다.', '']
    bookmarks = report.get('bookmarks')
    if bookmarks is not None:
        lines.extend(['## 책갈피', '',
                      f"추가: {bookmarks['inserted']}개, 기존 보존: {bookmarks['preserved_existing']}개, "
                      f"검토 필요: {len(bookmarks['review'])}개", ''])
        if not bookmarks['inserted'] and not bookmarks['preserved_existing']:
            lines.append('확인된 책갈피가 없습니다. 상세 보고서의 목차 탐지 결과를 확인하세요.')
        reasons = {'title_not_found': '본문 제목 또는 쪽수 대응을 확인하지 못함',
                   'source_page_missing': '원본 PDF에서 해당 인쇄 쪽수가 빠져 있음',
                   'ambiguous_title': '같은 제목의 목적지가 여러 개임',
                   'destination_out_of_order': '본문 이동 순서가 목차와 다름',
                   'duplicate_entry': '중복 항목'}
        for item in bookmarks['review']:
            lines.append(f"- {item['title']} (목차 PDF {item['toc_page']}쪽): "
                         + reasons.get(item['reason'], item['reason']))
        lines.append('')
    labels = {'line_alignment_rejected':'줄 재인식 불일치: 기본 배치 유지',
              'unassigned_boxes':'텍스트가 배정되지 않은 줄 상자',
              'paragraph_fallback':'문단 단위 대체 배치',
              'paragraph_compacted':'문단 넘침 또는 대체 폰트로 압축 배치됨: 원문 대조 필요',
              'complex_structure':'표 또는 수식: 정밀 줄 검증 제외',
              'no_ocr_text':'인식된 텍스트 없음: 공백/그림/누락 여부 확인',
              'existing_text_preserved':'기존 텍스트 보존: 중복 삽입 생략'}
    labels['ocr_disagreement_content_preserved'] = '줄 인식 문구가 다름: 기존 문구 보존, 원본 대조 필요'
    labels['boundary_recognition_rejected'] = '잘린 영역 확대: 재인식 문구 검증 실패, 기존 문구 유지 및 원본 대조 필요'
    labels['glyph_equivalent_substituted'] = '같은 글리프의 다른 코드포인트로 기록됨: 내용 동일, 검색어 주의'
    labels['undecodable_character_dropped'] = '인식이 표현하지 못한 자리 표시 문자 제외: 해당 글자는 원본 확인 필요'
    for p in report.get('pages', []):
        if p['warnings']:
            lines.append(f"- {p['page']}페이지: "+', '.join(labels.get(w,w) for w in sorted(set(p['warnings']))))
    if isinstance(path, TextArtifact):
        path.write_text('\n'.join(lines)+'\n')
        return
    temp = path.with_suffix('.md.tmp')
    temp.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    temp.replace(path)

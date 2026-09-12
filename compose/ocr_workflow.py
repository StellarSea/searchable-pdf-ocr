"""Service readiness and output verification for unattended OCR jobs."""
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from collections import Counter
from pathlib import Path

import requests
import pymupdf as fitz


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
                probe = subprocess.run([docker,'info','--format','{{.ServerVersion}}'],
                                       capture_output=True, timeout=20, creationflags=flags)
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


def verify(source, output, report, debug=False):
    count = lambda s: Counter(c for c in s if not c.isspace())
    with fitz.open(source) as original, fitz.open(output) as result:
        if len(original) != len(result):
            raise RuntimeError('Output page count mismatch')
        if ([p['page'] for p in report['pages']] != list(range(1,len(result)+1))):
            raise RuntimeError('Validation report must cover every page in order')
        for pi, info in enumerate(report['pages']):
            if not debug:
                before = original[pi].get_pixmap(matrix=fitz.Matrix(1,1))
                after = result[pi].get_pixmap(matrix=fitz.Matrix(1,1))
                if (before.width,before.height,before.samples) != (after.width,after.height,after.samples):
                    info['warnings'].append('visible_page_changed')
            expected = Counter(info.pop('expected_characters'))
            actual = count(result[pi].get_text())
            if expected != actual:
                info['warnings'].append('extracted_text_mismatch')
                info['missing'] = dict(expected-actual)
                info['extra'] = dict(actual-expected)
            if '\x00' in result[pi].get_text():
                info['warnings'].append('nul_character')
            if (not info.get('existing_text') and
                    any(s['type'] != 3 for s in result[pi].get_texttrace())):
                info['warnings'].append('non_invisible_text')
            print(f'[verify] {pi+1}/{len(result)}', flush=True)
    report['review_pages'] = [p['page'] for p in report['pages'] if p['warnings']]
    fatal = {'visible_page_changed', 'extracted_text_mismatch', 'nul_character', 'non_invisible_text', 'insertion_failed'}
    report['validation_failed'] = any(fatal.intersection(p['warnings']) for p in report['pages'])
    report['status'] = 'validation_failed' if report['validation_failed'] else ('completed_with_warnings' if report['review_pages'] else 'completed')
    return report


def write_summary(path, report):
    lines = ['# OCR 처리 결과', '', f"상태: {report['status']}",
             f"페이지: {len(report.get('pages', []))}",
             f"확인할 페이지: {', '.join(map(str,report.get('review_pages', []))) or '없음'}", '',
             '경고가 없어도 모든 글자의 인식 정확성을 보장하지는 않습니다.', '']
    labels = {'line_alignment_rejected':'줄 재인식 불일치: 기본 배치 유지',
              'unassigned_boxes':'텍스트가 배정되지 않은 줄 상자',
              'paragraph_fallback':'문단 단위 대체 배치',
              'complex_structure':'표 또는 수식: 정밀 줄 검증 제외',
              'no_ocr_text':'인식된 텍스트 없음: 공백/그림/누락 여부 확인',
              'existing_text_preserved':'기존 텍스트 보존: 중복 삽입 생략'}
    labels['ocr_disagreement_content_preserved'] = '줄 인식 문구가 다름: 기존 문구 보존, 원본 대조 필요'
    for p in report.get('pages', []):
        if p['warnings']:
            lines.append(f"- {p['page']}페이지: "+', '.join(labels.get(w,w) for w in sorted(set(p['warnings']))))
    temp = path.with_suffix('.md.tmp')
    temp.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    temp.replace(path)

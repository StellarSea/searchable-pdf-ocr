"""Exact PDF verification with bounded, process-owned read-only page checks.

The caller owns report publication. No bitmaps cross process boundaries; workers
open independent PDFs and return only page findings. Any pool failure discards
its partial findings and repeats every check on the original serial path.
"""
import atexit
from collections import Counter
import copy
import math
import multiprocessing as mp
import os
from pathlib import Path
import signal
import time

import pymupdf as fitz
try:
    import psutil
except ImportError:
    psutil = None

MIB = 1024**2
SYSTEM_RESERVE = 2048*MIB
MIN_PARALLEL_PIXELS = 8_000_000
GLYPH_EQUIVALENTS = {'〜': '～'}
FATAL = {'visible_page_changed', 'extracted_text_mismatch', 'nul_character',
         'non_invisible_text', 'insertion_failed'}
_original = _result = None
_init_error = None


def fold_equivalents(counter):
    folded = Counter()
    for char, number in counter.items():
        folded[GLYPH_EQUIVALENTS.get(char, char)] += number
    return folded


def check_page(original, result, index, page_info, debug=False):
    """Keep the former serial pixel / character / invisibility checks exact."""
    info = copy.deepcopy(page_info)
    if not debug:
        before = original[index].get_pixmap(matrix=fitz.Matrix(1, 1))
        after = result[index].get_pixmap(matrix=fitz.Matrix(1, 1))
        if (before.width, before.height, before.samples) != (after.width, after.height, after.samples):
            info['warnings'].append('visible_page_changed')
        del before, after
    expected = Counter(info.pop('expected_characters'))
    extracted = result[index].get_text()
    actual = Counter(c for c in extracted if not c.isspace())
    if expected != actual:
        folded_expected, folded_actual = fold_equivalents(expected), fold_equivalents(actual)
        if folded_expected == folded_actual:
            info['warnings'].append('glyph_equivalent_substituted')
            info['substituted'] = dict(expected - actual)
        else:
            info['warnings'].append('extracted_text_mismatch')
            info['missing'] = dict(folded_expected - folded_actual)
            info['extra'] = dict(folded_actual - folded_expected)
    if '\x00' in extracted:
        info['warnings'].append('nul_character')
    if not info.get('existing_text') and any(s['type'] != 3 for s in result[index].get_texttrace()):
        info['warnings'].append('non_invisible_text')
    return info


def available_memory():
    return psutil.virtual_memory().available if psutil is not None else 0


def _child_rss():
    total = 0
    if psutil is not None:
        for child in psutil.Process().children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
    return total


def _stamp(path):
    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def choose_workers(original, result, requested, memory_mb, debug):
    """Budget decoded images, pixel comparisons and interpreter/cache headroom."""
    if requested < 2 or len(result) < 2 or debug:
        return 0, 0, 'serial requested, single page, or debug text-only check'
    aa = fitz.TOOLS.show_aa_level()
    if aa['graphics'] != aa['text'] or aa['graphics_min_line_width'] != 0:
        return 0, 0, 'custom renderer settings'
    capacity = min(memory_mb*MIB, max(0, (available_memory()-SYSTEM_RESERVE)//2))
    if capacity < 768*MIB:
        return 0, capacity, 'insufficient memory for two workers'
    areas, reservations = [], []
    for index in range(len(result)):
        area, decoded = 0, 0
        for doc in (original, result):
            page = doc[index]
            area = max(area, (math.ceil(page.rect.width)+2)*(math.ceil(page.rect.height)+2))
            # Include image resources and transparency headroom, without decoding.
            images = {row[0]: row for row in page.get_images()}
            decoded += sum(row[2]*row[3]*8 for row in images.values())
        areas.append(area)
        reservations.append(384*MIB + 12*area + decoded)
    if sum(areas) < MIN_PARALLEL_PIXELS:
        return 0, capacity, 'small document; process startup would dominate'
    count = min(requested, 16, os.cpu_count() or 1, len(result), capacity//max(reservations))
    return (int(count) if count >= 2 else 0), capacity, 'memory-aware admission'


def close_worker():
    global _original, _result
    for doc in (_original, _result):
        if doc is not None:
            doc.close()
    _original = _result = None


def initialize_worker(source, output, stamps, aa, equivalents):
    global _original, _result, _init_error
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if (_stamp(source), _stamp(output)) != stamps:
            raise RuntimeError('PDF changed before worker initialization')
        fitz.TOOLS.set_aa_level(aa['graphics'])
        if fitz.TOOLS.show_aa_level() != aa:
            raise RuntimeError('Worker renderer settings differ')
        GLYPH_EQUIVALENTS.clear()
        GLYPH_EQUIVALENTS.update(equivalents)
        _original, _result = fitz.open(source), fitz.open(output)
        atexit.register(close_worker)
        _init_error = None
    except Exception as error:
        # Pool must not respawn failing initializers without returning an error.
        close_worker()
        _init_error = str(error)


def worker_check(index, info, debug):
    if _init_error is not None:
        raise RuntimeError(_init_error)
    return index, check_page(_original, _result, index, info, debug)


def _parallel(source, output, pages, count, capacity, stamps, timeout, stats):
    baseline_rss = _child_rss()
    pool = None
    try:
        pool = mp.get_context('spawn').Pool(count, initializer=initialize_worker,
            initargs=(str(source), str(output), stamps, fitz.TOOLS.show_aa_level(), dict(GLYPH_EQUIVALENTS)),
            maxtasksperchild=32)
        pending, results, cursor, completed = {}, [None]*len(pages), 0, 0
        while completed < len(pages):
            child_rss = max(0, _child_rss()-baseline_rss)
            stats['peak_worker_rss_mib'] = max(stats['peak_worker_rss_mib'], child_rss/MIB)
            if available_memory() < SYSTEM_RESERVE or child_rss > capacity:
                raise MemoryError('Verification pool memory pressure')
            while cursor < len(pages) and len(pending) < count:
                pending[cursor] = (pool.apply_async(worker_check, (cursor, pages[cursor], False)), time.monotonic())
                cursor += 1
                stats['peak_pending'] = max(stats['peak_pending'], len(pending))
            progressed = False
            for index, (future, started) in list(pending.items()):
                if future.ready():
                    returned_index, checked = future.get()
                    if returned_index != index or checked.get('page') != index+1 or 'expected_characters' in checked:
                        raise RuntimeError('Invalid verification worker result')
                    results[index] = checked
                    del pending[index]
                    completed += 1
                    stats['pool_pages'] += 1
                    progressed = True
                    print(f'[verify] {completed}/{len(pages)} checked', flush=True)
                elif time.monotonic()-started > timeout:
                    raise TimeoutError(f'Verification worker timed out on page {index+1}')
            if not progressed:
                time.sleep(0.05)
        pool.close()
        pool.join()
        pool = None
        return results
    finally:
        if pool is not None:
            pool.terminate()
            pool.join()


def verify(source, output, report, debug=False, *, workers=0, memory_mb=16384,
           stats=None, timeout=120):
    if not isinstance(workers, int) or workers < 0 or memory_mb < 256 or timeout <= 0:
        raise ValueError('Invalid verification worker/memory/timeout limit')
    stats = {} if stats is None else stats
    stats.update(requested_workers=workers, workers=0, pool_pages=0, serial_pages=0,
                 peak_pending=0, peak_worker_rss_mib=0, fallback=None)
    stamps = _stamp(source), _stamp(output)
    checked = None
    with fitz.open(source) as original, fitz.open(output) as result:
        if len(original) != len(result):
            raise RuntimeError('Output page count mismatch')
        if [p['page'] for p in report['pages']] != list(range(1, len(result)+1)):
            raise RuntimeError('Validation report must cover every page in order')
        for info in report['pages']:
            if 'expected_characters' not in info or not isinstance(info.get('warnings'), list):
                raise ValueError('Incomplete page verification inputs')
        try:
            count, capacity, reason = choose_workers(original, result, workers, memory_mb, debug)
            stats.update(workers=count, admission=reason)
            if count:
                print(f'[verify-cpu] {count}/{workers} workers, budget {capacity/MIB:.0f} MiB', flush=True)
                checked = _parallel(source, output, report['pages'], count, capacity, stamps, timeout, stats)
        except Exception as error:
            stats['fallback'] = str(error)
            print(f'[verify-cpu] {error}; checking all pages serially', flush=True)
        if checked is None:
            checked = []
            for index, info in enumerate(report['pages']):
                checked.append(check_page(original, result, index, info, debug))
                stats['serial_pages'] += 1
                print(f'[verify] {index+1}/{len(result)}', flush=True)
    if (_stamp(source), _stamp(output)) != stamps:
        raise RuntimeError('PDF changed during verification')
    # Publish findings only after every page finishes, preserving public dict identities.
    for info, findings in zip(report['pages'], checked):
        info.clear()
        info.update(findings)
    report['review_pages'] = [p['page'] for p in report['pages'] if p['warnings']]
    report['validation_failed'] = any(FATAL.intersection(p['warnings']) for p in report['pages'])
    report['status'] = ('validation_failed' if report['validation_failed'] else
                        'completed_with_warnings' if report['review_pages'] else 'completed')
    return report

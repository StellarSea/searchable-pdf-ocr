"""Reopen a copied diagnostic cache and verify production's automatic warm path."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_storage import atomic_json
from ocr_workflow import verify
from benchmark_prefetch import comparable
from benchmark_cpu_overlay import assert_pdf_equal


def run(args):
    if args.out.exists():
        raise ValueError('Use a new diagnostic output directory')
    manifest = json.loads((args.previous/'comparison.json').read_text(encoding='utf-8'))
    layouts = json.loads(args.layout.read_text(encoding='utf-8'))
    chosen = [layouts['pages'][str(number-1)]['result'] for number in manifest['pages']]
    source = args.previous/'source.pdf'
    with source.open('rb') as stream:
        sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    args.out.mkdir(parents=True)
    original = sqlite3.connect((args.previous/'round-0-workers-4/lines.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)
    copied = sqlite3.connect(args.out/'lines.sqlite3')
    try:
        original.backup(copied)
    finally:
        original.close()
        copied.close()
    refiner = ocr.LineRefiner(args.out/'lines.json', set(range(1, len(chosen)+1)), automatic=True, source_sha256=sha)
    try:
        with patch.object(ocr, 'recognize_lines', side_effect=AssertionError('Warm HTTP call')):
            started = time.perf_counter()
            report = ocr.overlay(source, chosen, args.out/'output.pdf', automatic=True,
                                 line_refiner=refiner, cpu_workers=4)
            elapsed = time.perf_counter()-started
        stats = report['cpu_prepare_stats']
        assert stats['pool_starts'] == 0 and not stats['fallbacks'], stats
        verified = verify(source, args.out/'output.pdf', copy.deepcopy(report))
        reference = json.loads((args.previous/'round-0-workers-0/quality.json').read_text(encoding='utf-8'))
        assert comparable(verified) == comparable(reference), 'Quality report changed'
        assert_pdf_equal(args.previous/'round-0-workers-0/output.pdf', args.out/'output.pdf')
        summary = {'pages': manifest['pages'], 'overlay_seconds': elapsed,
                   'stats': stats, 'exact': True, 'http_calls': 0}
        atomic_json(args.out/'comparison.json', summary)
        print(json.dumps(summary), flush=True)
    finally:
        refiner.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('previous', type=Path)
    parser.add_argument('layout', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    run(parser.parse_args())

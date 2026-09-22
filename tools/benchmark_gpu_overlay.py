"""Paired CPU16 overlay with/without GPU feed; isolated caches and no restarts."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_artifacts import resolve_text
from ocr_storage import atomic_json
from ocr_workflow import verify
from benchmark_cpu_overlay import assert_pdf_equal
from benchmark_cpu_workers import MemoryMonitor


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('previous', type=Path)
    ap.add_argument('layout')
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    if args.out.exists():
        raise ValueError('Use a new diagnostic directory')
    manifest = json.loads((args.previous/'comparison.json').read_text(encoding='utf-8'))
    layouts = json.loads(resolve_text(args.layout).read_text(encoding='utf-8'))
    chosen = [layouts['pages'][str(n-1)]['result'] for n in manifest['pages']]
    source = args.previous/'source.pdf'
    with source.open('rb') as handle:
        sha = hashlib.file_digest(handle, 'sha256').hexdigest()
    args.out.mkdir(parents=True)
    recognize = ocr.recognize_lines
    reference = reference_decisions = reference_pdf = None
    runs = []
    for enabled in (False, True):
        root = args.out/('feed' if enabled else 'baseline')
        root.mkdir()
        refiner = ocr.LineRefiner(root/'lines.json', set(range(1,len(chosen)+1)), automatic=True, source_sha256=sha)
        calls = []
        def api(images, **kwargs):
            start = time.perf_counter()
            result = recognize(images, **kwargs)
            calls.append({'images':len(images), 'seconds':time.perf_counter()-start})
            return result
        try:
            with MemoryMonitor() as memory, patch.object(ocr, 'recognize_lines', api):
                start = time.perf_counter()
                report = ocr.overlay(source, chosen, root/'output.pdf', automatic=True,
                    line_refiner=refiner, cpu_workers=16, gpu_prefetch=enabled)
                seconds = time.perf_counter()-start
            decisions = copy.deepcopy(refiner.cache['decisions'])
            start = time.perf_counter()
            verified = verify(source, root/'output.pdf', copy.deepcopy(report))
            verify_seconds = time.perf_counter()-start
            comparable = {key:value for key,value in verified.items() if not key.endswith('_stats')}
            atomic_json(root/'quality.json', verified)
            if reference is None:
                reference, reference_decisions, reference_pdf = comparable, decisions, root/'output.pdf'
            exact = comparable == reference and decisions == reference_decisions
            assert_pdf_equal(reference_pdf, root/'output.pdf')
            runs.append({'enabled': enabled, 'overlay_seconds':seconds, 'verify_seconds':verify_seconds,
                         'total_seconds':seconds+verify_seconds, 'http_calls':len(calls),
                         'http_images':sum(call['images'] for call in calls),
                         'peak_rss_mib':memory.peak_rss/1024**2,
                         'gpu_feed':report.get('gpu_feed_stats'), 'cpu':report.get('cpu_prepare_stats'),
                         'exact':exact})
            atomic_json(args.out/'comparison.json', {'pages':manifest['pages'], 'runs':runs})
            assert exact and not verified['validation_failed'], 'Candidates/decisions/quality changed'
            print(json.dumps(runs[-1]), flush=True)
        finally:
            refiner.close()
    # Warm resume must keep CPU/GPU idle; compare all text, geometry and pixels.
    refiner = ocr.LineRefiner(root/'lines.json', set(range(1,len(chosen)+1)), automatic=True, source_sha256=sha)
    try:
        with patch.object(ocr, 'recognize_lines', side_effect=AssertionError('warm HTTP')):
            report = ocr.overlay(source, chosen, args.out/'warm.pdf', automatic=True,
                line_refiner=refiner, cpu_workers=16, gpu_prefetch=True)
        assert report['gpu_feed_stats']['requests'] == 0
        assert report['cpu_prepare_stats']['pool_starts'] == 0
        assert refiner.cache['decisions'] == reference_decisions
        assert_pdf_equal(reference_pdf, args.out/'warm.pdf')
        atomic_json(args.out/'comparison.json', {'pages':manifest['pages'], 'runs':runs, 'warm_exact':True})
    finally:
        refiner.close()


if __name__ == '__main__':
    main()

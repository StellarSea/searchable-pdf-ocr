"""Replay real boundary preparation with recorded OCR; source/baseline read-only.

The replay verifies complete derived layouts/checkpoints and records exact PNG
hashes. --live explicitly enables real OCR and is not an accuracy comparison.
"""
import argparse
import copy
import cProfile
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
import ocr_boundary as boundary
from ocr_artifacts import TextArtifact
from ocr_storage import atomic_json


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_replay(source, baseline):
    db = baseline/'.ocr'/f'{source.stem}_line_ocr.sqlite3'
    with closing(sqlite3.connect(db.resolve().as_uri()+'?mode=ro', uri=True)) as conn:
        def artifact(suffix):
            row = conn.execute('SELECT content FROM artifacts WHERE name=?', (source.stem+suffix,)).fetchone()
            if row is None:
                raise ValueError(f'Missing baseline {suffix}')
            return json.loads(row[0])
        report, pages, cached = artifact('_auto_report.json'), artifact('_pruned.json'), artifact('_boundary_cache.json')
    identity = report['source_identity']
    if hashlib.sha256(source.read_bytes()).hexdigest() != identity['sha256']:
        raise ValueError('Baseline belongs to a different PDF')
    if cached['identity'] != {'source': identity['sha256'], 'version': boundary.VERSION, 'api': boundary.ocr_api.API}:
        raise ValueError('Boundary cache identity mismatch')
    expected, candidates = [], []
    for index, page in enumerate(pages):
        saved = cached['pages'][str(index)]
        if saved['fingerprint'] != digest(page):
            raise ValueError('Baseline boundary input fingerprint mismatch')
        if any('boundary_audit' in entry for entry in page.get('parsing_res_list', [])):
            raise ValueError('Boundary replay needs pre-boundary inputs')
        expected.append(saved['result'])
        for block in saved['result'].get('parsing_res_list', []):
            audit = block.get('boundary_audit')
            if audit is not None:
                candidates.append(audit['candidate'])
    return pages, identity, expected, candidates, cached


def run(source, pages, identity, expected, candidates, cached, out, *, live=False, **kwargs):
    images, responses, request_seconds = [], [], []
    original = copy.deepcopy(pages)
    cache = TextArtifact(out/'boundary.sqlite3', 'boundary.json')

    def recognize(png, file_type, **options):
        if file_type != 1 or options != {'options': {'useLayoutDetection': False}}:
            raise AssertionError('Boundary request settings changed')
        index = len(images)
        if index >= len(candidates):
            raise AssertionError('Unexpected additional boundary request')
        images.append(hashlib.sha256(png).hexdigest())
        if live:
            request_started = time.perf_counter()
            response = boundary.ocr_api._call_api(png, file_type, **options)
            request_seconds.append(time.perf_counter() - request_started)
            responses.append(response)
            return response
        value = candidates[index]
        return {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
            {'block_content': value, 'block_bbox': [0, 0, 1, 1]}]}}]}

    feed_stats = {}
    started = time.perf_counter()
    result = boundary.repair(source, pages, cache, identity, call_api=recognize, stats=feed_stats, **kwargs)
    elapsed = time.perf_counter()-started
    if pages != original or len(images) != len(candidates):
        raise AssertionError('Input mutation or unexpected boundary request count')
    checkpoint = json.loads(cache.read_text())
    exact = result == expected and checkpoint == cached
    record = {'seconds': elapsed, 'pages': len(result), 'requests': len(images), 'images': images,
              'result_sha256': digest(result), 'checkpoint_sha256': digest(checkpoint),
              'exact_baseline_layout_and_checkpoints': exact, 'live': live, 'responses': responses,
              'prefetch': kwargs.get('prefetch', False), 'feed_stats': feed_stats,
              'request_seconds': request_seconds,
              'outside_http_seconds': elapsed-sum(request_seconds) if live else None}
    atomic_json(out/'result.json', record)
    if not live and not exact:
        raise AssertionError('Boundary layout/audits/page checkpoints differ from recorded baseline')
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--overlap', action='store_true', help='Alternate serial and single-request HTTP overlap')
    args = parser.parse_args()
    if args.repeats < 1 or args.profile and args.live:
        parser.error('Positive repeats required; function profiling is offline only')
    data = load_replay(args.source, args.baseline)
    args.out.mkdir(parents=True, exist_ok=False)
    records = []
    if args.profile:
        directory = args.out/'profile'
        directory.mkdir()
        profiler = cProfile.Profile()
        profiler.runcall(run, args.source, *data, directory)
        profiler.dump_stats(args.out/'boundary.pstats')
    for index in range(args.repeats):
        variants = (False, True) if args.overlap else (False,)
        for overlap in variants if index % 2 == 0 else reversed(variants):
            directory = args.out/f'run-{len(records)+1}'
            directory.mkdir()
            record = run(args.source, *data, directory, live=args.live, prefetch=overlap)
            if records and record['images'] != records[0]['images']:
                raise AssertionError('Boundary input PNGs changed')
            records.append(record)
            print(f'[boundary-benchmark] prefetch={overlap}: {record["seconds"]:.3f}s, {record["requests"]} requests', flush=True)
    result = {'median_seconds': statistics.median(r['seconds'] for r in records), 'records': records,
              'median_by_prefetch': {str(mode): statistics.median(r['seconds'] for r in records if r['prefetch'] == mode)
                                     for mode in sorted({r['prefetch'] for r in records})},
              'scope': 'Boundary stage only; live model variation is not ground-truth accuracy'}
    atomic_json(args.out/'comparison.json', result)
    print({'median_by_prefetch': result['median_by_prefetch']}, flush=True)
    if args.live and not all(r['exact_baseline_layout_and_checkpoints'] for r in records):
        raise SystemExit('Live boundary results differ from recorded baseline')


if __name__ == '__main__':
    main()

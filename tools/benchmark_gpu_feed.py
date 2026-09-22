"""Compare block requests with lossless cross-block batches on the live service.

Reads existing crop fixtures only. No operational cache writes, model changes,
Docker restarts, or OCR job launches. Outputs are confined to the given report.
"""
import argparse
import base64
import json
from pathlib import Path
import statistics
import sys
import time

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_api import LINE_BASE, LINE_API
from ocr_storage import atomic_json


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('corpus', type=Path, nargs='+')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--rounds', type=int, default=3)
    args = ap.parse_args()
    cases = []
    for path in args.corpus:
        for case in json.loads(path.read_text(encoding='utf-8'))['cases']:
            if case['kind'] == 'block':
                cases.append((case['lang'], [(str(path.parent/name), base64.b64encode(
                    (path.parent/name).read_bytes()).decode()) for name in case['files']]))
    health = requests.get(LINE_BASE+'/health', timeout=5).json()
    def run(size):
        if size == 0:
            groups = cases
        else:
            groups = []
            for lang in ('default', 'korean'):
                items = [item for language, images in cases if language == lang for item in images]
                groups.extend((lang, items[i:i+size]) for i in range(0, len(items), size))
        outputs, scores, timing = {}, {}, []
        start = time.perf_counter()
        for lang, images in groups:
            response = requests.post(LINE_API, json={'lang': lang, 'images': [data for _, data in images]}, timeout=(5, 120))
            response.raise_for_status()
            result = response.json()
            assert len(result['texts']) == len(images)
            for (key, _), text, score in zip(images, result['texts'], result['scores']):
                outputs[key], scores[key] = text, score
            timing.append(result.get('timing', {}))
        return {'seconds': time.perf_counter()-start, 'requests': len(groups),
                'texts': outputs, 'scores': scores, 'server_timing': timing}
    # Model warmup, excluded from measurements.
    run(32)
    results, reference = [], None
    for round_no in range(args.rounds):
        order = [0, 32, 64]
        order = order[round_no%3:]+order[:round_no%3]
        for size in order:
            result = run(size)
            if reference is None:
                reference = result
            result['equal_texts'] = result['texts'] == reference['texts']
            result['max_score_delta'] = max(abs(result['scores'][key]-reference['scores'][key]) for key in result['scores'])
            result.update(round=round_no, request_images=size)
            results.append(result)
            print(json.dumps({k: result[k] for k in ('round','request_images','seconds','requests','equal_texts','max_score_delta')}), flush=True)
    summary = {str(size): statistics.median(r['seconds'] for r in results if r['request_images'] == size) for size in (0,32,64)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.out, {'health': health, 'lines': sum(len(items) for _, items in cases),
                           'median_seconds': summary, 'runs': results})
    if not all(result['equal_texts'] for result in results):
        raise SystemExit('Batching changed recognition text; do not enable by default')


if __name__ == '__main__':
    main()

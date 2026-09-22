"""Confirm deployed worker settings and exact ordered text agreement via HTTP."""
import argparse
import base64
import json
from pathlib import Path
import time
import requests


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('corpus', type=Path)
    ap.add_argument('baseline', type=Path)
    ap.add_argument('--workers', type=int, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    base = 'http://127.0.0.1:8081'
    response = requests.get(base+'/health', timeout=10)
    response.raise_for_status()
    health = response.json()
    assert health['status'] == 'ok' and health['workers'] == args.workers, health
    assert health['inputWidth'] == 320 and health['batchSize'] == 32, health
    corpus = json.loads(args.corpus.read_text(encoding='utf-8'))
    baseline = {c['id']: c for c in json.loads(args.baseline.read_text(encoding='utf-8'))['cases']}
    results = []
    for case in corpus['cases']:
        if case['kind'] != 'block':
            continue
        encoded = [base64.b64encode((args.corpus.parent/f).read_bytes()).decode() for f in case['files']]
        started = time.perf_counter()
        response = requests.post(base+'/recognize', json={'images': encoded, 'lang': case['lang']}, timeout=120)
        response.raise_for_status()
        result = response.json()
        expected = baseline[case['id']]['texts']
        assert result['texts'] == expected, f"Changed text or order: {case['id']}"
        results.append({'id': case['id'], 'lines': len(expected), 'exact_match': True,
                        'http_seconds': time.perf_counter()-started, 'timing': result['timing']})
    report = {'health': health, 'cases': results, 'lines_verified': sum(c['lines'] for c in results)}
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'workers': health['workers'], 'lines_verified': report['lines_verified'], 'exact_match': True}))


if __name__ == '__main__':
    main()

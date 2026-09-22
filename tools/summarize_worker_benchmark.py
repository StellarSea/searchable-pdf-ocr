"""Summarize same-corpus worker runs; agreement is not ground-truth accuracy."""
import argparse
import json
from pathlib import Path
import statistics


def summarize(paths, baseline):
    reference = json.loads(baseline.read_text(encoding='utf-8'))
    expected = {c['id']: c for c in reference['cases']}
    rows = []
    for path in paths:
        report = json.loads(path.read_text(encoding='utf-8'))
        if report.get('status') != 'completed' or report.get('skipped'):
            raise ValueError(f'{path}: incomplete benchmark')
        if (report.get('corpus_sha256') and reference.get('corpus_sha256')
                and report['corpus_sha256'] != reference['corpus_sha256']):
            raise ValueError(f'{path}: mismatched corpus hash')
        cases = report['cases']
        if {c['id'] for c in cases} != set(expected):
            raise ValueError(f'{path}: mismatched corpus')
        for case in cases:
            if (case['lines'], case['lang'], case['kind']) != tuple(
                    expected[case['id']][key] for key in ('lines', 'lang', 'kind')):
                raise ValueError(f'{path}: mismatched case shape')
        changes = sum(sum(a != b for a, b in zip(c['texts'], expected[c['id']]['texts']))
                      + abs(len(c['texts'])-len(expected[c['id']]['texts'])) for c in cases)
        blocks = [c for c in cases if c['kind'] == 'block']
        rows.append({'run': path.name, 'workers': report['workers'],
                     'block_lines': sum(c['lines'] for c in blocks),
                     'block_seconds': sum(c['median_seconds'] for c in blocks),
                     'block_predict_seconds': sum(statistics.median(r['predictSeconds'] for r in c['runs']) for c in blocks),
                     'batch_seconds': sum(c['median_seconds'] for c in cases if c['kind'] == 'batch'),
                     'text_changes_from_baseline': changes,
                     'repeat_text_changes': sum(c['repeat_text_changes'] for c in cases),
                     'peak_total_gpu_mib': report['telemetry']['peak_used_mib'],
                     'minimum_free_gpu_mib': report['telemetry']['min_free_mib']})
    return {'baseline': baseline.name, 'results': rows,
            'scope': 'Warm server recognition, including line splitting; excludes HTTP, PNG decode, PDF rendering and overlay. GPU memory is total device use sampled every 0.2s, not isolated model allocation.',
            'accuracy': 'Exact ordered string agreement with baseline, not manually labeled OCR accuracy.'}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('runs', type=Path, nargs='+')
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    result = summarize(args.runs, args.baseline)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))

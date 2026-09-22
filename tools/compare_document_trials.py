"""Compare saved byte-identical OCR trials; reports variation, never accuracy.

Read-only inputs. Each trial is a benchmark_document_repeat directory; optional
resource samples are read from its sibling NAME-profile directory. No OCR calls.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from benchmark_document_repeat import snapshot, differences, content_changes
from summarize_ocr_profile import windows_attribution


def read_trial(folder):
    summary = json.loads((folder/'comparison.json').read_text(encoding='utf-8'))
    if summary.get('status') != 'completed' or len(summary.get('records', [])) < 2:
        raise ValueError('Require completed repeated trials')
    request = folder/('request.image' if (folder/'request.image').exists() else 'request.pdf')
    digest = hashlib.sha256(request.read_bytes()).hexdigest()
    if digest != summary.get('input_sha256'):
        raise ValueError('Recorded request digest mismatch')
    responses = [snapshot(json.loads((folder/f'response-{r["run"]}.json').read_text(encoding='utf-8')),
                          summary['expected_pages']) for r in summary['records']]
    times = [r['seconds'] for r in summary['records']]
    if any(not isinstance(value, (int, float)) or not 0 < value < float('inf') for value in times):
        raise ValueError('Invalid trial duration')
    return summary, responses, times


def resources(folder):
    path = folder.with_name(folder.name+'-profile')/'samples.jsonl'
    if not path.exists():
        return {'unavailable': 'No resource profile'}
    samples = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    fresh = [s for s in samples if s.get('gpu', {}).get('age_seconds', float('inf')) <= 2
             and isinstance(s.get('gpu', {}).get('memory_mib'), (int, float))]
    vlm_peaks = {}
    for key in ('vllm:num_requests_running', 'vllm:num_requests_waiting', 'vllm:kv_cache_usage_perc'):
        values = [s['vlm'][key] for s in samples
                  if s.get('vlm', {}).get('age_seconds', float('inf')) <= 2
                  and isinstance(s.get('vlm', {}).get(key), (int, float))]
        vlm_peaks[key] = max(values, default=None)
    # Windows records contain the latest 5s reading; these are resampled values,
    # not independent measurements. Do not add different adapters/engines.
    adapters = {}
    for sample in samples:
        probe = sample.get('windows_gpu', {})
        if probe.get('age_seconds', float('inf')) > 10:
            continue
        for adapter in probe.get('adapters', []):
            row = adapters.setdefault(adapter['Name'], {'dedicated_max_bytes': 0, 'shared_max_bytes': 0})
            for source, target in (('DedicatedUsage', 'dedicated_max_bytes'), ('SharedUsage', 'shared_max_bytes')):
                row[target] = max(row[target], adapter[source])
    return {'fresh_gpu_samples': len(fresh),
            'gpu_memory_max_mib': max((s['gpu']['memory_mib'] for s in fresh), default=None),
            'gpu_memory_median_mib': statistics.median(s['gpu']['memory_mib'] for s in fresh) if fresh else None,
            'host_available_memory_min_bytes': min((s['available_memory_bytes'] for s in samples), default=None),
            'fresh_vlm_peaks': vlm_peaks,
            'windows_adapters': adapters, 'windows_engines': windows_attribution(samples),
            'scope': 'Whole command profile includes startup/end; asynchronous counters, not causal proof'}


def check_expected(summary, responses, checks):
    """Small visually transcribed checks are source-bound, not a whole-book score."""
    if checks.get('input_sha256') != summary['input_sha256']:
        raise ValueError('Ground-truth checks belong to different request bytes')
    if not isinstance(checks.get('checks'), list) or not checks['checks']:
        raise ValueError('At least one source-bound check is required')
    results = []
    for run, response in enumerate(responses, 1):
        for index, check in enumerate(checks['checks']):
            pno, bno = check['page']-1, check['block_index']
            if pno < 0 or bno < 0 or pno >= len(response) or bno >= len(response[pno]):
                block = None
            else:
                block = response[pno][bno]
            identity = block is not None and block['block_id'] == check['block_id'] and block['block_bbox'] == check['block_bbox']
            actual = block['block_content'] if block is not None else None
            passed = identity and ''.join(actual.split()) == ''.join(check['expected_text'].split())
            results.append({'run': run, 'check': index, 'passed': passed, 'identity_matches': identity,
                            'actual': actual, 'expected': check['expected_text']})
    return results


def compare_trials(folders, checks=None):
    trials, reference, identity = [], None, None
    for folder in folders:
        summary, responses, times = read_trial(folder)
        current = (summary['input_sha256'], summary['input_bytes'], summary['expected_pages'], summary['options'])
        if identity is not None and current != identity:
            raise ValueError('Trials used different input or OCR options')
        if reference is None:
            reference, identity = responses[0], current
        changes = [differences(reference, response) for response in responses]
        trials.append({'trial': str(folder), 'seconds': times, 'median_seconds': statistics.median(times),
            'excluding_first_median_seconds': statistics.median(times[1:]),
            'exact_within_trial': all(response == responses[0] for response in responses),
            'changes_from_first_reference': changes,
            'content_changes_from_first_reference': [content_changes(reference, response, changed)
                                                     for response, changed in zip(responses, changes)],
            'resources': resources(folder),
            'source_bound_checks': check_expected(summary, responses, checks) if checks is not None else None})
    return {'input_sha256': identity[0] if identity else None, 'trials': trials,
            'scope': 'Byte-identical requests and options; response variation is not ground-truth accuracy; '
                     'excluding first run describes timing, not a guarantee of equal warm state'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trials', nargs='+', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--checks', type=Path, help='Optional visually transcribed, request-hash-bound checks; failures exit 1')
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError('Choose a new comparison path')
    checks = json.loads(args.checks.read_text(encoding='utf-8')) if args.checks else None
    result = compare_trials(args.trials, checks=checks)
    with args.out.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    for trial in result['trials']:
        print({k: trial[k] for k in ('trial', 'seconds', 'median_seconds', 'excluding_first_median_seconds', 'exact_within_trial')})
        if checks is not None:
            print({'source_bound_checks_failed': sum(not row['passed'] for row in trial['source_bound_checks'])})
    return int(any(not row['passed'] for trial in result['trials'] for row in trial['source_bound_checks'] or []))


if __name__ == '__main__':
    raise SystemExit(main())

"""Read-only summary of log-delimited OCR regions and fresh GPU/VLM samples.

Log boundaries include adjacent bookkeeping; these are not exact function timers.
GPU and VLM probes are asynchronous. Sample counts are never reported as seconds.
"""
import argparse
import json
from pathlib import Path
import statistics


def windows_attribution(samples, max_age=10):
    """Keep engines separate: adding utilization across engines/devices is invalid.

    Counts refer to 1s monitor records holding the latest 5s Windows reading,
    not independent engine measurements or exclusive seconds of GPU ownership.
    """
    groups = {}
    records = 0
    for sample in samples:
        probe = sample.get('windows_gpu', {})
        if probe.get('age_seconds', float('inf')) > max_age or not isinstance(probe.get('engines'), list):
            continue
        records += 1
        for engine in probe['engines']:
            if not isinstance(engine, dict) or not isinstance(engine.get('utilization'), (int, float)):
                continue
            key = (engine.get('name'), engine.get('process'))
            row = groups.setdefault(key, {'engine': key[0], 'process': key[1], 'pid': engine.get('pid'),
                                          'nonzero_records': 0, 'peak_utilization': 0})
            row['nonzero_records'] += 1
            row['peak_utilization'] = max(row['peak_utilization'], engine['utilization'])
    return {'fresh_monitor_records': records, 'max_probe_age_seconds': max_age,
            'scope': 'Latest Windows readings resampled by monitor; engines are not additive',
            'engines': sorted(groups.values(), key=lambda row: row['peak_utilization'], reverse=True)}


def summarize(samples, max_age=2):
    events = sorted((event for sample in samples for event in sample.get('events', [])),
                    key=lambda event: event['elapsed'])
    definitions = [('body', '[health]'), ('repair', '[ocr-feed]'),
                   ('boundary', '[md]'), ('overlay', '[font]'),
                   ('verify_and_publish', '[verify-cpu]'), ('end', '[done]')]
    markers = {}
    for name, prefix in definitions:
        found = next((event for event in events if event['line'].startswith(prefix)), None)
        if found is not None:
            markers[name] = found['elapsed']
    regions = []
    for (name, _), (following, _) in zip(definitions, definitions[1:]):
        if name not in markers or following not in markers:
            regions.append({'region': name, 'unavailable': 'Missing boundary log marker'})
            continue
        begin, end = markers[name], markers[following]
        if end < begin:
            raise ValueError('Out-of-order stage markers')
        selected = [s for s in samples if begin <= s['elapsed'] < end]
        fresh = [s for s in selected if s.get('gpu', {}).get('age_seconds', float('inf')) <= max_age
                 and isinstance(s.get('gpu', {}).get('gpu_percent'), (int, float))]
        paired = [s for s in fresh if s.get('vlm', {}).get('age_seconds', float('inf')) <= max_age
                  and all(isinstance(s.get('vlm', {}).get(key), (int, float))
                          for key in ('vllm:num_requests_running', 'vllm:num_requests_waiting'))]
        low = [s for s in paired if s['gpu']['gpu_percent'] <= 10]
        cpu = [statistics.mean(s['host_cpu_per_core']) for s in selected if s.get('host_cpu_per_core')]
        regions.append({'region': name, 'begin': begin, 'end': end, 'seconds': end - begin,
                        'samples': len(selected), 'fresh_gpu_samples': len(fresh),
                        'gpu_mean': statistics.mean(s['gpu']['gpu_percent'] for s in fresh) if fresh else None,
                        'gpu_le10_samples': sum(s['gpu']['gpu_percent'] <= 10 for s in fresh),
                        'gpu_ge90_samples': sum(s['gpu']['gpu_percent'] >= 90 for s in fresh),
                        'fresh_gpu_vlm_pairs': len(paired), 'paired_low_gpu_samples': len(low),
                        'paired_low_gpu_no_vlm_work': sum(
                            s['vlm']['vllm:num_requests_running'] == 0
                            and s['vlm']['vllm:num_requests_waiting'] == 0 for s in low),
                        'host_cpu_mean': statistics.mean(cpu) if cpu else None,
                        'host_available_memory_min_bytes': min(
                            (s['available_memory_bytes'] for s in selected), default=None)})
    return {'scope': 'Approximate log-delimited regions; asynchronous samples, not exact idle duration; host includes other apps',
            'max_probe_age_seconds': max_age, 'markers': markers, 'regions': regions,
            'windows_gpu_attribution': windows_attribution(samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError('Choose a new summary output')
    samples = [json.loads(line) for line in (args.profile/'samples.jsonl').read_text(encoding='utf-8').splitlines()]
    result = summarize(samples)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

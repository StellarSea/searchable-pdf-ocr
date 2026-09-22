import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from summarize_ocr_profile import summarize, windows_attribution


class ProfileSummaryTests(unittest.TestCase):
    def test_windows_engines_are_not_added_and_stale_data_is_excluded(self):
        a = {'name': 'pid_1_gpu_0_engine_0', 'process': 'chrome', 'pid': 1, 'utilization': 70}
        b = {'name': 'pid_1_gpu_0_engine_1', 'process': 'chrome', 'pid': 1, 'utilization': 50}
        result = windows_attribution([
            {'windows_gpu': {'age_seconds': 1, 'engines': [a, b]}},
            {'windows_gpu': {'age_seconds': 11, 'engines': [a]}},
            {'windows_gpu': {'unavailable': 'no permission'}}])
        self.assertEqual(result['fresh_monitor_records'], 1)
        self.assertEqual([row['peak_utilization'] for row in result['engines']], [70, 50])

    def test_missing_marker_is_not_guessed(self):
        self.assertTrue(all('unavailable' in r for r in summarize([])['regions']))

    def test_stale_and_missing_metrics_are_excluded(self):
        samples = []
        for i in range(6):
            samples.append({'elapsed': i, 'host_cpu_per_core': [20, 40],
                            'available_memory_bytes': 100,
                            'events': [], 'gpu': {'age_seconds': 0, 'gpu_percent': 5},
                            'vlm': {'age_seconds': 0, 'vllm:num_requests_running': 0,
                                    'vllm:num_requests_waiting': 0}})
        samples[0]['events'] = [{'elapsed': 0, 'line': '[health] Healthy'}]
        samples[5]['events'] = [{'elapsed': 5, 'line': '[ocr-feed] finished'}]
        samples[1]['gpu']['age_seconds'] = 3
        samples[2]['vlm']['age_seconds'] = 3
        samples[3]['vlm'] = {'age_seconds': 0, 'error': 'unavailable'}
        samples[4]['vlm']['vllm:num_requests_running'] = 1
        body = summarize(samples)['regions'][0]
        self.assertEqual(body['samples'], 5)
        self.assertEqual(body['fresh_gpu_samples'], 4)
        self.assertEqual(body['fresh_gpu_vlm_pairs'], 2)
        self.assertEqual(body['paired_low_gpu_no_vlm_work'], 1)
        self.assertEqual(body['host_cpu_mean'], 30)


if __name__ == '__main__':
    unittest.main()

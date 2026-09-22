import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from compare_document_trials import compare_trials, resources, check_expected


class DocumentTrialTests(unittest.TestCase):
    def fixture(self, folder, text='abc', options=None):
        folder.mkdir()
        request = b'fixed request bytes'
        (folder/'request.pdf').write_bytes(request)
        (folder/'comparison.json').write_text(json.dumps({'status': 'completed',
            'input_sha256': hashlib.sha256(request).hexdigest(), 'input_bytes': len(request),
            'expected_pages': 1, 'options': options or {},
            'records': [{'run': 1, 'seconds': 10}, {'run': 2, 'seconds': 2}, {'run': 3, 'seconds': 4}]}))
        for n in range(1, 4):
            result = {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
                {'block_content': text, 'block_bbox': [0, 0, 10, 10]}]}}]}
            (folder/f'response-{n}.json').write_text(json.dumps(result))

    def test_timing_and_output_variation_are_separate(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td)/'a', Path(td)/'b'
            self.fixture(a)
            self.fixture(b, 'abd')
            result = compare_trials([a, b])
            self.assertEqual(result['trials'][0]['median_seconds'], 4)
            self.assertEqual(result['trials'][0]['excluding_first_median_seconds'], 3)
            self.assertTrue(result['trials'][1]['exact_within_trial'])
            self.assertEqual(len(result['trials'][1]['content_changes_from_first_reference'][0]), 1)

    def test_different_inputs_and_options_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td)/'a', Path(td)/'b'
            self.fixture(a)
            self.fixture(b, options={'changed': True})
            with self.assertRaisesRegex(ValueError, 'options'):
                compare_trials([a, b])
            (a/'request.pdf').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'digest'):
                compare_trials([a])

    def test_profiles_exclude_stale_counters_and_keep_adapters_separate(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)/'a'
            profile = Path(td)/'a-profile'
            profile.mkdir()
            samples = [{'available_memory_bytes': 100, 'gpu': {'age_seconds': 0, 'memory_mib': 12},
                'windows_gpu': {'age_seconds': 0, 'adapters': [
                    {'Name': 'a', 'DedicatedUsage': 2, 'SharedUsage': 3},
                    {'Name': 'b', 'DedicatedUsage': 5, 'SharedUsage': 7}]}}
            , {'available_memory_bytes': 90, 'gpu': {'age_seconds': 3, 'memory_mib': 999}}]
            (profile/'samples.jsonl').write_text('\n'.join(json.dumps(s) for s in samples))
            result = resources(folder)
            self.assertEqual(result['gpu_memory_max_mib'], 12)
            self.assertEqual(result['host_available_memory_min_bytes'], 90)
            self.assertEqual(result['windows_adapters']['a']['shared_max_bytes'], 3)
            self.assertEqual(result['windows_adapters']['b']['shared_max_bytes'], 7)
            self.assertIsNone(result['fresh_vlm_peaks']['vllm:kv_cache_usage_perc'])

    def test_vlm_capacity_uses_only_fresh_numeric_values(self):
        with tempfile.TemporaryDirectory() as td:
            folder, profile = Path(td)/'a', Path(td)/'a-profile'
            profile.mkdir()
            samples = [{'available_memory_bytes': 100, 'vlm': {'age_seconds': age,
                'vllm:kv_cache_usage_perc': value}} for age, value in ((0, .2), (3, .9), (0, None))]
            (profile/'samples.jsonl').write_text('\n'.join(json.dumps(s) for s in samples))
            self.assertEqual(resources(folder)['fresh_vlm_peaks']['vllm:kv_cache_usage_perc'], .2)

    def test_source_bound_check_catches_glyph_changes_but_not_whitespace(self):
        summary = {'input_sha256': 'same'}
        checks = {'input_sha256': 'same', 'checks': [{'page': 1, 'block_index': 0,
            'block_id': 5, 'block_bbox': [0, 0, 10, 10], 'expected_text': '덱을 이용'}]}
        def response(text):
            return [[{'block_id': 5, 'block_bbox': [0, 0, 10, 10], 'block_content': text}]]
        rows = check_expected(summary, [response('덱을\n이용'), response('텍을 이용')], checks)
        self.assertEqual([r['passed'] for r in rows], [True, False])
        checks['checks'][0]['block_bbox'] = [1, 0, 10, 10]
        self.assertFalse(check_expected(summary, [response('덱을 이용')], checks)[0]['passed'])
        checks['input_sha256'] = 'other'
        with self.assertRaisesRegex(ValueError, 'different request'):
            check_expected(summary, [response('덱을 이용')], checks)

    def test_missing_ground_truth_block_is_failure_not_a_pass(self):
        checks = {'input_sha256': 'same', 'checks': [{'page': 1, 'block_index': 0,
            'block_id': 5, 'block_bbox': [0, 0, 10, 10], 'expected_text': 'abc'}]}
        rows = check_expected({'input_sha256': 'same'}, [[[]]], checks)
        self.assertFalse(rows[0]['passed'])
        self.assertFalse(rows[0]['identity_matches'])
        checks['checks'] = []
        with self.assertRaisesRegex(ValueError, 'At least one'):
            check_expected({'input_sha256': 'same'}, [[[]]], checks)


if __name__ == '__main__':
    unittest.main()

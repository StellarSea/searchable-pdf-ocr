"""Read-only VLM counters distinguish generation limits from normal completion."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from profile_ocr_resources import parse_vlm_metrics


class VLMMetricsTests(unittest.TestCase):
    def test_engine_totals_keep_completion_reasons_and_ignore_histogram_buckets(self):
        result = parse_vlm_metrics('''# HELP ignored
vllm:num_requests_running{engine="0"} 2.0
vllm:num_requests_running{engine="1"} 3.0
vllm:request_success_total{finished_reason="stop",engine="0"} 12.0
vllm:request_success_total{engine="1",finished_reason="stop"} 8.0
vllm:request_success_total{finished_reason="length"} 4.0
vllm:request_success_total{finished_reason="abort"} 1.0
vllm:request_generation_tokens_sum{engine="0"} 23000.0
vllm:request_generation_tokens_count{engine="0"} 25.0
vllm:request_generation_tokens_bucket{le="8192"} 25.0
vllm:num_preemptions_total{engine="0"} 2.0
''')
        self.assertEqual(result, {'vllm:num_requests_running': 5.0,
            'vllm:request_success_total:stop': 20.0,
            'vllm:request_success_total:length': 4.0,
            'vllm:request_success_total:abort': 1.0,
            'vllm:request_generation_tokens_sum': 23000.0,
            'vllm:request_generation_tokens_count': 25.0,
            'vllm:num_preemptions_total': 2.0})

    def test_missing_counters_are_unavailable_not_zero(self):
        self.assertEqual(parse_vlm_metrics('# HELP only\n'), {})
        self.assertEqual(parse_vlm_metrics('vllm:request_success_total{engine="0"} 5'), {})
        self.assertEqual(parse_vlm_metrics('vllm:num_requests_waiting 0'),
                         {'vllm:num_requests_waiting': 0.0})


if __name__ == '__main__':
    unittest.main()

"""Offline checks for the live repeatability diagnostic's comparison contract."""
import copy
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from benchmark_document_repeat import content_changes, differences, main, snapshot


class DocumentRepeatTests(unittest.TestCase):
    def test_ignore_transport_but_keep_text_geometry_order(self):
        response = {'layoutParsingResults': [{'prunedResult': {'parsing_res_list': [
            {'block_id': 0, 'block_order': 0, 'block_label': 'text',
             'block_bbox': [1, 2, 3, 4], 'block_content': '17'}]}}]}
        before = copy.deepcopy(response)
        first = snapshot(response, 1)
        response['logId'] = 'transport-only'
        self.assertEqual([], differences(first, snapshot(response, 1)))
        block = response['layoutParsingResults'][0]['prunedResult']['parsing_res_list'][0]
        for key, value in [('block_content', '1'), ('block_bbox', [1, 2, 3, 5]), ('block_order', 2)]:
            prior = block[key]
            block[key] = value
            self.assertEqual([{'page': 1, 'block_index': 0, 'fields': [key]}],
                             differences(first, snapshot(response, 1)))
            block[key] = prior
        self.assertEqual(snapshot(before, 1), first)

    def test_count_and_malformed_data_fail(self):
        with self.assertRaises(ValueError):
            snapshot({'layoutParsingResults': []}, 1)
        with self.assertRaises(ValueError):
            snapshot({'layoutParsingResults': [{'prunedResult': {}}]}, 1)
        with self.assertRaises(ValueError):
            differences([], [[]])
        self.assertTrue(differences([[]], [[{'block_content': 'added'}]]))

    def test_whitespace_is_not_a_character_change(self):
        before = [[{'block_content': '17  abc'}]]
        after = [[{'block_content': '1 7\nabc'}]]
        self.assertEqual([], content_changes(before, after, differences(before, after)))
        after[0][0]['block_content'] = '1 abc'
        self.assertEqual(1, len(content_changes(before, after, differences(before, after))))

    def test_failed_request_is_recorded_without_retry(self):
        response = requests.Response()
        response.status_code = 422
        response._content = b'Invalid diagnostic option'
        error = requests.HTTPError('422', response=response)
        with tempfile.TemporaryDirectory() as directory:
            source, out = Path(directory) / 'source.png', Path(directory) / 'new'
            source.write_bytes(b'fake image; HTTP is mocked')
            argv = ['repeat', '--source', str(source), '--out', str(out), '--image', '--live']
            with patch.object(sys, 'argv', argv), patch('benchmark_document_repeat._call_api', side_effect=error) as call:
                with self.assertRaises(requests.HTTPError):
                    main()
            report = json.loads((out / 'comparison.json').read_text(encoding='utf-8'))
            self.assertEqual('failed', report['status'])
            self.assertEqual('Invalid diagnostic option', report['response_error'])
            self.assertEqual(1, call.call_count)


if __name__ == '__main__':
    unittest.main()

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from analyze_ocr_input_variation import compare


class InputVariationTests(unittest.TestCase):
    def test_uses_original_before_repair_and_does_not_mutate(self):
        pages = [{'parsing_res_list': [{'block_content': 'repaired', 'block_bbox': [1, 2, 3, 4],
                                       'repair_audit': {'original': 'raw'}}]}]
        before = copy.deepcopy(pages)
        other = [{'parsing_res_list': [{'block_content': 'raw', 'block_bbox': [1, 2, 3, 4]}]}]
        self.assertEqual(compare(pages, other), {'upstream_text_changed_pages': [],
                                               'upstream_geometry_changed_pages': []})
        self.assertEqual(pages, before)
        other[0]['parsing_res_list'][0]['block_content'] = 'different'
        self.assertEqual(compare(pages, other)['upstream_text_changed_pages'], [1])

    def test_shifted_pages_or_boxes_are_not_reported_as_text_only(self):
        a = [{'parsing_res_list': [{'block_content': 'a', 'block_bbox': [1, 2, 3, 4]}]}]
        b = copy.deepcopy(a)
        b[0]['parsing_res_list'][0]['block_bbox'][0] = 0
        self.assertEqual(compare(a, b)['upstream_geometry_changed_pages'], [1])
        with self.assertRaises(ValueError):
            compare(a, [])


if __name__ == '__main__':
    unittest.main()

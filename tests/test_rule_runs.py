"""Threshold boundaries, arbitrary strides, dense intervals and exact mask parity."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from benchmark_rule_runs import reference
from ocr_render import _long_runs as candidate


class RuleRunTests(unittest.TestCase):
    def equal(self, mask, length):
        before = mask.copy()
        self.assertTrue(np.array_equal(reference(mask, length), candidate(mask, length)),
                        (mask.shape, length))
        self.assertTrue(np.array_equal(mask, before))

    def test_all_boolean_patterns_through_width_fourteen(self):
        for width in range(15):
            values = np.arange(1 << width)[:, None]
            mask = ((values >> np.arange(width)) & 1).astype(bool)
            for length in range(width + 3):
                self.equal(mask, length)

    def test_independent_run_definition_and_threshold_edges(self):
        rng = np.random.default_rng(42)
        for length in (2, 3, 5, 60, 80):
            mask = rng.random((7, 300)) < 0.75
            mask[0, :length] = True
            mask[0, length] = False
            mask[1, -length:] = True
            expected = np.zeros_like(mask)
            for row in range(len(mask)):
                start = 0
                for col in range(mask.shape[1] + 1):
                    if col == mask.shape[1] or not mask[row, col]:
                        if col - start >= length:
                            expected[row, start:col] = True
                        start = col + 1
            self.assertTrue(np.array_equal(expected, candidate(mask, length)))

    def test_strides_transposes_and_readonly_inputs(self):
        rng = np.random.default_rng(170)
        mask = rng.random((213, 509)) < 0.8
        mask.flags.writeable = False
        for view in (mask, mask.T, mask[::-1, ::-1], mask[::2, ::3]):
            for length in (0, 1, 2, 3, 60, 80, view.shape[1], view.shape[1] + 1):
                self.equal(view, length)

    def test_chunk_boundaries_many_intervals_and_empty_axes(self):
        pairs = np.tile([True, True, False, False], (1100, 300))
        self.equal(pairs, 2)
        self.equal(np.ones((1100, 1200), dtype=bool), 60)
        for shape in ((0, 8), (8, 0), (0, 0), (1, 1048600)):
            self.equal(np.zeros(shape, dtype=bool), 2)

    def test_legacy_non_boolean_arithmetic_is_preserved(self):
        for dtype in (np.int8, np.int32, np.float64):
            mask = np.array([[2, 0, 1, 1, -1, 3, 0]], dtype=dtype)
            for length in (0, 1, 2, 3, 7, 8):
                self.equal(mask, length)

    def test_non_integral_lengths_keep_legacy_error(self):
        mask = np.ones((2, 8), dtype=bool)
        for length in (2.0, 2.5, np.float64(3)):
            with self.assertRaises(TypeError):
                candidate(mask, length)
            with self.assertRaises(TypeError):
                reference(mask, length)


if __name__ == '__main__':
    unittest.main()

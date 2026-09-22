import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_boundary as boundary
import ocr_to_searchable_pdf as ocr


class BoundaryTests(unittest.TestCase):
    def test_follows_ink_across_each_edge(self):
        for ink_box, box, axis, expected in [
            ((25,40,80,60), (50,30,130,90), 0, 23),
            ((100,40,150,60), (50,30,130,90), 2, 152),
            ((60,20,80,60), (50,40,130,90), 1, 18),
            ((60,65,80,110), (50,30,130,90), 3, 112),
        ]:
            with self.subTest(axis=axis):
                ink = np.zeros((150,200), bool)
                x0,y0,x1,y1 = ink_box
                ink[y0:y1,x0:x1] = True
                grown = boundary.expanded_box(ink, box, (40,40))
                self.assertEqual(grown[axis], expected)

    def test_whitespace_rules_and_neighbours_do_not_expand(self):
        ink = np.zeros((150,200), bool)
        ink[40:60,55:90] = True
        ink[40:60,10:30] = True  # Separate column across a whitespace gap.
        box = [50,30,130,90]
        self.assertEqual(boundary.expanded_box(ink, box, (40,40)), box)
        ink[70:72,:] = True  # A page rule, not clipped characters.
        self.assertEqual(boundary.expanded_box(ink, box, (40,40)), box)
        ink[40:60,25:80] = True
        neighbour = [20,35,49,65]
        self.assertEqual(boundary.expanded_box(ink, box, (40,40), [neighbour]), box)

    def test_content_accepts_recovery_but_not_deletions_or_changed_digits(self):
        self.assertTrue(boundary.preserves_content('R 02 title', 'CHAPTER 02 title'))
        self.assertFalse(boundary.preserves_content('R 02 title', 'CHAPTER 03 title'))
        self.assertFalse(boundary.preserves_content('Chapter 12', 'Chapter 123'))
        self.assertFalse(boundary.preserves_content('Original important words', 'Original words'))

    def test_default_cli_enables_boundary_repair_and_versions_completion(self):
        args = ocr.build_parser().parse_args(['book.pdf'])
        self.assertFalse(args.no_boundary_repair)
        self.assertEqual(ocr.run_options(args)['boundary_repair'], boundary.VERSION)
        args = ocr.build_parser().parse_args(['book.pdf','--no-boundary-repair'])
        self.assertNotIn('boundary_repair', ocr.run_options(args))

    def test_real_crop_rerecognition_is_cached_without_mutating_raw_ocr(self):
        with tempfile.TemporaryDirectory() as td:
            source, cache = Path(td)/'source.pdf', Path(td)/'boundary.json'
            with fitz.open() as doc:
                p = doc.new_page(width=300,height=200)
                p.insert_text((40,100), 'CHAPTER 02', fontsize=16)
                doc.save(source)
            pages = [{'width':300,'height':200,'parsing_res_list':[
                {'block_content':'R 02','block_bbox':[80,80,145,105]}]}]
            untouched = copy.deepcopy(pages)
            response = {'layoutParsingResults':[{'prunedResult':{'parsing_res_list':[
                {'block_content':'CHAPTER 02','block_bbox':[0,0,100,20]}]}}]}
            with patch.object(boundary.ocr_api,'_call_api',return_value=response) as api:
                fixed = boundary.repair(source,pages,cache,{'sha256':'one'})
                self.assertEqual(api.call_count,1)
                self.assertEqual(api.call_args.kwargs['options'], {'useLayoutDetection':False})
            block = fixed[0]['parsing_res_list'][0]
            self.assertEqual(block['block_content'],'CHAPTER 02')
            self.assertLess(block['block_bbox'][0], 45)
            self.assertEqual(pages, untouched)
            with patch.object(boundary.ocr_api,'_call_api',side_effect=AssertionError('cache miss')):
                self.assertEqual(boundary.repair(source,pages,cache,{'sha256':'one'}), fixed)
            with patch.object(boundary.ocr_api,'_call_api',return_value=response) as api:
                boundary.repair(source,pages,cache,{'sha256':'different source'})
                self.assertEqual(api.call_count,1)


if __name__ == '__main__':
    unittest.main()

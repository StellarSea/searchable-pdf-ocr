"""Explicit image-bound corrections survive rejection of model boundaries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_to_searchable_pdf import LineRefiner


class ReviewFallbackTests(unittest.TestCase):
    def test_rejected_ocr_uses_reviewed_text_with_original_boundaries_and_cache(self):
        with tempfile.TemporaryDirectory() as directory, fitz.open() as doc:
            page = doc.new_page(width=300, height=180)
            page.insert_text((20, 35), 'Source first line')
            page.insert_text((20, 65), 'Source second line')
            rectangles = [fitz.Rect(15, 20, 240, 40), fitz.Rect(15, 50, 240, 70)]
            chunks = ['🏢이 첫 문장 17입니다.', '두 번째 문장입니다.']
            original = ' '.join(chunks)
            replacement = original.replace('🏢이', '풀이')
            cache = Path(directory)/'cache.json'
            calls = []

            def recognize(images, **kwargs):
                calls.append(images)
                return ['Unrelated 999', 'Different 888']

            first = LineRefiner(cache, {1}, recognize=recognize)
            self.assertEqual(first.refine(page, original, rectangles, chunks), chunks)
            decision = first.cache['decisions'][-1]
            self.assertFalse(decision['accepted'])
            responses = dict(first.cache['responses'])
            review = Path(directory)/'review.json'
            review.write_text(json.dumps({decision['block_key']: {
                'original': original, 'replacement': replacement,
                'reason': 'Compared the source glyphs directly'}}), encoding='utf-8')

            def no_requests(*args, **kwargs):
                self.fail('Cached responses must be reused')

            after = LineRefiner(cache, {1}, review, recognize=no_requests)
            selected = after.refine(page, original, rectangles, chunks)
            self.assertEqual(len(selected), len(rectangles))
            self.assertEqual(''.join(''.join(selected).split()), ''.join(replacement.split()))
            self.assertTrue(selected[0].startswith('풀이'))
            self.assertEqual(after.cache['responses'], responses)
            actual = after.cache['decisions'][-1]
            self.assertFalse(actual['accepted'])
            self.assertEqual(actual['original'], original)
            self.assertEqual(actual['reviewed'], replacement)
            self.assertEqual(actual['image_keys'], decision['image_keys'])
            self.assertEqual(len(calls), 1)
            # A different source image cannot reuse the approved correction.
            page.insert_text((25, 32), 'Changed', color=(1, 0, 0))
            changed = LineRefiner(cache, {1}, review, recognize=recognize)
            self.assertEqual(changed.refine(page, original, rectangles, chunks), chunks)
            self.assertFalse(changed.used_reviews)


if __name__ == '__main__':
    unittest.main()

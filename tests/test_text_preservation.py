"""Literal OCR text, critical reading order, and exact similarity shortcuts."""
import random
import sys
import tempfile
import unittest
from difflib import SequenceMatcher
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_text as text
from audit_ocr_quality import agreement
from ocr_render import layout_table
from ocr_to_searchable_pdf import overlay


class TextPreservationTests(unittest.TestCase):
    def test_mixed_html_preserves_literal_code_spelling(self):
        cases = [
            ('<p>vector<T> <Node>v</Node> <Item/></p>',
             'vector<T> <Node>v</Node> <Item/>'),
            ('<p>한글 <Node>값</Node > &amp; &lt;b&gt;x&lt;/b&gt;</p>',
             '한글 <Node>값</Node > & <b>x</b>'),
            ('<p>custom <p-value>x</p-value> <b>bold</b></p>',
             'custom <p-value>x</p-value> bold'),
            ('<p>A<br/>B<span/>C</p>', 'A BC'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(text.strip_html(source), expected)

    def test_table_cells_preserve_code_and_decode_entities_once(self):
        source = ('<table><tr><td>vector<T></td><td><Node>값</Node></td>'
                  '<td>&lt;b&gt;x&lt;/b&gt; &amp;lt; &amp;</td>'
                  '<td><b>bold</b><br/>next<Item/></td><td/></tr></table>')
        parser = text._TableParser()
        parser.feed(source)
        parser.close()
        cells = [text.strip_html(value) for value, span in parser.rows[0]]
        self.assertEqual(cells, ['vector<T>', '<Node>값</Node>',
                                '<b>x</b> &lt; &', 'bold next<Item/>', ''])
        self.assertEqual(' '.join(cells).strip(), text.strip_html(source))

    def test_unknown_end_tag_can_arrive_in_chunks(self):
        parser = text._TextParser()
        for chunk in ['<p><Node>x</No', 'de ><Item', '/></p>']:
            parser.feed(chunk)
        parser.close()
        self.assertEqual(''.join(parser.parts).strip(), '<Node>x</Node ><Item/>')

    def test_table_layout_keeps_literal_text_in_its_source_cell(self):
        values = [['vector<T>', '<Node>x</Node>', '<b>x</b>'],
                  ['first', 'second', 'third'], ['1', '2', '3']]
        markup = ('<table><tr><td>vector<T></td><td><Node>x</Node></td>'
                  '<td>&lt;b&gt;x&lt;/b&gt;</td></tr><tr><td>first</td><td>second</td>'
                  '<td>third</td></tr><tr><td>1</td><td>2</td><td>3</td></tr></table>')
        with fitz.open() as doc:
            page = doc.new_page(width=600, height=200)
            rects = []
            for row, cells in enumerate(values):
                for column, value in enumerate(cells):
                    x, y = 20 + 190 * column, 25 + 40 * row
                    page.insert_text((x, y + 12), value, fontsize=10)
                    rects.append(fitz.Rect(x, y, x + 140, y + 17))
            result = layout_table(page, fitz.Rect(15, 20, 560, 130), markup, rects,
                                  fitz.Font('helv'))
            self.assertIsNotNone(result)
            boxes, chunks = result
            self.assertEqual(chunks, [value for cells in values for value in cells])
            self.assertEqual(len(boxes), len(rects))
            for box, source_cell in zip(boxes, rects):
                self.assertTrue(source_cell.contains(box), (source_cell, box))

    def test_swapped_critical_tokens_require_review(self):
        prefix = 'The explanatory text stays unchanged. ' * 20
        for before, after in [('first=12;second=34', 'first=34;second=12'),
                              ('a=1.25;b=2.50', 'a=2.50;b=1.25'),
                              ('<a@b.com>;<c@d.com>', '<c@d.com>;<a@b.com>'),
                              ('a=12;b=12;c=34', 'a=12;b=34;c=12')]:
            with self.subTest(before=before):
                original, candidate = prefix + before, prefix + after
                self.assertTrue(text.recognition_disagrees(original, [candidate]))
                self.assertFalse(text.line_refinement_is_safe(original, [candidate]))
                # A line model can still guide boundaries; its text is never copied.
                chunks = text.align_to_recognized_lines(original, [candidate])
                self.assertEqual(chunks, [original])

    def test_similarity_shortcuts_match_exact_reference(self):
        rng = random.Random(294)
        pairs = [('', ''), ('a' * 500, 'a' * 500), ('a' * 500, 'b' * 500)]
        for _ in range(100):
            a = ''.join(rng.choices('abc가나다123 ', k=rng.randrange(1, 120)))
            cut = rng.randrange(len(a))
            b = a[:cut] + rng.choice(['', '가', 'ab', '12345']) + a[cut+1:]
            pairs.append((a, b))
        for a, b in pairs:
            ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
            for threshold in (0, 0.88, 0.94, 0.97, 1, 1.01, ratio):
                self.assertEqual(text._similarity_below(a, b, threshold), ratio < threshold)

    def test_exact_repeated_text_skips_matcher_and_keeps_line_cuts(self):
        original = 'Repeated text 123. ' * 300
        candidates = [original[:950], original[950:1900], original[1900:]]
        with patch.object(text, 'SequenceMatcher', side_effect=AssertionError('quadratic match')):
            self.assertFalse(text.recognition_disagrees(original, candidates))
            self.assertTrue(text.line_refinement_is_safe(original, candidates))
            self.assertTrue(text.line_boundaries_are_usable(original, candidates))
            chunks = text.align_to_recognized_lines(original, candidates)
        self.assertEqual(''.join(''.join(chunks).split()), ''.join(original.split()))
        self.assertEqual(text.align_to_recognized_lines('alpha beta gamma delta',
                         ['alpha be', 'ta gamma', 'delta']), ['alpha', 'beta gamma', 'delta'])

    def test_exact_audit_keeps_score_without_matcher(self):
        row = {'original': 'Repeated words ' * 300, 'candidate': ['Repeated words ' * 300], 'page': 1}
        with patch('audit_ocr_quality.SequenceMatcher', side_effect=AssertionError('quadratic match')):
            result = agreement(None, decisions=[row])
        self.assertEqual(result['character_weighted_agreement'], 1.0)
        self.assertEqual(result['bands'], {'0.99+': 1})

    def test_overlay_preserves_code_text_boxes_and_source_pixels(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td) / 'source.pdf', Path(td) / 'output.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=300, height=150)
                page.draw_rect(fitz.Rect(10, 10, 290, 140), color=(0, 0, 0))
                doc.save(source)
            value = 'vector<T> <Node>x</Node> <Item/>'
            block = {'block_content': f'<p>{value}</p>', 'block_bbox': [20, 30, 280, 60]}
            overlay(source, [{'width': 300, 'height': 150, 'parsing_res_list': [block]}],
                    output, automatic=False)
            with fitz.open(source) as original, fitz.open(output) as result:
                self.assertEqual(original[0].get_pixmap().samples, result[0].get_pixmap().samples)
                self.assertEqual(''.join(result[0].get_text().split()), ''.join(value.split()))
                for token in ('vector<T>', '</Node>', '<Item/>'):
                    boxes = result[0].search_for(token)
                    self.assertTrue(boxes, token)
                    self.assertTrue(all(fitz.Rect(15, 20, 285, 70).contains(box) for box in boxes))


if __name__ == '__main__':
    unittest.main()

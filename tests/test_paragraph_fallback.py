"""Paragraph fallback must retain text inside the source box, including CJK."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr
from ocr_verify import verify


class ParagraphFallbackTests(unittest.TestCase):
    def check_roundtrip(self, value, rotation=0):
        with tempfile.TemporaryDirectory() as directory:
            source, output = (Path(directory) / name for name in ('source.pdf', 'result.pdf'))
            with fitz.open() as doc:
                page = doc.new_page(width=340, height=260)
                page.draw_rect(fitz.Rect(10, 10, 325, 245), color=(0, 0, 0))
                page.set_cropbox(fitz.Rect(5, 5, 335, 255))
                page.set_rotation(rotation)
                width, height = page.rect.width, page.rect.height
                doc.save(source)
            box = [20, 20, 125, 38]
            pages = [{'width': width, 'height': height, 'parsing_res_list': [
                {'block_content': value, 'block_bbox': box}]}]

            def pick(doc):
                doc[0].insert_font(fontname='helv')
                return 'helv', None, fitz.Font('helv')

            with patch.object(ocr, 'pick_font', side_effect=pick):
                report = ocr.overlay(source, pages, output, paragraph_mode=True, automatic=True)
            verify(source, output, report)
            self.assertFalse(report['validation_failed'], report)
            with fitz.open(source) as before, fitz.open(output) as after:
                self.assertEqual(before[0].get_pixmap().samples, after[0].get_pixmap().samples)
                self.assertEqual(''.join(after[0].get_text().split()), ''.join(value.split()))
                self.assertTrue(all(span['type'] == 3 for span in after[0].get_texttrace()))
                for span in after[0].get_texttrace():
                    for char in span['chars']:
                        bounds = fitz.Rect(char[3]) * after[0].rotation_matrix
                        self.assertTrue((fitz.Rect(box) + (-1, -1, 1, 1)).contains(bounds), bounds)
            extracted = PdfReader(io.BytesIO(output.read_bytes())).pages[0].extract_text()
            self.assertEqual(''.join(extracted.split()), ''.join(value.split()))
            return report

    def test_long_repeated_ocr_text_does_not_overflow_page(self):
        value = ' '.join(f'x_{{{i}}}=2' for i in range(1, 700))
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                self.check_roundtrip(value, rotation)

    def test_missing_primary_font_glyph_uses_fallback(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                self.check_roundtrip('[无法识别]', rotation)

    def test_ordinary_paragraph_keeps_existing_textbox_geometry(self):
        from ocr_fonts import insert_invisible_paragraph
        value = 'An ordinary paragraph with several words.'
        box = fitz.Rect(20, 20, 125, 90)
        with fitz.open() as expected, fitz.open() as actual:
            for doc in (expected, actual):
                doc.new_page(width=200, height=150).insert_font(fontname='helv')
            fs = max(2.0, min((box.get_area() / (0.68 * len(value))) ** 0.5, box.height, 40.0))
            for _ in range(12):
                if expected[0].insert_textbox(box, value, fontname='helv', fontsize=fs,
                                             render_mode=3, align=0) >= 0:
                    break
                fs *= 0.8
                if fs < 1.5:
                    self.fail('Ordinary paragraph fixture unexpectedly overflows')
            self.assertFalse(insert_invisible_paragraph(actual[0], value, box, 'helv', fitz.Font('helv'), []))
            self.assertEqual(actual[0].get_text('rawdict'), expected[0].get_text('rawdict'))
            self.assertEqual(actual[0].read_contents(), expected[0].read_contents())


if __name__ == '__main__':
    unittest.main()

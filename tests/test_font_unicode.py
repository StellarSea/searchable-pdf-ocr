"""Supplementary Unicode must survive PDF save/reopen without altering pixels."""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
from ocr_fonts import _utf16_cmap, repair_generated_font_unicode
import ocr_to_searchable_pdf as ocr
from ocr_verify import verify
import test_ocr_automatic as automatic
import ocr_workflow


class FontUnicodeTests(unittest.TestCase):
    def test_failure_message_points_to_real_artifact_and_actual_final_state(self):
        for has_final in (False, True):
            with self.subTest(has_final=has_final), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'book.pdf'
                helper = automatic.AutomaticTests()
                helper.scanned(source, 1)
                out = Path(directory) / 'ocr_output'
                out.mkdir()
                final = out / 'book_auto_searchable.pdf'
                if has_final:
                    final.write_bytes(b'previous final')
                api, lines, _, _ = helper.responder()
                def fail(*args, **kwargs):
                    result = verify(*args, **kwargs)
                    result['validation_failed'] = True
                    return result
                with patch.object(ocr_workflow, 'verify', side_effect=fail):
                    with self.assertRaisesRegex(RuntimeError, 'validation failed') as raised:
                        helper.run_cli(source, api, lines)
                artifact = ocr_workflow.find_artifact(out, 'book_auto_failed_report.json')
                self.assertTrue(artifact.exists())
                self.assertIn(str(artifact), str(raised.exception))
                self.assertIn('previous final PDF retained' if has_final else 'no final PDF published',
                              str(raised.exception))
                self.assertEqual(final.exists(), has_final)
                if has_final:
                    self.assertEqual(final.read_bytes(), b'previous final')

    def test_scalar_ranges_and_surrogate_boundary(self):
        original = (b'1 begincodespacerange\n<0000> <ffff>\nendcodespacerange\n'
                    b'3 beginbfrange\n<0001> <0002> <0041>\n'
                    b'<0003> <0005> <1f3ff>\n<0006> <0006> <100000>\nendbfrange\n'
                    b'2 beginbfchar\n<0007> <1f3e2>\n<0008> <d83cdfe2>\nendbfchar\n')
        fixed = _utf16_cmap(original)
        self.assertIn(b'<0001> <0002> <0041>', fixed)
        self.assertIn(b'<0003> <0005> [<d83cdfff> <d83ddc00> <d83ddc01>]', fixed)
        self.assertIn(b'<0006> <0006> [<dbc0dc00>]', fixed)
        self.assertIn(b'<0007> <d83cdfe2>', fixed)
        self.assertIn(b'<0008> <d83cdfe2>', fixed)
        self.assertEqual(_utf16_cmap(fixed), fixed)
        self.assertTrue(fixed.startswith(original.split(b'3 beginbfrange')[0]))

    def test_valid_multichar_and_array_destinations_are_unchanged(self):
        valid = (b'2 beginbfchar\n<0001> <00660069>\n<0002> <d840dc00>\nendbfchar\n'
                 b'1 beginbfrange\n<0003> <0004> [<d83cdfe2> <0061>]\nendbfrange\n')
        self.assertEqual(_utf16_cmap(valid), valid)

    def test_invalid_scalar_is_not_silently_truncated(self):
        for row in (b'<0001> <0002> <10ffff>', b'<0002> <0001> <1f3e2>'):
            with self.assertRaisesRegex(ValueError, 'Invalid generated'):
                _utf16_cmap(b'1 beginbfrange\n' + row + b'\nendbfrange')

    def test_original_fonts_and_shared_original_stream_are_untouched(self):
        malformed = b'1 beginbfchar\n<0001> <1f3e2>\nendbfchar'
        with fitz.open() as doc:
            doc.new_page()
            def stream():
                xref = doc.get_new_xref()
                doc.update_object(xref, '<<>>')
                doc.update_stream(xref, malformed)
                return xref
            def font(cmap):
                xref = doc.get_new_xref()
                doc.update_object(xref, f'<< /Type /Font /Subtype /Type0 /ToUnicode {cmap} 0 R >>')
            original_map = stream()
            font(original_map)
            boundary = doc.xref_length()
            font(original_map)
            new_map = stream()
            font(new_map)
            font(new_map)
            self.assertEqual(repair_generated_font_unicode(doc, boundary), 1)
            self.assertEqual(doc.xref_stream(original_map), malformed)
            self.assertIn(b'<d83cdfe2>', doc.xref_stream(new_map))
            self.assertEqual(repair_generated_font_unicode(doc, boundary), 0)

    @unittest.skipUnless(Path('C:/Windows/Fonts/seguisym.ttf').is_file(), 'Windows supplementary font')
    def test_overlay_reopens_as_exact_unicode_in_two_extractors(self):
        font_path = 'C:/Windows/Fonts/seguisym.ttf'
        def pick(doc):
            doc[0].insert_font(fontname='ocrtest', fontfile=font_path)
            return 'ocrtest', font_path, fitz.Font(fontfile=font_path)
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'source.pdf', Path(directory) / 'output.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=500, height=160)
                page.draw_rect(fitz.Rect(3, 3, 495, 155), color=(0, 0, 0))
                doc.save(source)
            value = 'A\U0001f3e2B\U0001f400C'
            pages = [{'width': 500, 'height': 160, 'parsing_res_list': [
                {'block_content': value, 'block_bbox': [20, 20, 470, 120]}]}]
            with patch.object(ocr, 'pick_font', side_effect=pick):
                report = ocr.overlay(source, pages, output, paragraph_mode=True, automatic=True)
            verify(source, output, report)
            self.assertFalse(report['validation_failed'], report)
            with fitz.open(source) as original, fitz.open(output) as result:
                self.assertEqual(original[0].get_pixmap().samples, result[0].get_pixmap().samples)
                self.assertEqual(''.join(result[0].get_text().split()), value)
                self.assertEqual(''.join(PdfReader(io.BytesIO(output.read_bytes())).pages[0].extract_text().split()), value)
                self.assertTrue(result[0].search_for('\U0001f3e2'))


if __name__ == '__main__':
    unittest.main()

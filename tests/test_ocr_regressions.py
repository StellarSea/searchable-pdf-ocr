import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pymupdf as fitz
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr


class RegressionTests(unittest.TestCase):
    def test_reviewed_layout_restores_clipped_heading_and_preserves_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, output, review_path = root/'in.pdf', root/'out.pdf', root/'review.json'
            original = [{'width': 300, 'height': 200, 'parsing_res_list': [
                {'block_content': 'R 02', 'block_bbox': [80, 40, 140, 60]}]}]
            review = {'source_sha256': 'correct', 'pages': [{
                'page': 1, 'reason': 'Compared against the source image',
                'original_blocks': original[0]['parsing_res_list'],
                'lines': [{'text': 'CHAPTER 02', 'bbox': [20, 40, 140, 60]}]}]}
            ocr.atomic_json(review_path, review)
            fixed = ocr.apply_layout_review(original, review_path, 'correct')
            self.assertEqual(original[0]['parsing_res_list'][0]['block_content'], 'R 02')
            with self.assertRaisesRegex(ValueError, 'source PDF'):
                ocr.apply_layout_review(original, review_path, 'wrong')
            with fitz.open() as doc:
                doc.new_page(width=300, height=200)
                doc.save(source)
            with patch.object(ocr, 'detect_lines', side_effect=AssertionError('reclipped review')):
                ocr.overlay(source, fixed, output, automatic=True)
            with fitz.open(output) as doc:
                self.assertTrue(doc[0].search_for('CHAPTER 02'))
            review['pages'][0]['original_blocks'] = []
            ocr.atomic_json(review_path, review)
            with self.assertRaisesRegex(ValueError, 'original OCR'):
                ocr.apply_layout_review(original, review_path, 'correct')

    def test_line_alignment_preserves_content_despite_recognition_changes(self):
        original = '그림 20.1에서 잠깐 닫거나 열면 반응한다.'
        candidates = ['그', '림 20.1에서 갑자기 닫거나', '열면 반응한다.']
        result = ocr.align_to_recognized_lines(original, candidates)
        self.assertEqual(result, ['그', '림 20.1에서 잠깐 닫거나', '열면 반응한다.'])
        self.assertEqual(''.join(''.join(result).split()), ''.join(original.split()))

    def test_line_refinement_guards_content_and_critical_tokens(self):
        text = '문장 경계를 정확히 읽어 원본의 모든 내용을 안전하게 보존해야 합니다. 값은 47992입니다.'
        self.assertTrue(ocr.line_refinement_is_safe(text, [text[:20], text[20:]]))
        self.assertFalse(ocr.line_refinement_is_safe(text, ['']))
        self.assertFalse(ocr.line_refinement_is_safe(text, [text.replace('47992', '47993')]))
        self.assertFalse(ocr.line_refinement_is_safe(text, ['전혀 관계없는 문장입니다.']))

    def test_line_refiner_reuses_image_cache_and_retains_failed_candidate(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            boxes = [fitz.Rect(10,10,150,25),fitz.Rect(10,30,150,45)]
            page.insert_text((12,22),'First')
            page.insert_text((12,42),'Second')
            values = ['원본 첫 줄입니다.', '원본 두 번째 줄입니다.']
            refiner = ocr.LineRefiner(Path(td)/'lines.json',{1})
            with patch.object(ocr, 'recognize_lines', return_value=values) as api:
                self.assertEqual(refiner.refine(page,''.join(values),boxes,values), values)
                self.assertEqual(api.call_count,1)
                self.assertEqual(len(api.call_args.args[0]), 2)
                self.assertTrue(refiner.cache['decisions'][-1]['accepted'])
            with patch.object(ocr, 'recognize_lines', side_effect=AssertionError('cache miss')):
                self.assertEqual(refiner.refine(page,''.join(values),boxes,values), values)
                self.assertEqual(refiner.refine(page,'다른 원본',boxes,['다른','원본']), ['다른','원본'])
            self.assertFalse(refiner.cache['decisions'][-1]['accepted'])

    def test_line_refiner_review_is_bound_to_images_and_original(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            box = [fitz.Rect(10,10,100,25)]
            page.insert_text((12,22),'text')
            raw = '카르노 맥을 이용합니다.'
            cache = Path(td)/'lines.json'
            refiner = ocr.LineRefiner(cache,{1})
            with patch.object(ocr,'recognize_lines',return_value=[raw]):
                refiner.refine(page,raw,box,[raw])
            key = refiner.cache['decisions'][0]['block_key']
            review = Path(td)/'review.json'
            ocr.atomic_json(review,{key:{'original':raw,'replacement':raw.replace('맥','맵'),
                                         'reason':'Verified against source'}})
            refiner = ocr.LineRefiner(cache,{1},review)
            with patch.object(ocr,'recognize_lines',side_effect=AssertionError('cache miss')):
                self.assertEqual(refiner.refine(page,raw,box,[raw]),['카르노 맵을 이용합니다.'])
            refiner.reviews[key]['original'] = 'wrong original'
            with self.assertRaisesRegex(ValueError,'Review does not match'):
                refiner.refine(page,raw,box,[raw])

    def test_line_refiner_stops_new_requests_after_fallback_failure(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            refiner = ocr.LineRefiner(Path(td)/'lines.json',{1})
            with patch.object(ocr,'recognize_lines',side_effect=RuntimeError('offline')) as call:
                for y in (10,30):
                    self.assertEqual(refiner.refine(page,'한글',[fitz.Rect(10,y,100,y+10)],['한글']),['한글'])
                self.assertEqual(call.call_count,2)

    def test_line_refiner_batches_all_missing_lines_in_one_request(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            boxes, chunks = [], []
            for index in range(8):
                y = 10 + index * 25
                text = f'Line {index}'
                boxes.append(fitz.Rect(10, y, 150, y + 18))
                chunks.append(text)
                page.insert_text((12, y + 14), text)

            refiner = ocr.LineRefiner(Path(td)/'lines.json',{1})
            with patch.object(ocr, 'recognize_lines', return_value=chunks) as api:
                refiner.refine(page, ''.join(chunks), boxes, chunks)
            self.assertEqual(api.call_count, 1)
            self.assertEqual(len(api.call_args.args[0]), 8)

    def test_recognize_lines_splits_requests_at_server_limit(self):
        request_sizes = []

        def post(_url, json, timeout):
            start = sum(request_sizes)
            request_sizes.append(len(json['images']))
            response = Mock()
            response.json.return_value = {
                'texts': [f'line {index}' for index in range(start, start + len(json['images']))]
            }
            return response

        images = [f'image {index}'.encode() for index in range(331)]
        with patch.object(ocr.requests, 'post', side_effect=post):
            values = ocr.recognize_lines(images, lang='korean')

        self.assertEqual(request_sizes, [256, 75])
        self.assertEqual(values, [f'line {index}' for index in range(331)])

    def test_aligned_boundary_never_lands_inside_a_latin_word(self):
        """A recognizer that drops characters used to shift the boundary mid-word."""
        original = '71. What type of products does the business repair?'
        # The recognizer lost "ss", so the mapped boundary falls inside "business".
        candidates = ['71. What type of products d oes the busine', 'repair?']
        result = ocr.align_to_recognized_lines(original, candidates)
        self.assertEqual(result, ['71. What type of products does the business', 'repair?'])
        self.assertEqual(''.join(''.join(result).split()), ''.join(original.split()))

        # Korean wraps between syllables in print, so Hangul must stay splittable.
        korean = '토익 시험은 세계적인 직무 영어능력 평가 시험으로 지난'
        parts = ocr.align_to_recognized_lines(korean, ['토익 시험은 세계적인 직무 영어능', '력 평가 시험으로 지난'])
        self.assertEqual(parts, ['토익 시험은 세계적인 직무 영어능', '력 평가 시험으로 지난'])

        # Snapping picks the nearer edge and leaves boundaries already outside.
        self.assertEqual(ocr.snap_out_of_latin_token('the business repair', 15), 13)
        self.assertEqual(ocr.snap_out_of_latin_token('the business repair', 5), 4)
        self.assertEqual(ocr.snap_out_of_latin_token('the business repair', 12), 12)
        self.assertEqual(ocr.snap_out_of_latin_token('English-language', 8), 8)

    def test_a_character_outside_the_book_scripts_does_not_throw_away_the_book(self):
        """These four came out of a Korean physics scan and failed every fallback."""
        with fitz.open() as doc:
            page = doc.new_page()
            name, path, fontobj = ocr.pick_font(doc)
            fallbacks = []
            for label, file in (('ocrcjk', r'C:\Windows\Fonts\simsun.ttc'),
                                ('ocrsymbol', r'C:\Windows\Fonts\seguisym.ttf')):
                if Path(file).exists():
                    fallbacks.append((label, fitz.Font(fontfile=file), file))
            text = 'xᴇᵍใ₹'
            self.assertFalse(all(fontobj.has_glyph(ord(c)) for c in text))
            ocr.insert_invisible_line(page, text, fitz.Rect(20, 20, 260, 44),
                                      name, fontobj, fallbacks)
            extracted = ''.join(page.get_text().split())
            for char in text:
                self.assertIn(char, extracted, f'U+{ord(char):04X} was dropped')

    def test_the_replacement_character_is_not_carried_into_the_text_layer(self):
        """U+FFFD marks text recognition could not represent, and reads back as CJK."""
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td)/'in.pdf', Path(td)/'out.pdf'
            with fitz.open() as doc:
                doc.new_page(width=300, height=120)
                doc.save(src)
            pruned = {'width': 300, 'height': 120, 'parsing_res_list': [
                {'block_content': 'ab�cd�', 'block_bbox': [20, 20, 280, 60]}]}
            report = ocr.overlay(src, [pruned], out, automatic=True)
            page = report['pages'][0]
            self.assertIn('undecodable_character_dropped', page['warnings'])
            self.assertEqual(page['undecodable_characters'], 2)
            import ocr_workflow
            ocr_workflow.verify(src, out, report)
            self.assertFalse(report['validation_failed'])
            with fitz.open(out) as doc:
                extracted = doc[0].get_text()
            self.assertNotIn('�', extracted)
            self.assertIn('ab', ''.join(extracted.split()))

    def test_a_table_cell_holding_only_a_picture_is_not_an_insertion_failure(self):
        """This discarded a finished 392-page book over one image in a table."""
        import ocr_workflow
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td)/'in.pdf', Path(td)/'out.pdf'
            with fitz.open() as doc:
                doc.new_page(width=300, height=200)
                doc.save(src)
            pruned = {'width': 300, 'height': 200, 'parsing_res_list': [
                {'block_content': '14 BCD-to-10 디코더를 설계하시오.', 'block_bbox': [20, 20, 280, 44]},
                {'block_content': '<table><tr><td><img src="imgs/img_1.jpg"></td></tr></table>',
                 'block_bbox': [20, 60, 280, 170]}]}
            report = ocr.overlay(src, [pruned], out, automatic=True)
            page = report['pages'][0]
            self.assertNotIn('insertion_failed', page['warnings'])
            self.assertEqual(page.get('textless_blocks'), 1)
            ocr_workflow.verify(src, out, report)
            self.assertFalse(report['validation_failed'])
            with fitz.open(out) as doc:
                self.assertIn('디코더', doc[0].get_text())

    def test_disagreement_warning_ignores_near_matches_but_not_changed_digits(self):
        text = 'Call 02-2285-1523 for the Official TOEIC prep books in Korea today.'
        self.assertFalse(ocr.recognition_disagrees(text, [text]))
        # One dropped letter is how the small line model reads; not worth review.
        self.assertFalse(ocr.recognition_disagrees(text, [text.replace('Official', 'Oficial')]))
        # A changed number always is, however close the rest looks.
        self.assertTrue(ocr.recognition_disagrees(text, [text.replace('1523', '1520')]))
        self.assertTrue(ocr.recognition_disagrees(text, ['완전히 다른 문장입니다.']))
        self.assertTrue(ocr.recognition_disagrees(text, ['']))

    def test_korean_blocks_use_the_korean_recognizer_and_its_own_cache(self):
        """The default dictionary has no Hangul, so the model must be chosen per block."""
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            boxes = [fitz.Rect(10, 10, 150, 28), fitz.Rect(10, 35, 150, 53)]
            page.insert_text((12, 24), 'First')
            page.insert_text((12, 49), 'Second')
            refiner = ocr.LineRefiner(Path(td)/'lines.json', {1})

            korean = ['한글 첫 줄입니다.', '한글 두 번째 줄입니다.']
            with patch.object(ocr, 'recognize_lines', return_value=korean) as api:
                refiner.refine(page, ''.join(korean), boxes, korean)
            self.assertEqual(api.call_args.kwargs['lang'], 'korean')
            korean_keys = refiner.cache['decisions'][-1]['image_keys']
            self.assertEqual(refiner.cache['decisions'][-1]['engine'], ocr.LINE_ENGINE_KOREAN)

            latin = ['A first English line.', 'A second English line.']
            with patch.object(ocr, 'recognize_lines', return_value=latin) as api:
                refiner.refine(page, ''.join(latin), boxes, latin)
            self.assertIsNone(api.call_args.kwargs['lang'])
            self.assertEqual(refiner.cache['decisions'][-1]['engine'], ocr.LINE_ENGINE)

            # Same crops, different model: the caches must not be shared.
            self.assertEqual(len(korean_keys), 2)
            self.assertTrue(set(korean_keys).isdisjoint(refiner.cache['decisions'][-1]['image_keys']))

            # A mixed block stays on the Korean model, which also reads Latin.
            # Fresh boxes, so these crops are not already cached from above.
            elsewhere = [fitz.Rect(10, 60, 150, 78), fitz.Rect(10, 85, 150, 103)]
            page.insert_text((12, 74), 'Third')
            page.insert_text((12, 99), 'Fourth')
            mixed = ['YBM은 ETS 독점', '계약사입니다.']
            with patch.object(ocr, 'recognize_lines', return_value=mixed) as api:
                refiner.refine(page, ''.join(mixed), elsewhere, mixed)
            self.assertEqual(api.call_args.kwargs['lang'], 'korean')

    def test_line_refiner_recovers_then_retries_failed_crop_sequentially(self):
        with tempfile.TemporaryDirectory() as td, fitz.open() as doc:
            page = doc.new_page()
            boxes = [fitz.Rect(10, 10, 150, 28), fitz.Rect(10, 35, 150, 53)]
            chunks = ['recognized', 'recognized']
            page.insert_text((12, 24), 'First')
            page.insert_text((12, 49), 'Second')
            service = Mock()
            refiner = ocr.LineRefiner(Path(td)/'lines.json', {1}, service=service)
            with patch.object(ocr, 'recognize_lines', side_effect=[RuntimeError('batch timeout'), chunks]) as api:
                self.assertEqual(refiner.refine(page, ''.join(chunks), boxes, chunks), chunks)
            self.assertEqual(api.call_count, 2)
            service.ensure.assert_called_once_with()
            service.recover.assert_called_once_with()
            self.assertTrue(refiner.cache['decisions'][-1]['accepted'])

    def test_fast_boundary_guide_can_disagree_without_changing_original_content(self):
        original = 'Order 47992 will arrive tomorrow morning.'
        candidates = ['Order 4799Z will arrive', 'tomorrow morning.']
        self.assertTrue(ocr.line_boundaries_are_usable(original, candidates))
        selected = ocr.align_to_recognized_lines(original, candidates)
        self.assertEqual(''.join(''.join(selected).split()), ''.join(original.split()))

    def test_korean_can_break_inside_a_word(self):
        font = fitz.Font('korea')
        expected = ['생각', '하며, 다음', '문장입니다.']
        rects = [fitz.Rect(0, i*20, font.text_length(t, fontsize=10), i*20+12)
                 for i,t in enumerate(expected)]
        chunks = ocr.split_text('생각하며, 다음문장입니다.', rects, font)
        self.assertEqual(chunks, expected)

    def test_korean_split_keeps_latin_words_and_all_characters(self):
        font = fitz.Font('korea')
        text = '한글과 decoder 단어 및 0.950c 숫자를 보존합니다.'
        rects = [fitz.Rect(0,i*20,80,i*20+12) for i in range(5)]
        chunks = ocr.split_text(text, rects, font)
        self.assertEqual(''.join(''.join(chunks).split()), ''.join(text.split()))
        self.assertTrue(any('decoder' in c for c in chunks))
        self.assertTrue(any('0.950c' in c for c in chunks))

    def test_oversized_scan_coordinates_do_not_split_lines_into_words(self):
        """Scanner pixel coordinates used as points must scale line thresholds."""
        with fitz.open() as doc:
            page = doc.new_page(width=3346.5, height=4314.75)
            page.insert_font(fontname='korea')
            first = '이러한 링크를 다양하게 늘리면 선형 자료 구조 뿐 아니라'
            second = '두 번째 줄입니다'
            page.insert_text((500, 1000), first, fontname='korea', fontsize=60)
            page.insert_text((500, 1120), second, fontname='korea', fontsize=60)

            self.assertGreater(ocr.page_coordinate_scale(page), 5)
            rects = ocr.detect_lines(page, fitz.Rect(450, 900, 2900, 1180))
            self.assertEqual(len(rects), 2)
            self.assertLess(rects[0].y1, rects[1].y0)

    def test_normal_pdf_coordinates_are_not_rescaled(self):
        with fitz.open() as doc:
            page = doc.new_page(width=612, height=792)
            self.assertEqual(ocr.page_coordinate_scale(page), 1.0)

    def test_a_rotated_cropped_page_is_rendered_upright_before_ocr(self):
        """The layout model read less than half the text off a rotated, cropped scan."""
        with tempfile.TemporaryDirectory() as td:
            plain, awkward = Path(td)/'plain.pdf', Path(td)/'awkward.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=300, height=200)
                page.insert_text((20, 40), 'content', fontsize=11)
                doc.save(plain)
            with fitz.open() as doc:
                page = doc.new_page(width=300, height=200)
                page.insert_text((20, 40), 'content', fontsize=11)
                page.set_rotation(270)
                page.set_cropbox(fitz.Rect(3, 3, 297, 197))
                doc.save(awkward)

            self.assertFalse(ocr.needs_flattening(plain))
            self.assertTrue(ocr.needs_flattening(awkward))
            # Only the awkward one records it, so untouched books keep their cache.
            self.assertNotIn('ocr_input', ocr.source_identity(plain))
            self.assertIn('ocr_input', ocr.source_identity(awkward))

            start, chunk, total = next(iter(ocr.split_pdf(awkward, 1)))
            self.assertEqual((start, total), (0, 1))
            with fitz.open(stream=chunk, filetype='pdf') as sent, fitz.open(awkward) as original:
                self.assertEqual(sent[0].rotation, 0)
                self.assertEqual(tuple(sent[0].cropbox), tuple(sent[0].mediabox))
                # The pixels handed over are the ones page.rect describes.
                self.assertAlmostEqual(sent[0].rect.width, original[0].rect.width, delta=1)
                self.assertAlmostEqual(sent[0].rect.height, original[0].rect.height, delta=1)

            # A plain document is still passed through without re-rendering.
            _, untouched, _ = next(iter(ocr.split_pdf(plain, 1)))
            with fitz.open(stream=untouched, filetype='pdf') as sent:
                self.assertIn('content', sent[0].get_text())

    def test_debug_borders_land_on_the_text_on_a_cropped_page(self):
        """draw_rect works from the media box, so a crop box shifted the borders."""
        import numpy as np
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td)/'in.pdf', Path(td)/'out.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=300, height=200)
                page.insert_text((22, 38), 'marker text', fontsize=15)
                page.set_rotation(270)
                page.set_cropbox(fitz.Rect(6, 6, 294, 194))
                doc.save(src)

            zoom = 6.0
            with fitz.open(src) as doc:
                page = doc[0]
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                      colorspace=fitz.csGRAY, alpha=False)
                g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width)
                ys, xs = np.nonzero(g < 150)
                ink = fitz.Rect(xs.min()/zoom, ys.min()/zoom,
                                xs.max()/zoom, ys.max()/zoom)
                size = (page.rect.width, page.rect.height)
            # Describe the block exactly where the ink is, at scale 1.
            pruned = {'width': size[0], 'height': size[1], 'parsing_res_list': [
                {'block_content': 'marker text',
                 'block_bbox': [ink.x0 - 2, ink.y0 - 2, ink.x1 + 2, ink.y1 + 2]}]}
            ocr.overlay(src, [pruned], out, debug=True, automatic=False)

            with fitz.open(out) as doc:
                page = doc[0]
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                    pix.height, pix.width, 3)
                red = (arr[:, :, 0] > 150) & (arr[:, :, 1] < 110) & (arr[:, :, 2] < 110)
                ys, xs = np.nonzero(red)
            self.assertTrue(xs.size, 'no debug border was drawn')
            # The border must sit on the ink it marks, not a crop box away.
            self.assertAlmostEqual(xs.min()/zoom, ink.x0, delta=2.0)
            self.assertAlmostEqual(ys.min()/zoom, ink.y0, delta=2.0)

    def test_short_text_in_a_wide_box_still_covers_it(self):
        """Every glyph needs a character under it, or a drag over it lets go."""
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation), fitz.open() as doc:
                page = doc.new_page(width=400, height=300)
                page.set_rotation(rotation)
                name, path, fontobj = ocr.pick_font(doc)
                # A table cell: the box is as wide as the column, the ink in it
                # is a few digits tall. Sizing the text by height alone left the
                # rest of the cell with no character in it at all.
                box = fitz.Rect(20, 40, 20 + page.rect.width * 0.5, 47)
                ocr.insert_invisible_line(page, '10001', box, name, fontobj, [])

                boxes = []
                for b in page.get_text('rawdict')['blocks']:
                    for l in b.get('lines', []):
                        for s in l.get('spans', []):
                            for c in s.get('chars', []):
                                if not c['c'].strip():
                                    continue
                                r = fitz.Rect(c['bbox']) * page.rotation_matrix
                                r.normalize()
                                boxes.append(r)
                self.assertTrue(boxes, 'nothing was inserted')
                covered = max(r.x1 for r in boxes) - min(r.x0 for r in boxes)
                self.assertGreater(covered, box.width * 0.85,
                                   'the text stops short of the box it fills')
                # Stretching must not make the glyphs tall enough to reach the
                # lines above and below.
                height = max(r.y1 for r in boxes) - min(r.y0 for r in boxes)
                self.assertLess(height, box.height * 2.6)

    def test_math_conversion_is_conservative(self):
        self.assertEqual(ocr.strip_html(r'$ 3 \times 8 $ decoder'), '3 × 8 decoder')
        for text in (r'$\frac{a}{b}$', r'$x_{1} \times y$', r'$x \unknown y$',
                     r'Price $20 and $30', r'$$3 \times 8$$', r'\$3 \times 8$'):
            self.assertEqual(ocr.strip_html(text), text)

    def test_addresses_and_comparisons_survive(self):
        for text in ('To: Alice <alice@example.com>', 'x < 5 and y > 2'):
            self.assertEqual(ocr.strip_html(text), text)
        self.assertEqual(ocr.strip_html('<td>A &lt;a@b.com&gt;</td>'), 'A <a@b.com>')

    def test_cell_and_line_boundaries(self):
        self.assertEqual(ocr.text_units('<table><tr><td>To:</td><td>Alice</td></tr>'
                                        '<tr><td>From:</td><td>Bob</td></tr></table>'),
                         ['To:', 'Alice', 'From:', 'Bob'])
        self.assertEqual(ocr.text_units('Subject: Details\nDear Alice,'),
                         ['Subject: Details', 'Dear Alice,'])

    def test_repeated_text_at_different_positions(self):
        a = {'block_content': 'Same', 'block_bbox': [0, 0, 50, 20]}
        b = {'block_content': 'Same', 'block_bbox': [0, 40, 50, 60]}
        self.assertEqual(len(ocr._find_blocks({'parsing_res_list': [a, a, b]})), 2)

    def test_short_api_batch_fails_before_shifting_pages(self):
        with patch.object(ocr, 'split_pdf', return_value=iter([(0, b'x', 2)])), \
             patch.object(ocr, 'ocr_pdf', return_value={'layoutParsingResults': []}):
            with self.assertRaisesRegex(RuntimeError, 'page count mismatch'):
                ocr.run_ocr(Path('unused'), 2)

    def test_repair_does_not_replace_known_content(self):
        old = 'To: Alice <alice@example.com> Order 47992'
        self.assertFalse(ocr.repair_is_safe(old, 'Unrelated content ' * 30))
        self.assertFalse(ocr.repair_is_safe(old, old.replace('47992', '47993') + ' More body' * 20))
        self.assertTrue(ocr.repair_is_safe(old, old + ' Recovered body text.' * 20))
        self.assertTrue(ocr.repair_is_safe('', 'Recovered body text.' * 20))

    def test_units_cannot_steal_the_next_cell(self):
        font = fitz.Font('helv')
        rects = [fitz.Rect(0, 0, w, 10) for w in [20, 250, 30, 240, 40, 200, 120]]
        text = 'To:\nAlice <alice@example.com>\nFrom:\nBob <bob@example.com>\nSubject:\nStatus report\nDear Alice,'
        chunks = ocr.split_text(text, rects, font)
        self.assertEqual(chunks, text.splitlines())

    def test_checkpoint_keeps_completed_batch(self):
        row = {'markdown': {'text': 'page'}, 'prunedResult': {}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'partial.json'
            with patch.object(ocr, 'split_pdf', return_value=iter([(0,b'a',2),(1,b'b',2)])), \
                 patch.object(ocr, 'ocr_pdf', side_effect=[{'layoutParsingResults':[row]}, RuntimeError('down')]):
                with self.assertRaisesRegex(RuntimeError, 'down'):
                    ocr.run_ocr(Path('unused'), 1, path, {'id':1})
            self.assertEqual(len(json.loads(path.read_text())['pages']), 1)
            with patch.object(ocr, 'split_pdf', return_value=iter([(0,b'a',2),(1,b'b',2)])), \
                 patch.object(ocr, 'ocr_pdf', return_value={'layoutParsingResults':[row]}) as call:
                self.assertEqual(len(ocr.run_ocr(Path('unused'), 1, path, {'id':1})[1]), 2)
                call.assert_called_once_with(b'b')

    def test_rotated_invisible_text_preserves_pixels_and_coordinates(self):
        font = fitz.Font('helv')
        for rotation in (0, 90, 180, 270):
            with fitz.open() as doc:
                page = doc.new_page(width=300,height=400)
                page.set_cropbox(fitz.Rect(10,20,290,380))
                page.set_rotation(rotation)
                before = page.get_pixmap().samples
                rect = fitz.Rect(30,40,180,60)
                ocr.insert_invisible_line(page,'Hello World',rect,'helv',font,[])
                self.assertEqual(before,page.get_pixmap().samples)
                self.assertIn('Hello World',page.get_text())
                trace = page.get_texttrace()[0]
                self.assertEqual(trace['type'],3)
                origin=fitz.Point(trace['chars'][0][2])*page.rotation_matrix
                # A stretched line carries a matrix of its own, and the round
                # trip through it costs a thousandth of a point.
                self.assertAlmostEqual(origin.x,rect.x0,delta=0.01)
                self.assertAlmostEqual(origin.y,rect.y1-rect.height*0.18,delta=0.01)

    def test_fallback_font_preserves_checkbox(self):
        path=Path(r'C:\Windows\Fonts\seguisym.ttf')
        if not path.exists():
            self.skipTest('Windows symbol font unavailable')
        with fitz.open() as doc:
            p=doc.new_page()
            ocr.insert_invisible_line(p,'A ☐ B',fitz.Rect(20,20,120,40),'helv',fitz.Font('helv'),
                                      [('ocrsymbol',fitz.Font(fontfile=str(path)),str(path))])
            self.assertIn('☐',p.get_text())
            self.assertNotIn('\x00',p.get_text())

    def test_multiline_table_cells_resolve_split_text_and_preserve_characters(self):
        # Three logical rows, four physical bands. At least one row must use
        # the multiline branch that the old single-band table test missed.
        rows = [['Alpha Beta', '100 200', 'One Two'],
                ['Gamma', '300', 'Three'], ['Delta', '400', 'Four']]
        physical = [['Alpha', '100', 'One'], ['Beta', '200', 'Two'],
                    rows[1], rows[2]]
        html = '<table>' + ''.join('<tr>'+''.join('<td>'+v+'</td>' for v in row)+'</tr>'
                                   for row in rows) + '</table>'
        font = fitz.Font('helv')
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=200)
            rects = []
            for ri, row in enumerate(physical):
                for ci, value in enumerate(row):
                    x, y = 30+ci*80, 30+ri*30
                    page.insert_text((x, y+10), value, fontsize=10)
                    rects.append(fitz.Rect(x, y, x+font.text_length(value, fontsize=10), y+13))
            result = ocr.layout_table(page, fitz.Rect(20, 20, 280, 140), html, rects, font)
            self.assertIsNotNone(result)
            boxes, chunks = result
            self.assertGreater(len(boxes), 9)
            self.assertEqual(len(boxes), len(chunks))
            self.assertEqual(''.join(''.join(chunks).split()), ''.join(''.join(v for r in rows for v in r).split()))

    def test_table_reconstructs_merged_cells_without_consuming_neighbor_text(self):
        rows = [['NAME','VALUE','RANK'],['ALPHA','5000','1'],['BETA','3000','2']]
        html = '<table>' + ''.join('<tr>'+''.join('<td>'+v+'</td>' for v in row)+'</tr>'
                                   for row in rows) + '</table>'
        font = fitz.Font('helv')
        with fitz.open() as doc:
            page = doc.new_page(width=300,height=200)
            rects=[]
            for ri,row in enumerate(rows):
                for ci,value in enumerate(row):
                    x,y=30+ci*80,40+ri*30
                    page.insert_text((x,y+10),value,fontsize=10)
                    rects.append(fitz.Rect(x,y,x+font.text_length(value,fontsize=10),y+13))
            rects[6] |= rects[7]
            del rects[7]
            result=ocr.layout_table(page,fitz.Rect(20,30,280,130),html,rects,font)
            self.assertIsNotNone(result)
            boxes,chunks=result
            self.assertEqual(chunks,[v for row in rows for v in row])
            for index,box in enumerate(boxes):
                self.assertLess(abs(box.x0-(30+(index%3)*80)),4)


if __name__ == '__main__':
    unittest.main()


class QualityAuditTests(unittest.TestCase):
    def pdf(self, path, pages):
        with fitz.open() as doc:
            for body in pages:
                page = doc.new_page(width=300, height=400)
                page.insert_text((20, 30), body, fontsize=9)
            doc.save(path)

    def test_unrelated_numbering_on_a_page_does_not_look_like_missing_items(self):
        """A form printed as a question's graphic numbers its own fields from 1."""
        import audit_ocr_quality as audit
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.pdf(src, ['1. first\n2. second\n3. third',
                           '1. first\n2. second\n3. third',
                           # a registration form restarts at 1 beside item 3
                           '1. Name\n2. Street\n3. third'])
            result = audit.sequence(src, 1, 3, copies=3)
            self.assertEqual(result['items_short'], [])
            self.assertEqual(result['total_shortfall'], 0)

    def test_a_genuinely_missing_item_is_reported_with_its_pages(self):
        import audit_ocr_quality as audit
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.pdf(src, ['1. first\n2. second\n3. third', '1. first\n3. third'])
            result = audit.sequence(src, 1, 3, copies=2)
            self.assertEqual([s['item'] for s in result['items_short']], [2])
            self.assertEqual(result['items_short'][0]['pages'], [1])
            self.assertEqual(result['total_shortfall'], 1)

    def test_the_audit_reads_the_layout_the_pdf_was_actually_built_from(self):
        """A fixed book looked unchanged because the stale cache was audited."""
        import hashlib
        import json as _json
        import audit_ocr_quality as audit
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            stem = 'book'
            pdf = folder / f'{stem}_auto_searchable.pdf'
            pdf.write_bytes(b'%PDF-1.4')
            stale = folder / f'{stem}_pruned.json'
            ocr.atomic_json(stale, [{'stale': True}])
            # No report yet: fall back to the plain name.
            self.assertEqual(audit.layout_cache(pdf, stem), stale)

            identity = {'sha256': 'abc', 'api': 'x', 'ocr_input': 'flattened-2.0x'}
            ocr.atomic_json(folder / f'{stem}_auto_report.json',
                            {'source_identity': identity})
            fingerprint = hashlib.sha256(
                _json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
            isolated = folder / '.ocr_cache' / fingerprint / 'pruned.json'
            isolated.parent.mkdir(parents=True)
            ocr.atomic_json(isolated, [{'fresh': True}])
            self.assertEqual(audit.layout_cache(pdf, stem), isolated)

    def test_agreement_bands_and_blocks_without_recognizer_output(self):
        import audit_ocr_quality as audit
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'lines.sqlite3'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE decisions (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
            rows = [{'page': 1, 'original': 'Exactly the same text here', 'candidate': ['Exactly the same text here']},
                    {'page': 2, 'original': 'Completely different content', 'candidate': ['']},
                    {'page': 3, 'original': 'abcdefghij', 'candidate': ['abcdefXYZj']}]
            db.executemany('INSERT INTO decisions(value) VALUES (?)',
                           [(json.dumps(r, ensure_ascii=False),) for r in rows])
            db.commit(); db.close()
            result = audit.agreement(path)
            self.assertEqual(result['blocks'], 3)
            self.assertEqual(result['compared'], 2)
            self.assertEqual(result['bands']['no recognizer output'], 1)
            self.assertEqual(result['bands']['0.99+'], 1)
            self.assertEqual(result['bands']['under 0.90'], 1)
            self.assertLess(result['character_weighted_agreement'], 1.0)

    def test_uncovered_ink_is_found_but_a_dark_background_is_not_ink(self):
        """The real case: a divider page whose large numeral was never detected."""
        import audit_ocr_quality as audit
        with tempfile.TemporaryDirectory() as td:
            src, pruned = Path(td)/'doc.pdf', Path(td)/'doc_pruned.json'
            with fitz.open() as doc:
                page = doc.new_page(width=300, height=300)
                page.insert_text((20, 40), 'covered heading', fontsize=14)
                page.insert_text((20, 200), '10', fontsize=90)   # never detected
                dark = doc.new_page(width=300, height=300)
                dark.draw_rect(dark.rect, color=(0.1, 0.1, 0.3), fill=(0.1, 0.1, 0.3))
                dark.insert_text((20, 40), 'on a dark page', fontsize=14, color=(1, 1, 1))
                doc.save(src)
            # Layout that found the heading on each page and nothing else.
            layout = [{'width': 300, 'height': 300, 'parsing_res_list': [
                          {'block_content': 'covered heading', 'block_bbox': [15, 20, 200, 50]}]},
                      {'width': 300, 'height': 300, 'parsing_res_list': [
                          {'block_content': 'on a dark page', 'block_bbox': [15, 20, 200, 50]}]}]
            ocr.atomic_json(pruned, layout)
            result = audit.coverage(src, pruned)
            flagged = {r['page'] for r in result['pages_with_uncovered_ink']}
            self.assertIn(1, flagged)      # the undetected numeral
            self.assertNotIn(2, flagged)   # a dark background is not missing text

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pymupdf as fitz
import ocr_to_searchable_pdf as ocr


class RegressionTests(unittest.TestCase):
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
                self.assertAlmostEqual(origin.x,rect.x0,places=3)
                self.assertAlmostEqual(origin.y,rect.y1-rect.height*0.18,places=3)

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

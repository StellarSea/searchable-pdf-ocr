"""Offline printed-contents, destination and publication regressions."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_bookmarks import build_plan, apply_plan, verify_bookmarks
from ocr_modes import build_parser, run_options
from ocr_source import source_identity
from ocr_storage import atomic_json
from ocr_workflow import document_artifact, find_artifact


def block(text, y=200, x=80, label='text', right=800):
    return {'block_content': text, 'block_bbox': [x, y, right, y+30*len(text.splitlines())],
            'block_label': label}


def page(*blocks):
    return {'width': 1000, 'height': 1400, 'parsing_res_list': list(blocks)}


def sample():
    return [page(block('목차', 80), block('제1장 기초 개념 ..... 1', 200),
                 block('1.1 정의 ..... 3', 250, 110), block('제2장 응용 ..... 5', 300)),
            page(), page(block('제1장 기초 개념', label='paragraph_title')),
            page(), page(block('1.1 정의', label='paragraph_title')),
            page(), page(block('제2장 응용', label='paragraph_title'))]


class BookmarkTests(unittest.TestCase):
    def test_learning_objectives_above_real_sections_do_not_hide_body_headings(self):
        pages=[page(block('Contents'),block('Chapter 9 Files',200),block('9.1 Save 3',300),block('9.1.1 Command 3',400)),
               page(block('CHAPTER 09',label='doc_title'),block('Files',300,label='doc_title'),
                    block('9.1 Save',700),block('9.2 Load',800)),
               page(block('Files',100,label='paragraph_title'),block('| 학습목표',200,label='paragraph_title'),
                    block('9.1 Save',600,label='paragraph_title'),block('9.1.1 Command',800,label='paragraph_title'),
                    block('3',1320,label='number'))]
        self.assertEqual([e['page'] for e in build_plan(pages)['entries']],[2,3,3])

    def test_decorative_exercise_bar_does_not_change_math_bars(self):
        from ocr_bookmark_layout import clean
        self.assertEqual(clean('|연습문제'),'연습문제')
        self.assertEqual(clean('|연습문제|'),'연습문제')
        self.assertEqual(clean('| 학습목표 |'),'학습목표')
        self.assertEqual(clean('| 학습목표'),'학습목표')
        self.assertEqual(clean('|x| + |y|'),'|x| + |y|')

    def test_review_can_select_same_page_repeated_title_by_chapter(self):
        pages=[page(block('Contents'),block('Chapter 1 First',200),block('Exercises ..... 3',300),
                    block('Chapter 2 Second',400),block('Exercises ..... 5',500)),
               page(block('Chapter 1 First',label='paragraph_title')),
               page(block('Exercises',label='paragraph_title'),block('3',1320,label='page_number')),
               page(block('Chapter 2 Second',label='paragraph_title')),
               page(block('Exercises',label='paragraph_title'),block('5',1320,label='page_number'))]
        review=dict(toc_page=1,original_title='Exercises',chapter='2',printed_page=5,reason='Checked source')
        self.assertEqual(len(build_plan(pages,[review])['entries']),4)
        with self.assertRaisesRegex(ValueError,'exactly one'):
            build_plan(pages,[dict(review,chapter='3')])

    def test_damaged_contents_middle_page_is_bridged_and_repair_heading_keeps_order(self):
        from ocr_bookmark_layout import rows, extract_contents
        pages=[page(block('Contents'),block('Chapter 1 First ..... 1',300)),
               page(block('CHAPTER 02 Second',500,label='paragraph_title'),
                    block('1.2 Earlier\nEarlier detail\nEarlier note\nEarlier end\nTER 02 Second\n2.1 Start\n2.2 End\nSummary',200,label='content')),
               page(block('2.3 Next 20\n2.4 More 21\n2.5 Last 22\nExercises 23',200,label='content'))]
        toc,entries=extract_contents([rows(p) for p in pages])
        self.assertEqual(toc,{0,1,2})
        titles=[e['title'] for e in entries]
        self.assertLess(titles.index('1.2 Earlier'),titles.index('TER 02 Second'))
        self.assertNotIn('CHAPTER 02 Second',titles)

    def test_appendix_and_letter_sections_form_a_hierarchy(self):
        pages = [page(block('Contents'), block('☐ 부록 A 광 저장장치 ..... 2', 300),
                      block('A.1 CD-ROM ..... 3', 400), block('A.2 DVD ..... 4', 450)),
                 page(block('APPENDIX', label='paragraph_title'),
                      block('광 저장장치', 300, label='paragraph_title'),
                      block('A.1 CD-ROM', 800), block('A.2 DVD', 900)),
                 page(block('A.1 CD-ROM', label='paragraph_title'), block('3',1320,label='page_number')),
                 page(block('A.2 DVD', label='paragraph_title'), block('4',1320,label='page_number'))]
        plan = build_plan(pages)
        self.assertEqual([(e['page'],e['level']) for e in plan['entries']], [(2,1),(3,2),(4,2)])
        self.assertEqual(plan['review'], [])

    def test_reviewed_destination_requires_exact_unique_heading(self):
        pages = [page(block('Contents'), block('3.5.2 뺄셈 ..... 2', 300)),
                 page(block('3.5.2 밸셈', label='paragraph_title'))]
        correction = dict(toc_page=1, original_title='3.5.2 뺄셈', destination_page=2,
                          destination_text='3.5.2 밸셈', reason='Source heading visually checked')
        self.assertFalse(build_plan(pages)['entries'])
        plan = build_plan(pages, [correction])
        self.assertEqual(plan['entries'][0]['title'], '3.5.2 뺄셈')
        self.assertEqual(plan['entries'][0]['page'], 2)
        with self.assertRaisesRegex(ValueError, 'exactly one OCR heading'):
            build_plan(pages, [dict(correction, destination_text='stale heading')])
        with self.assertRaises(ValueError):
            build_plan(pages, [dict(correction, destination_page=3)])
        pages[1]['parsing_res_list'].append(block('3.5.2 밸셈', 600, label='paragraph_title'))
        with self.assertRaisesRegex(ValueError, 'exactly one OCR heading'):
            build_plan(pages, [correction])

    def test_chapter_contents_panel_is_an_opening_not_the_book_contents(self):
        pages = [page(block('Contents', 80), block('Chapter 1 Basics', 200),
                      block('1.1 Definition ..... 3', 300), block('1.2 Uses ..... 4', 350)),
                 page(block('CHAPTER 01', 150, label='doc_title'),
                      block('Basics', 250, label='doc_title'), block('contents', 700),
                      block('1.1 Definition', 800), block('1.2 Uses', 900)),
                 page(block('Basics', 100, label='doc_title'),
                      block('1.1 Definition', 300, label='paragraph_title'),
                      block('3', 1320, label='page_number')),
                 page(block('1.2 Uses', 300, label='paragraph_title'),
                      block('4', 1320, label='page_number'))]
        plan = build_plan(pages)
        self.assertEqual(plan['toc_pages'], [1])
        self.assertEqual([(e['title'], e['page'], e['level']) for e in plan['entries']],
                         [('Chapter 1 Basics', 2, 1), ('1.1 Definition', 3, 2), ('1.2 Uses', 4, 2)])
        self.assertEqual(plan['review'], [])

    def test_chapter_intro_without_overview_label_wins_over_repeated_title(self):
        pages = [page(block('Contents', 80), block('Chapter 1 Basics', 200),
                      block('1.1 Definition ..... 3', 300), block('1.2 Uses ..... 4', 350)),
                 page(block('CHAPTER', 200, label='paragraph_title'),
                      block('Basics', 300, label='paragraph_title'),
                      block('1.1 Definition', 800), block('1.2 Uses', 900)),
                 page(block('Basics', 100, label='paragraph_title'),
                      block('1.1 Definition', 300, label='paragraph_title'),
                      block('3', 1320, label='page_number')),
                 page(block('1.2 Uses', 300, label='paragraph_title'),
                      block('4', 1320, label='page_number'))]
        plan = build_plan(pages)
        self.assertEqual([e['page'] for e in plan['entries']], [2, 3, 4])
        self.assertEqual(plan['review'], [])

    def test_detached_numeric_column_is_not_assigned_to_last_title(self):
        from ocr_bookmark_layout import rows, printed_entries
        parsed = printed_entries(rows(page(block('4.1 First\n4.2 Second\nExercises\n160\n175\n204'))))
        self.assertTrue(all(e['printed_page'] is None for e in parsed))

    def test_labelled_contents_continuation_survives_missing_folio_column(self):
        from ocr_bookmark_layout import rows, extract_contents
        pages = [page(block('Contents'), block('1.1 First ..... 3', 400)),
                 page(block('Contents', label='header'), block('Chapter 2 Next', 200),
                      block('2.1 Alpha\n2.2 Beta\nExercises\n20\n25\n30', 400))]
        toc, entries = extract_contents([rows(p) for p in pages])
        self.assertEqual(toc, {0, 1})
        self.assertIn('2.2 Beta', [e['title'] for e in entries])

    def test_printed_contents_hierarchy_unicode_and_input_immutability(self):
        pages = sample()
        saved = copy.deepcopy(pages)
        plan = build_plan(pages)
        self.assertEqual(pages, saved)
        self.assertEqual(plan['toc_pages'], [1])
        self.assertEqual([(e['level'], e['title'], e['page']) for e in plan['entries']],
                         [(1, '제1장 기초 개념', 3), (2, '1.1 정의', 5), (1, '제2장 응용', 7)])
        self.assertEqual(plan['review'], [])
        json.dumps(plan, ensure_ascii=False)

    def test_offsets_change_after_inserted_page_and_roman_frontmatter(self):
        pages = [page(block('Contents'), block('Preface ..... iv', 300),
                      block('Chapter 1 Basics ..... 1', 400), block('Chapter 2 Uses ..... 2', 500)),
                 page(block('Preface')), page(block('Chapter 1 Basics')),
                 page(), page(), page(block('Chapter 2 Uses'))]
        self.assertEqual([e['page'] for e in build_plan(pages)['entries']], [2, 3, 6])

    def test_ambiguous_short_title_and_missing_title_are_not_guessed(self):
        pages = [page(block('차례'), block('정의 ..... 1', 300), block('없는 제목 ..... 2', 400)),
                 page(block('정의')), page(block('정의'))]
        plan = build_plan(pages)
        self.assertEqual(plan['entries'], [])
        self.assertEqual([e['reason'] for e in plan['review']], ['ambiguous_title', 'title_not_found'])

    def test_folio_disambiguates_and_conflicting_folio_rejects(self):
        pages = [page(block('차례'), block('정의 ..... 2', 300), block('예제 ..... 4', 400)),
                 page(block('정의'), block('1', 1320, label='page_number')),
                 page(block('정의'), block('2', 1320, label='page_number')),
                 page(block('예제'), block('3', 1320, label='page_number'))]
        plan = build_plan(pages)
        self.assertEqual([e['page'] for e in plan['entries']], [3])
        self.assertEqual(plan['review'][0]['title'], '예제')

    def test_matching_neighbors_can_disambiguate_but_not_invent_title(self):
        pages = [page(block('Contents'), block('Chapter 1 Start ..... 1', 300),
                      block('Summary ..... 2', 400), block('Chapter 2 End ..... 3', 500)),
                 page(block('Summary')), page(block('Chapter 1 Start')),
                 page(block('Summary')), page(block('Chapter 2 End'))]
        self.assertEqual([e['page'] for e in build_plan(pages)['entries']], [3, 4, 5])
        pages[3] = page()
        pages[1] = page()
        plan = build_plan(pages)
        self.assertEqual([e['page'] for e in plan['entries']], [3, 5])
        self.assertEqual(plan['review'][0]['reason'], 'title_not_found')

    def test_continuation_and_split_title_with_detached_folio(self):
        pages = [page(block('Contents', 80), block('Chapter 1 Start ..... 1', 200)),
                 page(block('A long', 200), block('wrapped title ..... 2', 230),
                      block('Chapter 2 End', 300, right=650), block('3', 300, 700)),
                 page(block('Chapter 1 Start')), page(block('A long wrapped title')),
                 page(block('Chapter 2 End'))]
        plan = build_plan(pages)
        self.assertEqual(plan['toc_pages'], [1, 2])
        self.assertEqual([e['page'] for e in plan['entries']], [3, 4, 5])

    def test_html_table_contents_and_two_column_source_order(self):
        pages = [page(block('Contents', 50),
                      block('<table><tr><td>Chapter 1 A</td><td>1</td></tr>'
                            '<tr><td>1.1 Alpha</td><td>2</td></tr></table>', 200, right=450),
                      block('Chapter 2 B ..... 3\n2.1 Beta ..... 4', 200, 550, right=950)),
                 page(block('Chapter 1 A')), page(block('1.1 Alpha')),
                 page(block('Chapter 2 B')), page(block('2.1 Beta'))]
        plan = build_plan(pages)
        self.assertEqual([e['page'] for e in plan['entries']], [2, 3, 4, 5])
        self.assertEqual([e['level'] for e in plan['entries']], [1, 2, 1, 2])

    def test_running_headers_excluded_and_missing_parent_does_not_adopt_children(self):
        pages = sample()
        pages[0]['parsing_res_list'].insert(2, block('제9장 없음 ..... 2', 230))
        for i in (1, 3, 5):
            pages[i] = page(block('제1장 기초 개념', 30, label='paragraph_title'))
        plan = build_plan(pages)
        self.assertEqual([e['page'] for e in plan['entries']], [3, 5, 7])
        self.assertEqual([e['level'] for e in plan['entries']], [1, 1, 1])
        self.assertEqual(plan['review'][0]['reason'], 'title_not_found')

    def test_no_contents_fallback_requires_explicit_numbered_headings(self):
        plan = build_plan([page(block('Chapter 1 Basics', label='paragraph_title'),
                                block('1.1 A section', 350, label='paragraph_title'),
                                block('2. This is an ordinary list item', 450))])
        self.assertEqual(plan['mode'], 'headings')
        self.assertEqual([e['title'] for e in plan['entries']], ['Chapter 1 Basics', '1.1 A section'])
        self.assertEqual([e['level'] for e in plan['entries']], [1, 2])
        self.assertEqual(build_plan([page(block('Random body paragraph'))])['entries'], [])

    def test_order_reversal_duplicate_and_malformed_geometry(self):
        pages = [page(block('Contents'), block('Chapter 1 Start ..... 1', 300),
                      block('Chapter 1 Start ..... 1', 350), block('Earlier ..... 2', 400)),
                 page(block('Earlier')), page(block('Chapter 1 Start'))]
        plan = build_plan(pages)
        self.assertEqual([e['reason'] for e in plan['review']], ['duplicate_entry', 'destination_out_of_order'])
        bad = page(block('Chapter 3 Bad', label='title'))
        bad['parsing_res_list'][0]['block_bbox'][0] = float('nan')
        self.assertEqual(build_plan([bad])['entries'], [])

    def test_pdf_round_trip_pixels_text_boxes_crop_rotation_and_unicode(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'source.pdf', Path(td)/'output.pdf'
            for rotation, unit in ((r, u) for r in (0, 90, 180, 270) for u in (1, 2)):
                with self.subTest(rotation=rotation, user_unit=unit):
                    with fitz.open() as doc:
                        p = doc.new_page(width=500, height=700)
                        p.insert_text((80, 200), 'Source text and boxes preserved')
                        p.set_cropbox(fitz.Rect(30, 40, 470, 660))
                        p.set_rotation(rotation)
                        doc.xref_set_key(p.xref, 'UserUnit', str(unit))
                        doc.save(source)
                    plan = {'entries': [{'level': 1, 'title': '제1장 한글 · α 😀', 'page': 1, 'x': .2, 'y': .3}],
                            'review': [], 'version': 'test', 'mode': 'printed_contents', 'toc_pages': []}
                    with fitz.open(source) as doc:
                        report = apply_plan(doc, plan)
                        doc.save(output, garbage=3, deflate=True)
                    verify_bookmarks(source, output, report)
                    self.assertTrue(report['verified'])
                    with fitz.open(source) as before, fitz.open(output) as after:
                        self.assertEqual(before[0].get_pixmap().samples, after[0].get_pixmap().samples)
                        self.assertEqual(before[0].get_text('rawdict'), after[0].get_text('rawdict'))
                        self.assertEqual(before[0].rotation, after[0].rotation)
                        self.assertEqual(before[0].cropbox, after[0].cropbox)
                        self.assertEqual(after.get_toc()[0][1], plan['entries'][0]['title'])

    def test_existing_outline_and_empty_plan_preserved_and_bad_output_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'source.pdf', Path(td)/'output.pdf'
            with fitz.open() as doc:
                doc.new_page()
                doc.set_toc([[1, 'Original', 1, {'kind': fitz.LINK_GOTO, 'to': fitz.Point(20, 40), 'bold': True}],
                             [2, 'Website', -1, {'kind': fitz.LINK_URI, 'uri': 'https://example.org'}]])
                doc.save(source)
            with fitz.open(source) as doc:
                report = apply_plan(doc, build_plan(sample()))
                doc.save(output, garbage=3)
            verify_bookmarks(source, output, report)
            self.assertEqual(report['preserved_existing'], 2)
            self.assertEqual(report['inserted'], 0)
            with fitz.open(output) as doc:
                doc.set_toc([])
                doc.saveIncr()
            with self.assertRaisesRegex(RuntimeError, 'Existing bookmarks changed'):
                verify_bookmarks(source, output, report)

    def test_generated_destinations_must_survive_reopening(self):
        with tempfile.TemporaryDirectory() as td:
            source, output = Path(td)/'source.pdf', Path(td)/'output.pdf'
            with fitz.open() as doc:
                doc.new_page()
                doc.save(source)
            plan = build_plan([page(block('Chapter 1 Title', label='title'))])
            with fitz.open(source) as doc:
                report = apply_plan(doc, plan)
                doc.set_toc([[1, 'Chapter 1 Title', 1, 500]])
                doc.save(output)
            with self.assertRaisesRegex(RuntimeError, 'position mismatch'):
                verify_bookmarks(source, output, report)

    def test_cli_option_is_opt_in_and_completion_identity_is_separate(self):
        parser = build_parser()
        old = run_options(parser.parse_args(['book.pdf']))
        new = run_options(parser.parse_args(['book.pdf', '--bookmarks']))
        self.assertNotIn('bookmarks', old)
        self.assertEqual({k: v for k, v in new.items() if k != 'bookmarks'}, old)
        self.assertTrue(parser.parse_args(['book.pdf', '--toc', '--toc-all']).toc_all)

    def test_title_matching_keeps_meaningful_symbols(self):
        plan = build_plan([page(block('Contents'), block('C++ ..... 1', 300)), page(block('C#'))])
        self.assertEqual(plan['entries'], [])
        self.assertEqual(plan['review'][0]['reason'], 'title_not_found')

    def test_cached_pipeline_publishes_verified_bookmarks_without_ocr(self):
        from ocr_to_searchable_pdf import process_pdf, MD_SEP
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source, out = root/'book.pdf', root/'result'
            out.mkdir()
            pages = sample()
            with fitz.open() as doc:
                for i in range(len(pages)):
                    p = doc.new_page(width=500, height=700)
                    p.insert_text((40, 50), f'Existing searchable source page {i+1}')
                doc.save(source)
            identity = source_identity(source)
            artifact = lambda name: document_artifact(out, 'book', name)
            atomic_json(artifact('book_pruned.json'), pages)
            atomic_json(artifact('book_cache_meta.json'), identity)
            artifact('book.md').write_text(MD_SEP.join('text' for _ in pages))
            args = build_parser().parse_args([str(source), '--bookmarks', '--no-boundary-repair', '--cpu-workers', '0'])
            process_pdf(args, source, out, set(), True)
            report = json.loads(find_artifact(out, 'book_auto_report.json').read_text())
            self.assertEqual(report['bookmarks']['inserted'], 3)
            self.assertTrue(report['bookmarks']['verified'])
            self.assertEqual(report['source_identity'], identity)
            self.assertFalse(report['validation_failed'])
            self.assertTrue((out/'book_searchable.pdf').exists())
            self.assertFalse(find_artifact(out, 'book_searchable.partial.pdf').exists())
            self.assertEqual(json.loads(artifact('book_pruned.json').read_text()), pages)
            before_pdf = (out/'book_searchable.pdf').read_bytes()
            before_report = find_artifact(out, 'book_auto_report.json').read_bytes()
            def reject_bookmarks(source, output, report):
                raise RuntimeError('injected bookmark verification failure')
            with self.assertRaisesRegex(RuntimeError, 'injected bookmark'):
                process_pdf(args, source, out, set(), True, bookmark_verifier=reject_bookmarks)
            self.assertEqual((out/'book_searchable.pdf').read_bytes(), before_pdf)
            self.assertEqual(find_artifact(out, 'book_auto_report.json').read_bytes(), before_report)
            self.assertTrue(find_artifact(out, 'book_searchable.partial.pdf').exists())


if __name__ == '__main__':
    unittest.main()

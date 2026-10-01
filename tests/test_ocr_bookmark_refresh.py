"""Real-layout regressions and bookmark-only preservation/publication guards."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'compose'))
from ocr_bookmarks import build_plan, apply_plan, verify_bookmarks, load_review, layout_hash
from ocr_bookmark_refresh import refresh, verify_content, file_hash, publish_verified
from ocr_storage import atomic_json
from ocr_workflow import document_artifact, find_artifact
import ocr_bookmark_refresh as bookmark_refresh
from test_ocr_bookmarks import page, block


class BookmarkLayoutTests(unittest.TestCase):
    def test_detached_folios_missing_folios_chapters_and_exercises(self):
        pages = [page(block('차례', 80), block('CHAPTER 01 기초', 200, label='title'),
                     block('1.1 정의\n5\n상세 설명\n6\n연습문제\n7', 300)),
                 page(block('CHAPTER'), block('기초', 350, label='title'),
                      block('1.1 정의', 500), block('학습목표', 700)),
                 page(block('1.1 정의', label='title'), block('5', 1320, label='number')),
                 page(block('상세 설명', label='title'), block('6', 1320, label='number')),
                 page(block('연 / 습 / 문 / 제', label='title'), block('7', 1320, label='number'))]
        plan = build_plan(pages)
        self.assertEqual(plan['review'], [])
        self.assertEqual([e['page'] for e in plan['entries']], [2, 3, 4, 5])
        self.assertEqual([e['level'] for e in plan['entries']], [1, 2, 3, 2])

    def test_intro_cannot_steal_split_or_decorative_number_heading(self):
        pages = [page(block('Contents'), block('CHAPTER 3 이산 신호', 300), block('3.1 이산 신호란? 84', 400)),
                 page(block('CHAPTER'), block('이산 신호', 350, label='title'),
                      block('3.1 이산 신호란?', 500), block('단/원/개/요', 700)),
                 page(block('3.1', 300, right=150, label='title'),
                      block('이산 신호란?', 300, x=180, label='title'), block('84', 1320, label='number'))]
        plan = build_plan(pages)
        self.assertEqual([e['page'] for e in plan['entries']], [2, 3])
        self.assertEqual(plan['review'], [])

    def test_ocr_similarity_requires_independent_page_and_heading(self):
        pages = [page(block('Contents'), block('8.4 단방향 z-변환 299', 300)),
                 page(block('8.4 단방향 2-변환', label='title'), block('299', 1320, label='number'))]
        self.assertEqual(len(build_plan(pages)['entries']), 1)
        pages[1]['parsing_res_list'].pop()
        self.assertEqual(build_plan(pages)['entries'], [])
        pages = [page(block('Contents'), block('Lab 1.1 1부터 합 구하기 27', 300)),
                 page(block('Lab 1.1 2부터 합 구하기', label='title'), block('27', 1320, label='number'))]
        self.assertEqual(build_plan(pages)['entries'], [])

    def test_source_gap_does_not_create_false_bookmark(self):
        pages = [page(block('Contents'), block('Lab 2.1 행렬 표현하기 53', 300)),
                 page(block('52', 1320, label='number')),
                 page(block('Lab 2.1 행렬 표현하기', label='title'), block('55', 1320, label='number'))]
        plan = build_plan(pages)
        self.assertEqual(plan['entries'], [])
        self.assertEqual(plan['review'][0]['reason'], 'source_page_missing')

    def test_chapter_intro_without_chapter_text_and_one_character_title(self):
        pages = [page(block('Contents'), block('CHAPTER 04 큐', 300), block('4.1 큐란? 120', 400)),
                 page(block('큐', 250, label='title'), block('4.1 큐란?', 400),
                      block('4.2 큐의 연산', 500), block('학습목표', 700)),
                 page(block('4 큐', 250, label='title'), block('4.1 큐란?', 450, label='title'), block('120', 1320, label='number'))]
        self.assertEqual([e['page'] for e in build_plan(pages)['entries']], [2, 3])

    def test_review_matches_source_layout_and_exact_entry(self):
        pages = [page(block('Contents'), block('Lab 1.1 부터 합 구하기 27', 300)),
                 page(block('Lab 1.1 1부터 합 구하기', label='title'), block('27', 1320, label='number'))]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'review.json'
            data = {'source_sha256': 'source', 'layout_sha256': layout_hash(pages),
                    'corrections': [{'toc_page': 1, 'original_title': 'Lab 1.1 부터 합 구하기',
                                     'title': 'Lab 1.1 1부터 합 구하기', 'reason': 'Source image inspected'}]}
            atomic_json(path, data)
            corrections = load_review(path, {'sha256': 'source'}, pages)
            self.assertEqual(len(build_plan(pages, corrections)['entries']), 1)
            with self.assertRaisesRegex(ValueError, 'source PDF'):
                load_review(path, {'sha256': 'different'}, pages)
            with self.assertRaisesRegex(ValueError, 'exactly one'):
                build_plan([page()], corrections)
            data['layout_sha256'] = 'changed'
            atomic_json(path, data)
            with self.assertRaisesRegex(ValueError, 'OCR layout'):
                load_review(path, {'sha256': 'source'}, pages)

    def test_decorative_chapter_digit_is_not_a_printed_folio(self):
        pages = [page(block('Contents'), block('1.1 Definition 16', 300)),
                 page(block('1', 140, label='title'), block('1.1 Definition', 400, label='title'))]
        self.assertEqual([e['page'] for e in build_plan(pages)['entries']], [2])

    def test_reviewed_display_title_can_match_unchanged_ocr_spelling(self):
        pages = [page(block('Contents'), block('CHAPTER 8 2-변환과 디지털 시스템', 300),
                      block('8.1 z-변환 276', 400)),
                 page(block('CHAPTER'), block('2-변환과 디지털 시스템', 350, label='title'),
                      block('8.1 z-변환', 500), block('단/원/개/요', 700)),
                 page(block('8.1 z-변환', label='title'), block('276', 1320, label='number'))]
        corrections = [{'toc_page': 1, 'original_title': 'CHAPTER 8 2-변환과 디지털 시스템',
                        'title': 'CHAPTER 8 z-변환과 디지털 시스템', 'reason': 'Source image checked'}]
        plan = build_plan(pages, corrections)
        self.assertEqual(plan['entries'][0]['page'], 2)
        self.assertIn('z-변환', plan['entries'][0]['title'])
        self.assertEqual(pages[1]['parsing_res_list'][1]['block_content'], '2-변환과 디지털 시스템')


class BookmarkRefreshTests(unittest.TestCase):
    def fixture(self, root):
        source, out = root/'book.pdf', root/'out'
        out.mkdir()
        pdf = out/'book_auto_searchable.pdf'
        pages = [page(block('Contents'), block('Chapter 1 One ..... 1', 300), block('1.1 Two ..... 2', 400)),
                 page(block('Chapter 1 One', label='title'), block('1', 1320, label='number')),
                 page(block('1.1 Two', label='title'), block('2', 1320, label='number'))]
        with fitz.open() as doc:
            for i in range(3):
                p = doc.new_page(width=300, height=400)
                p.insert_text((30, 50), f'Original OCR text {i}', render_mode=3)
                p.draw_rect(fitz.Rect(30, 60, 100, 90), color=(.2, .5, .9))
            doc.save(source)
        old = build_plan(pages)
        old['entries'] = old['entries'][:1]
        with fitz.open(source) as doc:
            old = apply_plan(doc, old)
            doc.save(pdf)
        verify_bookmarks(source, pdf, old)
        artifact = lambda name: document_artifact(out, 'book', name)
        layout = artifact('book_auto_layout_'+layout_hash(pages)[:20]+'.json')
        atomic_json(layout, pages)
        atomic_json(artifact('book_pruned.json'), pages)
        report = {'status': 'completed', 'source': str(source), 'output': str(pdf),
                  'source_identity': {'sha256': file_hash(source)}, 'layout_cache': str(layout), 'bookmarks': old,
                  'pages': [{'page': i+1, 'warnings': []} for i in range(3)], 'review_pages': []}
        atomic_json(artifact('book_auto_report.json'), report)
        return source, pdf, pages

    def test_refresh_replaces_only_matching_generated_bookmarks_preserves_ocr_and_backup(self):
        with tempfile.TemporaryDirectory() as td:
            source, pdf, pages = self.fixture(Path(td))
            original = pdf.read_bytes()
            report = refresh(pdf)
            self.assertEqual(report['bookmarks']['inserted'], 2)
            self.assertTrue(report['bookmark_refresh']['preservation']['all_pixels_equal'])
            self.assertEqual(Path(report['bookmark_refresh']['backup']).read_bytes(), original)
            self.assertEqual(json.loads(find_artifact(pdf.parent, 'book_pruned.json').read_text()), pages)
            self.assertEqual(report['source_identity']['sha256'], file_hash(source))
            # Rerunning does not accumulate duplicate entries.
            self.assertEqual(refresh(pdf)['bookmarks']['inserted'], 2)

    def test_failed_verification_does_not_publish_pdf_or_report(self):
        with tempfile.TemporaryDirectory() as td:
            _, pdf, _ = self.fixture(Path(td))
            original, report = pdf.read_bytes(), find_artifact(pdf.parent, 'book_auto_report.json').read_bytes()
            def fail(source, candidate):
                raise RuntimeError('injected preservation failure')
            with self.assertRaisesRegex(RuntimeError, 'injected preservation'):
                refresh(pdf, content_verifier=fail)
            self.assertEqual(pdf.read_bytes(), original)
            self.assertEqual(find_artifact(pdf.parent, 'book_auto_report.json').read_bytes(), report)

    def test_saved_verification_retry_rejects_changed_candidate_and_publishes_exact_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            _, pdf, _ = self.fixture(Path(td))
            updated = refresh(pdf)
            history = Path(updated['bookmark_refresh']['backup']).parent
            candidate = history/'candidate.pdf'
            verified_bytes = pdf.read_bytes()
            candidate.write_bytes(verified_bytes)
            # Reproduce a failed final replacement, preserving the verified history.
            shutil.copyfile(history/'before.pdf', pdf)
            old_report = json.loads((history/'before_report.json').read_text(encoding='utf-8'))
            atomic_json(document_artifact(pdf.parent, 'book', 'book_auto_report.json'), old_report)
            candidate.write_bytes(verified_bytes+b'changed')
            with self.assertRaisesRegex(ValueError, 'candidate changed'):
                publish_verified(history)
            self.assertEqual(file_hash(pdf), updated['bookmark_refresh']['before_sha256'])
            candidate.write_bytes(verified_bytes)
            result = publish_verified(history)
            self.assertEqual(pdf.read_bytes(), verified_bytes)
            self.assertEqual(result, updated)

    def reviewed_fixture(self, root):
        source, pdf, pages = self.fixture(root)
        review = root/'review.json'
        atomic_json(review, {'source_sha256': file_hash(source), 'layout_sha256': layout_hash(pages),
            'corrections': [{'toc_page': 1, 'original_title': 'Chapter 1 One',
                             'title': 'Chapter 1 Reviewed One', 'reason': 'Source image inspected'}]})
        state = document_artifact(pdf.parent, 'book', 'book_auto_status.json')
        atomic_json(state, {'status': 'completed', 'options': {}})
        return pdf, review, state

    def test_review_identity_is_preserved_when_publication_resumes(self):
        with tempfile.TemporaryDirectory() as td:
            pdf, review, state = self.reviewed_fixture(Path(td))
            review_bytes, applied_hash = review.read_bytes(), file_hash(review)
            with patch.object(bookmark_refresh, 'write_summary', side_effect=OSError('summary failed')):
                with self.assertRaisesRegex(OSError, 'summary failed'):
                    refresh(pdf, review)
            history = next((pdf.parent/'.ocr'/'bookmark_history'/'book').iterdir())
            updated = json.loads((history/'verified_report.json').read_text(encoding='utf-8'))
            self.assertEqual(updated['bookmark_refresh']['review_sha256'], applied_hash)
            self.assertEqual(updated['bookmarks']['entries'][0]['title'], 'Chapter 1 Reviewed One')
            report = find_artifact(pdf.parent, 'book_auto_report.json')
            before_pdf, before_report, before_state = pdf.read_bytes(), report.read_bytes(), state.read_bytes()
            changed = json.loads(review_bytes)
            changed['corrections'][0]['title'] = 'Chapter 1 Changed Again'
            atomic_json(review, changed)
            with self.assertRaisesRegex(ValueError, 'review changed'):
                publish_verified(history)
            self.assertEqual(pdf.read_bytes(), before_pdf)
            self.assertEqual(report.read_bytes(), before_report)
            self.assertEqual(state.read_bytes(), before_state)
            review.write_bytes(review_bytes)
            for _ in range(2):
                self.assertEqual(publish_verified(history), updated)
                self.assertEqual(pdf.read_bytes(), before_pdf)
                self.assertEqual(json.loads(state.read_text())['options']['bookmark_review'], applied_hash)

    def test_review_change_during_verification_does_not_publish(self):
        with tempfile.TemporaryDirectory() as td:
            pdf, review, state = self.reviewed_fixture(Path(td))
            report = find_artifact(pdf.parent, 'book_auto_report.json')
            before_pdf, before_report, before_state = pdf.read_bytes(), report.read_bytes(), state.read_bytes()
            def verify_then_change(source, candidate):
                result = verify_content(source, candidate)
                changed = json.loads(review.read_text())
                changed['corrections'][0]['title'] = 'Chapter 1 Changed Again'
                atomic_json(review, changed)
                return result
            with self.assertRaisesRegex(ValueError, 'review changed'):
                refresh(pdf, review, content_verifier=verify_then_change)
            self.assertEqual(pdf.read_bytes(), before_pdf)
            self.assertEqual(report.read_bytes(), before_report)
            self.assertEqual(state.read_bytes(), before_state)

    def test_legacy_history_resumes_without_claiming_a_review_hash(self):
        for with_review in (False, True):
            for before_replacement in (False, True):
                with self.subTest(review=with_review, before_replacement=before_replacement), tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    if with_review:
                        pdf, review, state = self.reviewed_fixture(root)
                    else:
                        _, pdf, _ = self.fixture(root)
                        review = None
                        state = document_artifact(pdf.parent, 'book', 'book_auto_status.json')
                    atomic_json(state, {'status': 'completed', 'options': {'bookmark_review': 'unverified old hash'}})
                    with patch.object(bookmark_refresh, 'write_summary', side_effect=OSError('summary failed')):
                        with self.assertRaises(OSError):
                            refresh(pdf, review)
                    history = next((pdf.parent/'.ocr'/'bookmark_history'/'book').iterdir())
                    updated = json.loads((history/'verified_report.json').read_text(encoding='utf-8'))
                    del updated['bookmark_refresh']['review_sha256']
                    atomic_json(history/'verified_report.json', updated)
                    report = find_artifact(pdf.parent, 'book_auto_report.json')
                    verified_bytes = pdf.read_bytes()
                    if before_replacement:
                        (history/'candidate.pdf').write_bytes(verified_bytes)
                        shutil.copyfile(history/'before.pdf', pdf)
                        old = json.loads((history/'before_report.json').read_text(encoding='utf-8'))
                        atomic_json(report, old)
                    else:
                        atomic_json(report, updated)
                    if review:
                        changed = json.loads(review.read_text())
                        changed['corrections'][0]['title'] = 'Chapter 1 Later Review'
                        atomic_json(review, changed)
                    for _ in range(2):
                        self.assertEqual(publish_verified(history), updated)
                        self.assertEqual(pdf.read_bytes(), verified_bytes)
                        self.assertEqual(json.loads(report.read_text()), updated)
                        self.assertNotIn('bookmark_review', json.loads(state.read_text())['options'])

    def test_publication_retry_finishes_after_pdf_report_or_summary_was_published(self):
        for failed_step in ('report', 'summary', 'status'):
            with self.subTest(failed_step=failed_step), tempfile.TemporaryDirectory() as td:
                _, pdf, _ = self.fixture(Path(td))
                before = pdf.read_bytes()
                state = document_artifact(pdf.parent, 'book', 'book_auto_status.json')
                atomic_json(state, {'status': 'completed', 'options': {}})

                def write(path, value):
                    if getattr(path, 'key', None) == f'book_auto_{failed_step}.json':
                        raise OSError('injected publication failure')
                    return atomic_json(path, value)

                summary = bookmark_refresh.write_summary
                if failed_step == 'summary':
                    def summary(*args):
                        raise OSError('injected publication failure')
                with patch.object(bookmark_refresh, 'atomic_json', side_effect=write), \
                        patch.object(bookmark_refresh, 'write_summary', side_effect=summary):
                    with self.assertRaisesRegex(OSError, 'publication failure'):
                        refresh(pdf)
                history = next((pdf.parent/'.ocr'/'bookmark_history'/'book').iterdir())
                updated = json.loads((history/'verified_report.json').read_text(encoding='utf-8'))
                verified_bytes = pdf.read_bytes()
                self.assertNotEqual(before, verified_bytes)
                self.assertFalse((history/'candidate.pdf').exists())
                self.assertEqual((history/'before.pdf').read_bytes(), before)

                self.assertEqual(publish_verified(history), updated)
                self.assertEqual(pdf.read_bytes(), verified_bytes)
                self.assertEqual(json.loads(find_artifact(pdf.parent, 'book_auto_report.json').read_text()), updated)
                self.assertIn('OCR 처리 결과', find_artifact(pdf.parent, 'book_auto_report.md').read_text())
                self.assertEqual(json.loads(state.read_text())['options']['bookmarks'], updated['bookmarks']['version'])
                self.assertEqual(publish_verified(history), updated)

    def test_publication_retry_rejects_external_pdf_changes_after_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            _, pdf, _ = self.fixture(Path(td))
            def write(path, value):
                if getattr(path, 'key', None) == 'book_auto_report.json':
                    raise OSError('injected report failure')
                return atomic_json(path, value)
            with patch.object(bookmark_refresh, 'atomic_json', side_effect=write):
                with self.assertRaisesRegex(OSError, 'report failure'):
                    refresh(pdf)
            history = next((pdf.parent/'.ocr'/'bookmark_history'/'book').iterdir())
            pdf.write_bytes(pdf.read_bytes()+b'external change')
            modified = pdf.read_bytes()
            with self.assertRaisesRegex(ValueError, 'candidate is missing|Current PDF'):
                publish_verified(history)
            self.assertEqual(pdf.read_bytes(), modified)

    def test_user_edits_to_generated_outline_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            _, pdf, _ = self.fixture(Path(td))
            with fitz.open(pdf) as doc:
                doc.set_toc([[1, 'My custom bookmark', 3]])
                doc.saveIncr()
            original = pdf.read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                refresh(pdf)
            self.assertEqual(pdf.read_bytes(), original)

    def test_non_outline_changes_are_rejected_even_when_pixels_are_identical(self):
        with tempfile.TemporaryDirectory() as td:
            _, pdf, _ = self.fixture(Path(td))
            candidate = Path(td)/'bad.pdf'
            shutil.copyfile(pdf, candidate)
            with fitz.open(candidate) as doc:
                doc[0].insert_text((30, 300), 'Unwanted OCR change', render_mode=3)
                doc.saveIncr()
            with self.assertRaisesRegex(RuntimeError, 'non-outline object'):
                verify_content(pdf, candidate)


if __name__ == '__main__':
    unittest.main()

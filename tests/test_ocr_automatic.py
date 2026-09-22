import base64
import contextlib
import io
import json
from collections import Counter
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import pymupdf as fitz
from pypdf import PdfReader
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'compose'))
import ocr_to_searchable_pdf as ocr
import ocr_workflow


class AutomaticTests(unittest.TestCase):
    def test_boundary_prefetch_cli_default_and_serial_escape_preserve_report(self):
        import ocr_boundary
        for extra, expected in (((), True), (('--no-prefetch',), False)):
            with self.subTest(prefetch=expected), tempfile.TemporaryDirectory() as td:
                src = Path(td) / 'book.pdf'
                self.scanned(src, 1)
                call, line_call, _, _ = self.responder()
                with patch.object(ocr_boundary, 'repair', wraps=ocr_boundary.repair) as repair:
                    self.run_cli(src, call, line_call, '--cpu-workers', '0', *extra)
                self.assertEqual(repair.call_count, 1)
                self.assertEqual(repair.call_args.kwargs['prefetch'], expected)
                report = json.loads(ocr_workflow.find_artifact(src.parent / 'ocr_output',
                                    'book_auto_report.json').read_text())
                self.assertEqual(report['boundary_feed_stats']['requests'], 0)
                self.assertEqual(report['boundary_feed_stats']['errors'], 0)
                self.assertFalse(report['validation_failed'])

    def test_transient_api_errors_retry_but_bad_requests_do_not(self):
        good=Mock()
        good.json.return_value={'result':{'ok':True}}
        with patch.object(ocr.requests,'post',side_effect=[ocr.requests.Timeout(),good]) as call, patch.object(ocr.time,'sleep'):
            self.assertEqual(ocr._call_api(b'image',1),{'ok':True})
            self.assertEqual(call.call_count,2)
        bad=Mock()
        response=Mock(status_code=400)
        bad.raise_for_status.side_effect=ocr.requests.HTTPError(response=response)
        with patch.object(ocr.requests,'post',return_value=bad) as call:
            with self.assertRaises(ocr.requests.HTTPError):
                ocr._call_api(b'image',1)
            self.assertEqual(call.call_count,1)

    def test_healthy_service_does_not_start_docker(self):
        service=ocr_workflow.Service(ocr.BASE,Path(__file__).parent)
        with patch.object(service,'healthy',return_value=True), patch.object(ocr_workflow.subprocess,'run') as run:
            service.ensure()
            service.ensure()
            run.assert_not_called()

    def test_engine_probe_timeout_keeps_waiting_for_service_startup(self):
        service = ocr_workflow.Service(ocr.BASE, Path(__file__).parent)
        with patch.object(service, 'healthy', side_effect=[False, False, True]), \
             patch.object(ocr_workflow.shutil, 'which', return_value='docker'), \
             patch.object(ocr_workflow.Path, 'exists', return_value=False), \
             patch.object(ocr_workflow.time, 'sleep') as sleep, \
             patch.object(ocr_workflow.subprocess, 'run',
                          side_effect=ocr_workflow.subprocess.TimeoutExpired('docker info', 20)) as run:
            service.ensure()
        self.assertTrue(service.ready)
        run.assert_called_once()
        sleep.assert_called_once_with(10)

    def test_recover_restarts_only_unresponsive_pipeline_api(self):
        service=ocr_workflow.Service(ocr.BASE,Path(__file__).parent)
        with patch.object(service,'healthy',side_effect=[False,True]), \
             patch.object(ocr_workflow.shutil,'which',return_value='docker'), \
             patch.object(ocr_workflow.subprocess,'run') as run:
            service.recover()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0],
                         ['docker','compose','restart','paddleocr-vl-api'])
        self.assertTrue(service.ready)

    def test_ensure_recovers_running_but_unresponsive_pipeline(self):
        service=ocr_workflow.Service(ocr.BASE,Path(__file__).parent)
        probe=Mock(returncode=0)
        running=Mock(returncode=0,stdout='paddleocr-vlm-server\npaddleocr-vl-api\n')
        with patch.object(service,'healthy',return_value=False), \
             patch.object(service,'recover') as recover, \
             patch.object(ocr_workflow.shutil,'which',return_value='docker'), \
             patch.object(ocr_workflow.Path,'exists',return_value=False), \
             patch.object(ocr_workflow.subprocess,'run',side_effect=[probe,running]):
            service.ensure()
        recover.assert_called_once_with()

    def test_custom_service_restarts_its_own_container(self):
        service=ocr_workflow.Service('http://localhost:8081',Path(__file__).parent,
                                     compose_service='paddleocr-line-api')
        with patch.object(service,'healthy',side_effect=[False,True]), \
             patch.object(ocr_workflow.shutil,'which',return_value='docker'), \
             patch.object(ocr_workflow.subprocess,'run') as run:
            service.recover()
        self.assertEqual(run.call_args.args[0],
                         ['docker','compose','restart','paddleocr-line-api'])

    def test_same_job_lock_rejects_another_writer(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'job.lock'
            with ocr_workflow.job_lock(path):
                with self.assertRaisesRegex(RuntimeError,'already being processed'):
                    with ocr_workflow.job_lock(path):
                        pass
            with ocr_workflow.job_lock(path):
                pass

    def test_existing_text_is_not_inserted_twice(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.pdf'; out=Path(td)/'out.pdf'
            with fitz.open() as doc:
                p=doc.new_page()
                p.insert_text((20,30),'Already searchable')
                doc.save(src)
            pr={'width':595,'height':842,'parsing_res_list':[
                {'block_content':'Already searchable','block_bbox':[20,20,200,40]}]}
            report=ocr.overlay(src,[pr],out,automatic=True)
            ocr_workflow.verify(src,out,report)
            with fitz.open(out) as doc:
                self.assertEqual(doc[0].get_text().count('Already searchable'),1)
            self.assertFalse(report['validation_failed'])
            self.assertIn('existing_text_preserved',report['pages'][0]['warnings'])

    def scanned(self, path, pages):
        with fitz.open() as template:
            p = template.new_page(width=240, height=120)
            p.insert_text((20,40),'Alpha beta gamma',fontsize=10)
            p.insert_text((20,60),'Delta epsilon zeta',fontsize=10)
            png = p.get_pixmap(matrix=fitz.Matrix(2,2)).tobytes('png')
        with fitz.open() as doc:
            for _ in range(pages):
                p = doc.new_page(width=240,height=120)
                p.insert_image(p.rect,stream=png)
            doc.save(path)

    def responder(self):
        crops = []
        batches = []
        def row(text):
            return {'markdown':{'text':text}, 'prunedResult':{
                'width':240,'height':120,'parsing_res_list':[
                    {'block_content':text,'block_bbox':[15,25,220,65]}]}}
        def call(data, file_type, *args, **kwargs):
            if file_type == 0:
                # HTTP stubs may run in parallel with owner-thread PDF work.
                # Do not introduce multi-threaded PyMuPDF in the test server.
                count = len(PdfReader(io.BytesIO(data)).pages)
                batches.append(count)
                return {'layoutParsingResults':[row('Alpha beta gamma\nDelta epsilon zeta') for _ in range(count)]}
            raise AssertionError('Unexpected image request to the full document model')
        def line_call(images, timeout=120, lang=None):
            assert lang is None, 'Latin-only blocks must stay on the default recognizer'
            values = ['Alpha beta gamma','Delta epsilon zeta']
            start = len(crops)
            crops.extend(images)
            return [values[(start+i)%2] for i in range(len(images))]
        return call, line_call, crops, batches

    def run_cli(self, src, api, line_api, *extra):
        with patch('sys.argv',['ocr',str(src),*extra]), patch.object(ocr,'_call_api',side_effect=api), \
             patch.object(ocr,'recognize_lines',side_effect=line_api), \
             patch.object(ocr_workflow.Service,'ensure'), patch.object(ocr_workflow.Service,'recover'), \
             patch.object(ocr.requests,'get') as health, contextlib.redirect_stdout(io.StringIO()):
            health.return_value.json.return_value = {'errorMsg':'Healthy'}
            ocr.main()

    def test_300_pages_default_pipeline_and_cache_only_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.scanned(src,300)
            call,line_call,crops,batches = self.responder()
            self.run_cli(src,call,line_call)
            self.assertEqual(batches,[10]*30)
            self.assertEqual(len(crops),2)
            out = src.parent/'ocr_output'
            report = json.loads(ocr_workflow.find_artifact(out, 'book_auto_report.json').read_text(encoding='utf-8'))
            self.assertEqual(len(report['pages']),300)
            self.assertFalse(report['validation_failed'])
            self.assertEqual(report['status'],'completed')
            with fitz.open(out/'book_auto_searchable.pdf') as doc:
                self.assertEqual(len(doc),300)
            self.run_cli(src,lambda *a,**kw: self.fail('Unexpected API call on cached repeat'),
                         lambda *a,**kw: self.fail('Unexpected line OCR call on cached repeat'))
            repeated = json.loads(ocr_workflow.find_artifact(out, 'book_auto_report.json').read_text(encoding='utf-8'))
            self.assertEqual(repeated['line_cache_stats'],
                             {'rendered_crops': 0, 'early_cache_hits': 600})

    def test_a_font_swapping_one_glyph_codepoint_does_not_fail_the_book(self):
        """A wave dash read back as a fullwidth tilde discarded a 216-page book."""
        with tempfile.TemporaryDirectory() as td:
            src, out = Path(td)/'in.pdf', Path(td)/'out.pdf'
            with fitz.open() as doc:
                doc.new_page(width=200, height=80)
                doc.save(src)
            with fitz.open(src) as doc:
                page = doc[0]
                page.insert_font(fontname='malgun', fontfile=r'C:\Windows\Fonts\malgun.ttf')
                page.insert_text((10, 40), '3～5', fontname='malgun', render_mode=3)
                doc.save(out)

            report = {'pages': [{'page': 1, 'warnings': [],
                                 'expected_characters': Counter('3〜5')}]}
            ocr_workflow.verify(src, out, report, debug=True)
            page_report = report['pages'][0]
            self.assertNotIn('extracted_text_mismatch', page_report['warnings'])
            self.assertIn('glyph_equivalent_substituted', page_report['warnings'])
            self.assertFalse(report['validation_failed'])

            # A character that really went missing must still be fatal.
            report = {'pages': [{'page': 1, 'warnings': [],
                                 'expected_characters': Counter('3〜58')}]}
            ocr_workflow.verify(src, out, report, debug=True)
            self.assertIn('extracted_text_mismatch', report['pages'][0]['warnings'])
            self.assertEqual(report['pages'][0]['missing'], {'8': 1})
            self.assertTrue(report['validation_failed'])

    def test_batch_forwards_flags_and_tracks_debug_runs_separately(self):
        """--debug-lines on a folder used to skip every already-processed book."""
        import ocr_batch
        self.assertEqual(ocr_batch.status_tag([]), '_auto')
        self.assertEqual(ocr_batch.status_tag(['--debug-lines']), '_auto_debug')
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.scanned(src, 1)
            out = src.parent/'ocr_output'
            out.mkdir()
            ocr.atomic_json(out/'book_cache_meta.json', ocr.source_identity(src))
            final = out/'book_auto_searchable.pdf'
            final.write_bytes(b'%PDF-1.4')
            ocr.atomic_json(out/'book_auto_status.json',
                            {'status': 'completed', 'output': str(final),
                             'source_identity': ocr.source_identity(src)})
            # The normal run is finished, so a normal batch skips it...
            self.assertTrue(ocr_batch.finished(src, out, '_auto'))
            # ...but a debug run has not happened and must not be skipped.
            self.assertFalse(ocr_batch.finished(src, out, '_auto_debug'))

    def test_debug_run_keeps_its_own_status_and_report(self):
        """A diagnostic run must not overwrite the finished PDF's status."""
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.scanned(src,2)
            out = src.parent/'ocr_output'
            call,line_call,crops,batches = self.responder()
            self.run_cli(src,call,line_call)
            real = json.loads(ocr_workflow.find_artifact(out, 'book_auto_status.json').read_text(encoding='utf-8'))
            self.assertTrue(real['output'].endswith('book_auto_searchable.pdf'))

            call,line_call,crops,batches = self.responder()
            self.run_cli(src,call,line_call,'--debug-lines')
            self.assertTrue(ocr_workflow.artifact_path(out, 'book_auto_debug.pdf').exists())
            self.assertTrue(ocr_workflow.find_artifact(out, 'book_auto_debug_status.json').exists())
            self.assertTrue(ocr_workflow.find_artifact(out, 'book_auto_debug_report.json').exists())
            self.assertEqual(json.loads(ocr_workflow.find_artifact(out, 'book_auto_status.json').read_text(encoding='utf-8')), real)

    def test_crop_failure_resumes_without_losing_previous_final_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.scanned(src,1)
            out = src.parent/'ocr_output'
            out.mkdir()
            final = out/'book_auto_searchable.pdf'
            final.write_bytes(b'previous final stays intact')
            call,line_call,crops,batches = self.responder()
            def interrupted(images,timeout=120):
                raise RuntimeError('interrupted')
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                self.run_cli(src,call,interrupted)
            self.assertEqual(final.read_bytes(),b'previous final stays intact')
            self.assertEqual(json.loads(ocr_workflow.find_artifact(out, 'book_auto_status.json').read_text())['status'],'failed')
            self.run_cli(src,call,line_call)
            self.assertEqual(batches,[1])
            self.assertEqual(len(crops),2)
            self.assertTrue(final.read_bytes().startswith(b'%PDF'))

    def test_invalid_checkpoint_cannot_shift_pages(self):
        with tempfile.TemporaryDirectory() as td:
            cp=Path(td)/'partial.json'
            ocr.atomic_json(cp,{'identity':{},'batch':10,'pages':[{}],'markdown':['one']})
            with patch.object(ocr,'split_pdf',return_value=iter([(0,b'x',300)])):
                with self.assertRaisesRegex(RuntimeError,'Incomplete batch'):
                    ocr.run_ocr(Path('unused'),10,cp,{})

    def test_validation_detects_extra_and_missing_text(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.pdf'; out=Path(td)/'out.pdf'
            self.scanned(src,1)
            with fitz.open(src) as doc:
                doc[0].insert_text((20,90),'extra',render_mode=3)
                doc.save(out)
            report={'pages':[{'page':1,'warnings':[],'expected_characters':{'a':1}}]}
            ocr_workflow.verify(src,out,report)
            self.assertTrue(report['validation_failed'])


    def test_completion_is_bound_to_final_source_not_mutable_cache(self):
        import ocr_batch
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'
            self.scanned(src, 1)
            out = Path(td)/'ocr_output'; out.mkdir()
            final = out/'book_auto_searchable.pdf'; final.write_bytes(src.read_bytes())
            args = ocr.build_parser().parse_args([str(src)])
            ocr.atomic_json(out/'book_auto_status.json',
                            ocr.completed_status(args, ocr.source_identity(src), final))
            self.assertTrue(ocr_batch.finished(src, out, options=ocr.run_options(args)))
            self.scanned(src, 2)
            ocr.atomic_json(out/'book_cache_meta.json', ocr.source_identity(src))
            self.assertFalse(ocr_batch.finished(src, out))

    def test_batch_custom_output_and_fast_success_then_skip(self):
        import ocr_batch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); src = root/'book.pdf'; out = root/'custom'
            self.scanned(src, 1)
            api, lines, _, _ = self.responder()
            self.run_cli(src, api, lines, '--out', str(out))
            logs = out/'batch_logs'; logs.mkdir()
            result = ocr_batch.run_one(src, out, logs, ['--fast'], '_fast')
            self.assertEqual((result['exit_code'], result['status']), (0, 'completed'))
            self.assertTrue((out/'book_searchable.pdf').exists())
            self.assertFalse((root/'ocr_output').exists())
            args = ocr.build_parser().parse_args([str(src), '--fast'])
            self.assertTrue(ocr_batch.finished(src, out, '_fast', ocr.run_options(args)))
            args.toc = True
            self.assertFalse(ocr_batch.finished(src, out, '_fast', ocr.run_options(args)))

    def test_nonzero_exit_cannot_reuse_old_success(self):
        import ocr_batch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); src = root/'book.pdf'; self.scanned(src, 1)
            ocr.atomic_json(root/'book_auto_status.json', {'status': 'completed'})
            with patch.object(ocr_batch.subprocess, 'call', return_value=2):
                result = ocr_batch.run_one(src, root, root, [])
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['exit_code'], 2)

    def test_recursive_batch_excludes_generated_and_custom_outputs(self):
        import ocr_batch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            names = ['book.pdf', 'sub/book2.pdf', 'ocr_output/book_auto_searchable.pdf',
                     'sub/ocr_output/page.pdf', 'custom/copied.pdf', 'book_auto_debug.pdf',
                     'book_auto_searchable.partial.pdf']
            for name in names:
                path = root/name; path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'pdf')
            actual = ocr_batch.discover_sources(root, True, root/'custom')
            self.assertEqual({p.relative_to(root).as_posix() for p in actual},
                             {'book.pdf', 'sub/book2.pdf'})

    def test_all_modes_have_distinct_status_tags(self):
        import ocr_batch
        self.assertEqual([ocr_batch.status_tag(extra) for extra in
                          ([], ['--fast'], ['--paragraph'], ['--line-ocr-pages=1'],
                           ['--dump-structure'], ['--fast', '--debug-lines'])],
                         ['_auto', '_fast', '_paragraph', '_line', '_structure', '_fast_debug'])

    def test_failed_rerun_and_debug_keep_published_quality_records(self):
        import sqlite3
        import audit_ocr_quality as audit
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'; self.scanned(src, 1)
            api, lines, _, _ = self.responder()
            self.run_cli(src, api, lines)
            out = src.parent/'ocr_output'
            report_path = ocr_workflow.find_artifact(out, 'book_auto_report.json')
            before = report_path.read_bytes()
            def decisions():
                with contextlib.closing(sqlite3.connect(ocr_workflow.artifact_path(out, 'book_line_ocr.sqlite3'))) as db:
                    return db.execute('select value from decisions').fetchall()
            original = decisions()
            self.assertTrue(original)
            self.run_cli(src, api, lines, '--debug-lines')
            self.assertEqual(decisions(), original)
            self.assertEqual(report_path.read_bytes(), before)
            with patch.object(ocr, 'overlay', side_effect=RuntimeError('interrupted')):
                with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                    self.run_cli(src, api, lines)
            self.assertEqual(decisions(), original)
            self.assertEqual(report_path.read_bytes(), before)
            summary = audit.audit_one(out/'book_auto_searchable.pdf')
            self.assertGreater(summary['agreement']['compared'], 0)

    def test_failed_verification_keeps_previous_report_and_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'; self.scanned(src, 1)
            api, lines, _, _ = self.responder()
            self.run_cli(src, api, lines)
            out = src.parent/'ocr_output'
            pdf = out/'book_auto_searchable.pdf'; report = ocr_workflow.find_artifact(out, 'book_auto_report.json')
            previous_pdf, previous_report = pdf.read_bytes(), report.read_bytes()
            verify = ocr_workflow.verify
            def fail(*args, **kwargs):
                result = verify(*args, **kwargs)
                result.update(validation_failed=True, status='validation_failed')
                return result
            with patch.object(ocr_workflow, 'verify', side_effect=fail):
                with self.assertRaisesRegex(RuntimeError, 'validation failed'):
                    self.run_cli(src, api, lines)
            self.assertEqual(pdf.read_bytes(), previous_pdf)
            self.assertEqual(report.read_bytes(), previous_report)
            self.assertTrue(ocr_workflow.find_artifact(out, 'book_auto_failed_report.json').exists())

    def test_batch_stop_on_failure_uses_child_exit(self):
        import ocr_batch
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ('a.pdf', 'b.pdf'):
                self.scanned(root/name, 1)
            with patch('sys.argv', ['batch', str(root), '--stop-on-failure']), \
                    patch.object(ocr_batch.subprocess, 'call', return_value=2) as child, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ocr_batch.main(), 1)
            self.assertEqual(child.call_count, 1)


    def test_compact_output_keeps_only_pdf_at_top_level(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'; self.scanned(src, 1)
            api, lines, _, _ = self.responder()
            self.run_cli(src, api, lines)
            out = src.parent/'ocr_output'
            self.assertEqual({p.name for p in out.iterdir()},
                             {'book_auto_searchable.pdf', '.ocr'})
            self.assertEqual({p.name for p in (out/'.ocr').iterdir()},
                             {'book_line_ocr.sqlite3', 'book.lock'})
            self.run_cli(src, api, lines, '--debug-lines')
            self.assertEqual({p.name for p in out.iterdir()},
                             {'book_auto_searchable.pdf', '.ocr'})
            self.assertEqual({p.name for p in (out/'.ocr').iterdir()},
                             {'book_line_ocr.sqlite3', 'book.lock', 'book_auto_debug.pdf'})
            self.run_cli(src, lambda *a, **k: self.fail('Paragraph cache was lost'),
                         lambda *a, **k: self.fail('Line cache was lost'))

    def test_organize_migrates_legacy_output_and_reuses_both_caches(self):
        import audit_ocr_quality as audit
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'book.pdf'; self.scanned(src, 1)
            api, lines, _, _ = self.responder()
            self.run_cli(src, api, lines)
            out = src.parent/'ocr_output'; internal = out/'.ocr'
            # Recreate the old flat layout, including a stale lock file.
            for path in list(internal.iterdir()):
                path.rename(out/path.name)
            internal.rmdir()
            before = {p.name: (p.read_bytes() if p.is_file() else
                       {str(child.relative_to(p)): child.read_bytes()
                        for child in p.rglob('*') if child.is_file()})
                      for p in out.iterdir() if p.suffix != '.lock'}
            ocr_workflow.organize_output(out)
            for name, data in before.items():
                target = ocr_workflow.find_artifact(out, name)
                if isinstance(data, dict):
                    self.assertEqual(target.parent, internal)
                    self.assertEqual({str(child.relative_to(target)): child.read_bytes()
                                      for child in target.rglob('*') if child.is_file()}, data)
                else:
                    self.assertEqual(target.read_bytes(), data)
            ocr_workflow.organize_output(out)  # Idempotent.
            self.assertTrue(audit.audit_one(out/'book_auto_searchable.pdf')['coverage']['checked_against_layout'])
            self.run_cli(src, lambda *a, **k: self.fail('Paragraph cache was lost'),
                         lambda *a, **k: self.fail('Line cache was lost'))

    def test_organize_preserves_conflicts_and_rejects_active_legacy_jobs(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td); old = out/'book_pruned.json'; old.write_text('old')
            internal = out/'.ocr'; internal.mkdir()
            new = internal/old.name; new.write_text('new')
            with self.assertRaisesRegex(RuntimeError, 'preserved both'):
                ocr_workflow.organize_output(out)
            self.assertEqual(old.read_text(), 'old')
            self.assertEqual(new.read_text(), 'new')
            new.unlink()
            with ocr_workflow.job_lock(out/'book.lock'):
                with self.assertRaisesRegex(RuntimeError, 'already being processed'):
                    ocr_workflow.organize_output(out)
            self.assertTrue(old.exists())


if __name__ == '__main__':
    unittest.main()

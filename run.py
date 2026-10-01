"""Single entry point for PDF OCR, folder batches, audits, and tests."""
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    """Dispatch without executing the same module twice or leaking argv changes."""
    implementation = str(ROOT / 'compose')
    if implementation not in sys.path:
        sys.path.insert(0, implementation)
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ('-h', '--help'):
        print('사용법:\n'
              '  python run.py <PDF 또는 폴더> [OCR 옵션]\n'
              '  python run.py audit <결과 PDF 또는 폴더> [검사 옵션]\n'
              '  python run.py test\n\n'
              '  python run.py doctor [--services] [--json]\n'
              '  python run.py bookmarks <완료 PDF 또는 출력 폴더> [--review 검토.json]\n'
              '  python run.py organize <기존 출력 폴더>\n'
              '  python run.py compact <출력 폴더>\n'
              '  python run.py export <책_line_ocr.sqlite3> <내보낼 폴더>\n\n'
              '예: python run.py input --recursive\n'
              '    python run.py input/book.pdf --out output\n'
              '    python run.py audit input/ocr_output\n\n'
              '상세 옵션: python run.py <PDF 또는 폴더> --help\n'
              '사용 안내: docs/OCR_USAGE.md')
        return 0
    if args[0] == 'doctor':
        from ocr_doctor import main as doctor_main
        return doctor_main(args[1:])
    if args[0] == 'compact':
        import ocr_workflow
        if len(args) != 2 or not Path(args[1]).is_dir():
            raise SystemExit('Usage: python run.py compact <existing output folder>')
        removed = ocr_workflow.compact_output(Path(args[1]))
        print(f'[compact] {len(removed)} verified text files consolidated into SQLite and removed')
        return 0
    if args[0] == 'export':
        from ocr_artifacts import export_artifacts
        if len(args) != 3:
            raise SystemExit('Usage: python run.py export <document.sqlite3> <destination folder>')
        count = export_artifacts(args[1], args[2])
        print(f'[export] {count} text artifacts -> {Path(args[2]).resolve()}')
        return 0
    if args[0] == 'organize':
        import ocr_workflow
        if len(args) != 2 or not Path(args[1]).is_dir():
            raise SystemExit('Usage: python run.py organize <existing output folder>')
        ocr_workflow.organize_output(Path(args[1]))
        print('Organized:', Path(args[1]).resolve())
        return 0
    if args[0] == 'test':
        suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    if args[0] == 'bookmarks':
        script, forwarded = 'ocr_bookmark_refresh.py', args[1:]
    elif args[0] == 'audit':
        script, forwarded = 'audit_ocr_quality.py', args[1:]
    else:
        script = 'ocr_batch.py' if Path(args[0]).is_dir() else 'ocr_to_searchable_pdf.py'
        forwarded = args
    previous_argv = sys.argv
    try:
        sys.argv = [str(ROOT / 'compose' / script), *forwarded]
        result = importlib.import_module(Path(script).stem).main()
    finally:
        sys.argv = previous_argv
    return 0 if result is None else result


if __name__ == '__main__':
    sys.exit(main())

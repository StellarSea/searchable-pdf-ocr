# Working on this OCR project

Read `docs/ARCHITECTURE.md` for ownership and invariants, then the specific module
you need. User instructions take precedence. No external OCR calls or Docker
restarts are needed for the offline test suite.

## Main commands

- `python run.py test`: all offline regressions (install `requirements-dev.txt`).
- `python tools/check_architecture.py`: syntax and dependency-cycle checks.
- `python run.py <PDF or folder>`: real OCR; may start services and write outputs.
- `python run.py audit <output>`: quality signals, not ground-truth accuracy.

## Non-negotiable behavior

- Preserve original PDF pixels, Unicode content, reading order, and source-bound
  cache/review identities. Never substitute approximate line OCR for source text.
- Do not change render zoom, detection thresholds, model input shape or cache
  namespace as part of a structural refactor. Benchmark algorithm changes separately.
- Raw OCR caches and published reports are user data. Derived boundary repairs
  belong in separate caches. Publish audit decisions only after PDF verification.
- No multi-threaded PyMuPDF calls. SQLite and PDF writes belong to one owner.
- Do not restart running OCR jobs to test a refactor. Use temporary inputs/caches.
- Preserve dirty-worktree edits. Never delete `input/`, `output/`, `models/`, caches
  or old results during cleanup. `tmp/` is ignored but may hold useful diagnostics.

## Editing and verification

- `ocr_to_searchable_pdf.py` retains old import names for compatibility. New core
  code must import its owning module, not the facade. Avoid circular imports.
- Prefer explicit injected API callbacks to module-global monkey-patching in new tests.
- Add a regression for the behavior changed; compare pixels/boxes/text when layout
  is touched. Full-book correctness must not be inferred from a small sample.
- Keep CLI flags, output names, JSON/SQLite schemas and cache keys backward compatible
  unless the user requested a migration and it is documented and tested.
- Keep GPU model loading inside the service lifecycle. Core imports must remain
  usable offline without PaddleOCR, Docker, network access, or a GPU.

"""Offline syntax and import-boundary checks without importing GPU dependencies."""
import ast
import builtins
import symtable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = {'ocr_text', 'ocr_schema', 'ocr_render', 'ocr_fonts', 'ocr_api',
        'ocr_source', 'ocr_storage', 'ocr_modes', 'ocr_boundary', 'ocr_layout', 'ocr_prepare', 'ocr_artifacts',
        'ocr_gpu_prefetch', 'ocr_document_prefetch', 'ocr_repair_prefetch', 'ocr_verify', 'ocr_bookmarks',
        'ocr_bookmark_layout', 'ocr_bookmark_match', 'ocr_bookmark_refresh'}


def import_graph(folder):
    files = {path.stem: path for path in folder.glob('*.py')}
    graph = {}
    for name, path in files.items():
        imports = set()
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=str(path))):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split('.')[0])
        graph[name] = imports & files.keys()
    return graph


def validate_graph(graph):
    def visit(name, stack, done):
        if name in stack:
            raise ValueError('Circular import: ' + ' -> '.join([*stack, name]))
        if name in done:
            return
        for dependency in sorted(graph.get(name, ())):
            visit(dependency, [*stack, name], done)
        done.add(name)
    done = set()
    for name in graph:
        visit(name, [], done)
    for name in CORE:
        if 'ocr_to_searchable_pdf' in graph.get(name, ()):
            raise ValueError(f'{name} must not import the CLI / compatibility facade')


def undefined_globals(source, filename='<source>'):
    """Find statically unbound global loads, including rarely executed branches.

    This deliberately is not a complete linter / definite-assignment analysis.
    It catches extraction mistakes without importing GPU/runtime dependencies.
    """
    table = symtable.symtable(source, filename, 'exec')
    known = set(dir(builtins)) | {'__name__', '__file__', '__doc__', '__package__',
                                  '__spec__', '__loader__', '__cached__', '__builtins__'}
    known.update(s.get_name() for s in table.get_symbols()
                 if s.is_assigned() or s.is_imported() or s.is_namespace())
    missing = set()
    def visit(scope):
        missing.update(s.get_name() for s in scope.get_symbols()
                       if s.is_global() and s.is_referenced() and s.get_name() not in known)
        for child in scope.get_children():
            visit(child)
    visit(table)
    return sorted(missing)


def main():
    sources = [ROOT/'run.py']
    for folder in ('compose', 'tests', 'tools'):
        sources.extend((ROOT/folder).glob('*.py'))
    for path in sources:
        source = path.read_text(encoding='utf-8')
        ast.parse(source, filename=str(path))
        missing = undefined_globals(source, str(path))
        if missing:
            raise ValueError(f'{path}: undefined globals: {", ".join(missing)}')
    graph = import_graph(ROOT/'compose')
    validate_graph(graph)
    print(f'OK: {len(sources)} Python files parse with no undefined globals; {len(graph)} implementation modules have no import cycles.')


if __name__ == '__main__':
    main()

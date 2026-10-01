"""Text sidecars stored in the existing per-document SQLite database.

TextArtifact is an explicit storage entry, not an OS path. PDF files and the
process lock remain real files. Existing JSON keys and bytes are preserved.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3


def connect(database, *, writable=False):
    database = Path(database).resolve()
    if writable:
        database.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(database, timeout=30)
        db.execute('CREATE TABLE IF NOT EXISTS artifacts '
                   '(name TEXT PRIMARY KEY, content BLOB NOT NULL, sha256 TEXT NOT NULL)')
        db.commit()
        return db
    return sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=30)


class TextArtifact:
    def __init__(self, database, name):
        self.database = Path(database).resolve()
        key = PurePosixPath(name)
        if key.as_posix() == '.':
            raise ValueError('Artifact key cannot be empty')
        if key.is_absolute() or '..' in key.parts or '\\' in name or ':' in name:
            raise ValueError('Artifact key must be a safe relative name')
        self.key = key.as_posix()

    def __str__(self):
        return f'{self.database}::{self.key}'

    def exists(self):
        if not self.database.is_file():
            return False
        with closing(connect(self.database)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='artifacts'").fetchone():
                return False
            return db.execute('SELECT 1 FROM artifacts WHERE name=?', (self.key,)).fetchone() is not None

    is_file = exists

    def read_bytes(self):
        if not self.database.is_file():
            raise FileNotFoundError(str(self))
        with closing(connect(self.database)) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE name='artifacts'").fetchone()
            row = (db.execute('SELECT content,sha256 FROM artifacts WHERE name=?',
                              (self.key,)).fetchone() if table else None)
        if row is None:
            raise FileNotFoundError(str(self))
        data = bytes(row[0])
        if hashlib.sha256(data).hexdigest() != row[1]:
            raise ValueError(f'Artifact checksum mismatch: {self}')
        return data

    def read_text(self, encoding='utf-8'):
        return self.read_bytes().decode(encoding)

    def write_bytes(self, data):
        with closing(connect(self.database, writable=True)) as db, db:
            db.execute('INSERT OR REPLACE INTO artifacts VALUES (?,?,?)',
                       (self.key, data, hashlib.sha256(data).hexdigest()))
        return len(data)

    def write_text(self, text, encoding='utf-8'):
        self.write_bytes(text.encode(encoding))
        return len(text)

    def unlink(self, missing_ok=False):
        if not self.exists():
            if not missing_ok:
                raise FileNotFoundError(str(self))
            return
        with closing(connect(self.database, writable=True)) as db, db:
            db.execute('DELETE FROM artifacts WHERE name=?', (self.key,))


def resolve_text(value):
    """Read old file references and explicit database::entry references alike."""
    if isinstance(value, TextArtifact):
        return value
    if '::' in str(value):
        database, key = str(value).split('::', 1)
        return TextArtifact(database, key)
    path = Path(value)
    if path.exists():
        return path
    for parent in (path.parent, *path.parents):
        internal = parent if parent.name == '.ocr' else parent/'.ocr'
        if not internal.is_dir():
            continue
        try:
            key = path.relative_to(internal).as_posix()
        except ValueError:
            key = path.name
        for database in internal.glob('*_line_ocr.sqlite3'):
            entry = TextArtifact(database, key)
            if entry.exists():
                return entry
        if parent.name == '.ocr':
            break
    return path


def import_sidecars(database, files, root):
    """Commit and byte-verify every entry before removing any original file.

    Caller must hold the document's existing OS lock. Conflicts and links are
    rejected. Deletion is leaf-only within the explicitly supplied output root.
    A failed cleanup is safe to retry: already imported equal entries are valid.
    """
    root = Path(root).resolve()
    database = Path(database)
    if database.is_symlink() or not database.resolve().is_relative_to(root):
        raise ValueError(f'Unsafe migration database: {database}')
    records = []
    for path, key in files:
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f'Unsafe migration target: {path}')
        entry = TextArtifact(database, key)
        data = path.read_bytes()
        if path.suffix == '.json':
            json.loads(data.decode('utf-8'))
        records.append((path.resolve(), entry, data))
    if not records:
        return []
    with closing(connect(database, writable=True)) as db, db:
        for path, entry, data in records:
            old = db.execute('SELECT content FROM artifacts WHERE name=?', (entry.key,)).fetchone()
            if old is not None and bytes(old[0]) != data:
                raise RuntimeError(f'Conflicting artifact; preserved both: {path}, {entry}')
            db.execute('INSERT OR IGNORE INTO artifacts VALUES (?,?,?)',
                       (entry.key, data, hashlib.sha256(data).hexdigest()))
    with closing(connect(database)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('SQLite integrity check failed; old files retained')
    for path, entry, data in records:
        if entry.read_bytes() != data or path.read_bytes() != data:
            raise RuntimeError(f'Migration verification failed; old files retained: {path}')
    removed = []
    for path, entry, data in records:
        # Recheck each leaf at deletion time, not only at initial enumeration.
        if path.is_symlink() or not path.resolve().is_relative_to(root) or path.read_bytes() != data:
            raise RuntimeError(f'Artifact changed during migration: {path}')
        path.unlink()
        removed.append(str(path))
    return removed


def export_artifacts(database, destination):
    """Explicit lossless export; never overwrite existing files with other data."""
    destination = Path(destination).resolve()
    with closing(connect(database)) as db:
        keys = [row[0] for row in db.execute('SELECT name FROM artifacts ORDER BY name')]
    planned = []
    for key in keys:
        entry = TextArtifact(database, key)
        target = destination/key
        if target.is_symlink() or not target.resolve().is_relative_to(destination):
            raise ValueError(f'Unsafe export target: {target}')
        data = entry.read_bytes()
        if target.exists() and target.read_bytes() != data:
            raise FileExistsError(str(target))
        planned.append((target, data))
    for target, data in planned:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            with target.open('xb') as handle:
                handle.write(data)
    return len(planned)

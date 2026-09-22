"""Shared atomic JSON persistence. Write sidecars only after complete serialization."""
import json
import hashlib
import math
import sqlite3
from pathlib import Path
from ocr_artifacts import TextArtifact


def atomic_json(path, value):
    if isinstance(path, TextArtifact):
        path.write_text(json.dumps(value, ensure_ascii=False))
        return
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


class LineCache:
    """Own response storage and staged audit decisions for one job.

    SQLite is used in automatic mode, legacy JSON otherwise. Image keys, JSON
    field order, table names and publication semantics remain backward compatible.
    Only the job's owning thread may access this object.
    """

    def __init__(self, path: Path, automatic: bool = False):
        self.path = path
        self.data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        self.data.setdefault('responses', {})
        self.data.setdefault('crop_index', {})
        self.data.setdefault('detections', {})
        self.data['decisions'] = []
        self.detection_stats = {'hits': 0, 'misses': 0}
        self.db = None
        if automatic:
            self.db = sqlite3.connect(path.database if isinstance(path, TextArtifact)
                                      else path.with_suffix('.sqlite3'))
            try:
                self.db.execute('CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS crop_index (key TEXT PRIMARY KEY, image_key TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
                self.db.execute('CREATE TABLE IF NOT EXISTS detections (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                self.db.executemany('INSERT OR IGNORE INTO detections VALUES (?,?)',
                    ((key, json.dumps(value)) for key, value in self.data['detections'].items()))
                self.db.executemany('INSERT OR IGNORE INTO responses VALUES (?,?)', self.data['responses'].items())
                self.db.executemany('INSERT OR IGNORE INTO crop_index VALUES (?,?)', self.data['crop_index'].items())
                self.db.execute('CREATE TEMP TABLE pending_decisions (id INTEGER PRIMARY KEY, value TEXT NOT NULL)')
                self.db.commit()
            except BaseException:
                self.close()
                raise
            self.data['responses'] = {}
            self.data['crop_index'] = {}
            self.data['detections'] = {}

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def response(self, key):
        if self.db is not None:
            row = self.db.execute('SELECT value FROM responses WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
        return self.data['responses'].get(key)

    def indexed_response(self, crop_key):
        if crop_key is None:
            return None, None
        if self.db is not None:
            row = self.db.execute('SELECT c.image_key, r.value FROM crop_index c '
                                  'LEFT JOIN responses r ON r.key=c.image_key WHERE c.key=?',
                                  (crop_key,)).fetchone()
            return row if row else (None, None)
        else:
            key = self.data['crop_index'].get(crop_key)
        return key, self.response(key) if key else None

    def detection(self, key):
        if self.db is not None:
            row = self.db.execute('SELECT value FROM detections WHERE key=?', (key,)).fetchone()
            try:
                value = json.loads(row[0]) if row else None
            except (ValueError, TypeError):
                value = None
        else:
            value = self.data['detections'].get(key)
        # An empty list is a valid blank block; malformed data is a cache miss.
        valid = isinstance(value, list) and all(
            isinstance(box, list) and len(box) == 4
            and all(type(n) in (int, float) and math.isfinite(n) for n in box)
            and box[0] < box[2] and box[1] < box[3] for box in value)
        self.detection_stats['hits' if valid else 'misses'] += 1
        return value if valid else None

    def save_detection(self, key, boxes):
        value = [list(box) for box in boxes]
        if self.db is not None:
            self.db.execute('INSERT OR REPLACE INTO detections VALUES (?,?)', (key, json.dumps(value)))
            self.db.commit()
        else:
            self.data['detections'][key] = value
            atomic_json(self.path, self.data)

    def index_crop(self, crop_key, image_key):
        if crop_key is not None:
            if self.db is not None:
                self.db.execute('INSERT OR REPLACE INTO crop_index VALUES (?,?)', (crop_key, image_key))
            else:
                self.data['crop_index'][crop_key] = image_key

    def save_responses(self, values):
        for key, value in values.items():
            if self.db is not None:
                self.db.execute('INSERT OR REPLACE INTO responses VALUES (?,?)', (key, value))
            else:
                self.data['responses'][key] = value
        if self.db is not None:
            self.db.commit()
        elif values:
            atomic_json(self.path, self.data)

    def append_decision(self, decision):
        self.data['decisions'].append(decision)
        if self.db is not None:
            self.db.execute('INSERT INTO pending_decisions(value) VALUES (?)',
                            (json.dumps(decision, ensure_ascii=False),))
            self.db.commit()
        else:
            atomic_json(self.path, self.data)

    def publish_decisions(self):
        """Call only after the verified final PDF has been published."""
        if self.db is not None:
            with self.db:
                self.db.execute('DELETE FROM decisions')
                self.db.execute('INSERT INTO decisions SELECT * FROM pending_decisions')


class BoundaryCache:
    """Append per-page checkpoints; export the compatible aggregate only once.

    Shards are derived data, isolated by the full source/version/API identity.
    A failed page is never checkpointed. Existing aggregate JSON remains readable
    and is never replaced until a complete repair run has succeeded.
    """

    def __init__(self, path, identity):
        self.path, self.identity = path, identity
        namespace = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.namespace = namespace
        self.directory = (None if isinstance(path, TextArtifact) else
                          path.with_suffix(path.suffix + '.pages') / namespace)
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        self.pages = (saved.get('pages', {}) if isinstance(saved, dict)
                      and saved.get('identity') == identity else {})
        if not isinstance(self.pages, dict):
            self.pages = {}
        self.dirty = False

    def get(self, index, fingerprint):
        try:
            cached = json.loads(self.page_entry(index).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cached = None
        if not isinstance(cached, dict) or cached.get('fingerprint') != fingerprint:
            cached = self.pages.get(str(index))
        if (isinstance(cached, dict) and cached.get('fingerprint') == fingerprint
                and isinstance(cached.get('result'), dict)):
            if self.pages.get(str(index)) != cached:
                self.pages[str(index)] = cached
                self.dirty = True
            return cached['result']
        return None

    def save_page(self, index, fingerprint, result):
        cached = {'fingerprint': fingerprint, 'result': result}
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)
        atomic_json(self.page_entry(index), cached)
        self.pages[str(index)] = cached
        self.dirty = True

    def page_entry(self, index):
        if isinstance(self.path, TextArtifact):
            return TextArtifact(self.path.database, f'{self.path.key}.pages/{self.namespace}/{index}.json')
        return self.directory / f'{index}.json'

    def finish(self):
        if self.dirty:
            atomic_json(self.path, {'identity': self.identity, 'pages': self.pages})
            self.dirty = False

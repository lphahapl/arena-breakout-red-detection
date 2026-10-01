"""SQLite run/event archive. JSONL and PNG files remain portable evidence."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading


def valid_run(name: str) -> bool:
    return bool(re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name))


class RunStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'history.sqlite3'
        self._lock = threading.RLock()
        with self._db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, metadata TEXT NOT NULL DEFAULT '{}',
                    import_size INTEGER NOT NULL DEFAULT -1,
                    import_mtime INTEGER NOT NULL DEFAULT -1
                );
                CREATE TABLE IF NOT EXISTS events (
                    run TEXT NOT NULL, seq INTEGER NOT NULL,
                    record TEXT NOT NULL, review TEXT,
                    PRIMARY KEY(run, seq), FOREIGN KEY(run) REFERENCES runs(id)
                );
            ''')
        self.import_legacy()
        # No worker survives process startup. Retain its partial records.
        with self._db() as db:
            rows = db.execute('SELECT id,metadata FROM runs').fetchall()
        for row in rows:
            if json.loads(row['metadata']).get('status') == 'running':
                self.update_run(row['id'], {'status': 'interrupted'})

    @contextmanager
    def _db(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            try:
                with db:
                    yield db
            finally:
                db.close()

    def update_run(self, name: str, fields: dict):
        if not valid_run(name):
            raise ValueError('invalid run')
        with self._db() as db:
            row = db.execute('SELECT metadata FROM runs WHERE id=?', (name,)).fetchone()
            meta = json.loads(row['metadata']) if row else {}
            meta.update(fields)
            db.execute('INSERT INTO runs(id,metadata) VALUES(?,?) '
                       'ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata',
                       (name, json.dumps(meta, ensure_ascii=False)))

    def append_event(self, name: str, event: dict):
        if not valid_run(name):
            raise ValueError('invalid run')
        with self._db() as db:
            db.execute('INSERT OR IGNORE INTO runs(id) VALUES(?)', (name,))
            db.execute('INSERT INTO events(run,seq,record) VALUES(?,?,?) '
                       'ON CONFLICT(run,seq) DO UPDATE SET record=excluded.record',
                       (name, event['seq'], json.dumps(event, ensure_ascii=False)))

    def import_legacy(self):
        """Import changed files only; tolerate a torn final JSONL line."""
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not valid_run(directory.name):
                continue
            name = directory.name
            f = directory / 'events.jsonl'
            stat = f.stat() if f.exists() else None
            signature = (stat.st_size, stat.st_mtime_ns) if stat else (0, 0)
            with self._db() as db:
                row = db.execute('SELECT import_size,import_mtime FROM runs WHERE id=?',
                                 (name,)).fetchone()
            if row and tuple(row) == signature:
                continue
            with self._db() as db:
                db.execute('INSERT OR IGNORE INTO runs(id,metadata) VALUES(?,?)',
                           (name, json.dumps({'status': 'legacy', 'started_at': None})))
                if f.exists():
                    with f.open(encoding='utf-8') as source:
                        for line in source:
                            try:
                                event = json.loads(line)
                                if not isinstance(event, dict) or not isinstance(event.get('seq'), int):
                                    continue
                                db.execute('INSERT OR IGNORE INTO events(run,seq,record) VALUES(?,?,?)',
                                           (name, event['seq'], json.dumps(event, ensure_ascii=False)))
                            except (ValueError, TypeError):
                                continue
                db.execute('UPDATE runs SET import_size=?,import_mtime=? WHERE id=?',
                           (*signature, name))

    def events(self, name: str, since: int = 0) -> dict:
        if not valid_run(name):
            raise ValueError('invalid run')
        with self._db() as db:
            rows = db.execute('SELECT record,review FROM events WHERE run=? AND seq>? ORDER BY seq',
                              (name, since)).fetchall()
            last = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE run=?', (name,)).fetchone()[0]
        events = [{**json.loads(row['record']), 'review': row['review']} for row in rows]
        return {'events': events, 'last_seq': last}

    def review(self, name: str, seq: int, label: str | None):
        if label not in (None, 'red', 'clean', 'uncertain') or not valid_run(name):
            raise ValueError('invalid review')
        with self._db() as db:
            row = db.execute('SELECT record FROM events WHERE run=? AND seq=?', (name, seq)).fetchone()
            if not row or json.loads(row['record']).get('kind') not in ('red', 'clean', 'incomplete'):
                raise ValueError('only settled or incomplete events can be reviewed')
            db.execute('UPDATE events SET review=? WHERE run=? AND seq=?', (label, name, seq))

    def list_runs(self, limit: int = 30, offset: int = 0) -> dict:
        with self._db() as db:
            total = db.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
            rows = db.execute('''SELECT r.id,r.metadata,
                COUNT(e.seq) AS event_count,
                SUM(CASE WHEN json_extract(e.record,'$.kind')='red' THEN 1 ELSE 0 END) AS red,
                SUM(CASE WHEN json_extract(e.record,'$.kind')='clean' THEN 1 ELSE 0 END) AS clean,
                SUM(CASE WHEN json_extract(e.record,'$.kind')='incomplete' THEN 1 ELSE 0 END) AS incomplete,
                SUM(CASE WHEN e.review IS NOT NULL THEN 1 ELSE 0 END) AS reviewed,
                SUM(CASE WHEN json_extract(e.record,'$.kind')='red' AND e.review='clean' THEN 1 ELSE 0 END) AS false_positive,
                SUM(CASE WHEN json_extract(e.record,'$.kind')='clean' AND e.review='red' THEN 1 ELSE 0 END) AS missed,
                MAX(json_extract(e.record,'$.t')) AS last_event
                FROM (SELECT * FROM runs ORDER BY id DESC LIMIT ? OFFSET ?) r
                LEFT JOIN events e ON e.run=r.id GROUP BY r.id ORDER BY r.id DESC''',
                (limit, offset)).fetchall()
            result = []
            for row in rows:
                meta = json.loads(row['metadata'])
                counts = {key: row[key] for key in ('red', 'clean', 'incomplete')}
                result.append({**meta, 'id': row['id'], 'counts': counts,
                               'total': counts['red'] + counts['clean'],
                               'red_rate': round(counts['red'] / (counts['red'] + counts['clean']) * 100, 2)
                                    if counts['red'] + counts['clean'] else None,
                               **{key: row[key] for key in ('event_count', 'last_event',
                                      'reviewed', 'false_positive', 'missed')}})
        return {'runs': result, 'total': total, 'offset': offset, 'limit': limit}

    def run(self, name: str) -> dict | None:
        if not valid_run(name):
            raise ValueError('invalid run')
        # Locate the summary using the same ordering and counting as the list.
        with self._db() as db:
            if not db.execute('SELECT 1 FROM runs WHERE id=?', (name,)).fetchone():
                return None
            offset = db.execute('SELECT COUNT(*) FROM runs WHERE id>?', (name,)).fetchone()[0]
        return self.list_runs(limit=1, offset=offset)['runs'][0]

    def run_names(self) -> list[str]:
        with self._db() as db:
            return [row[0] for row in db.execute('SELECT id FROM runs ORDER BY id DESC')]

"""Disposable search resolution cache and saved latency inspection."""
import json
from pathlib import Path
import sqlite3
import threading


class ResolutionCache:
    def __init__(self, path, entries=5000, byte_limit=8 * 1024 * 1024):
        self.path, self.entries, self.byte_limit = Path(path), entries, byte_limit
        self.lock, self.db = threading.Lock(), None

    def connect(self):
        if self.db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for attempt in range(2):
                db = sqlite3.connect(self.path, check_same_thread=False)
                try:
                    self.path.chmod(0o600)
                    db.execute('PRAGMA journal_mode=MEMORY')
                    db.execute('PRAGMA synchronous=OFF')  # Recreated on a miss; never household state.
                    db.execute('PRAGMA cache_size=-1024')
                    db.execute('PRAGMA max_page_count=3072')
                    db.execute('CREATE TABLE IF NOT EXISTS picks (key TEXT UNIQUE, value TEXT, size INTEGER)')
                except (OSError, sqlite3.Error) as error:
                    db.close()
                    if attempt or not self.reset(error):
                        raise
                else:
                    self.db = db
                    break
        return self.db

    def reset(self, error):
        if getattr(error, 'sqlite_errorcode', 0) & 255 not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
            return False
        if self.db is not None:
            self.db.close()
            self.db = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def get(self, key):
        with self.lock:
            try:
                row = self.connect().execute('SELECT value FROM picks WHERE key=?', (key,)).fetchone()
                return json.loads(row[0]) if row else None
            except (OSError, sqlite3.Error, ValueError) as error:
                self.reset(error)
                return None

    def put(self, key, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        size = len(key.encode()) + len(raw.encode())
        if size > self.byte_limit:
            return
        with self.lock:
            try:
                db = self.connect()
                with db:
                    db.execute('DELETE FROM picks WHERE key=?', (key,))
                    count, total = db.execute('SELECT count(*), coalesce(sum(size), 0) FROM picks').fetchone()
                    while count >= self.entries or total + size > self.byte_limit:
                        rowid, old = db.execute('SELECT rowid, size FROM picks ORDER BY rowid LIMIT 1').fetchone()
                        db.execute('DELETE FROM picks WHERE rowid=?', (rowid,))
                        count, total = count - 1, total - old
                    db.execute('INSERT INTO picks VALUES (?, ?, ?)', (key, raw, size))
            except (OSError, sqlite3.Error) as error:
                self.reset(error)

    def stats(self):
        with self.lock:
            try:
                count, total = self.connect().execute('SELECT count(*), coalesce(sum(size), 0) FROM picks').fetchone()
            except sqlite3.Error as error:
                if not self.reset(error):
                    raise
                count, total = self.connect().execute('SELECT count(*), coalesce(sum(size), 0) FROM picks').fetchone()
            return {'entries': count, 'bytes': total, 'disk_bytes': self.path.stat().st_size}

    def close(self):
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None


def timings(data, limit=15):
    path = Path(data) / 'searches.jsonl'
    rows = []
    try:
        with path.open('rb') as fh:
            fh.seek(max(0, path.stat().st_size - 256 * 1024))
            if fh.tell():
                fh.readline()
            for line in fh:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and isinstance(row.get('timing'), dict):
                        rows.append(row)
                except (ValueError, UnicodeError):
                    continue
    except FileNotFoundError:
        pass
    return {'searches': rows[-limit:]}

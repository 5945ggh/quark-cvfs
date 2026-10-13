"""Private account-scoped SQLite facts, immutable snapshots and durable foreground jobs."""
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .models import RemoteEntry, ScopeBinding, VfsError, same_browse_identity


class Store:
    def __init__(self, path: Path):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise VfsError("UNSAFE_STATE_PATH", "Database cannot be a symbolic link.")
        self.db = sqlite3.connect(self.path)
        self._held = threading.local()
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bindings(account TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, account TEXT NOT NULL,
                parent TEXT NOT NULL, data TEXT NOT NULL, started TEXT NOT NULL, finished TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(account TEXT NOT NULL, kind TEXT NOT NULL,
                key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(account,kind,key));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, account TEXT NOT NULL,
                action TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS job_items(job TEXT NOT NULL REFERENCES jobs(id),
                ordinal INTEGER NOT NULL, data TEXT NOT NULL, state TEXT NOT NULL,
                result TEXT, PRIMARY KEY(job,ordinal));
        """)

    def close(self):
        self.db.close()

    def bind(self, binding: ScopeBinding) -> None:
        if not binding.account or not binding.scope_uuid or not binding.marker_fid or not binding.marker_name:
            raise VfsError("SCOPE_INVALID", "Scope binding lacks marker identity.")
        if binding.marker_name in ('.', '..') or any(c in binding.marker_name for c in ('/', '\\', '\x00')):
            raise VfsError('SCOPE_INVALID', 'Marker name must be a single safe local component.')
        if binding.marker_size is not None and (type(binding.marker_size) is not int or not 0 <= binding.marker_size <= 65536):
            raise VfsError('SCOPE_INVALID', 'Scope marker must have bounded size.')
        if len(binding.marker_sha256) != 64 or any(c not in '0123456789abcdef' for c in binding.marker_sha256):
            raise VfsError("SCOPE_INVALID", "Scope marker requires a trusted SHA-256.")
        try:
            with self.db:
                self.db.execute("INSERT INTO bindings VALUES (?,?)", (binding.account, json.dumps(asdict(binding))))
        except sqlite3.IntegrityError:
            raise VfsError("SCOPE_ALREADY_BOUND", "Existing aliases cannot silently change account scope; use a new alias and scope.") from None

    def binding(self, account: str) -> ScopeBinding:
        row = self.db.execute("SELECT data FROM bindings WHERE account=?", (account,)).fetchone()
        if row is None:
            raise VfsError("ACCOUNT_UNBOUND", "Bind a verified cloud marker before accessing account data.")
        return ScopeBinding(**json.loads(row[0]))

    @contextmanager
    def account_lock(self, account: str):
        """Nonblocking process lock shared by all wrappers using this database."""
        held = getattr(self._held, 'accounts', {})
        lock_key = '__runtime__'
        if lock_key in held:
            held[lock_key] += 1
            try:
                yield
            finally:
                held[lock_key] -= 1
            return
        try:
            import fcntl
        except ImportError:
            raise VfsError("LOCK_UNAVAILABLE", "Account process locking is unavailable on this platform.") from None
        name = 'runtime.lock'
        directory = self.path.parent / 'locks'
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink():
            raise VfsError('UNSAFE_STATE_PATH', 'Lock directory cannot be a symbolic link.')
        fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise VfsError("ACCOUNT_BUSY", "Another foreground operation holds this account lock.", retryable=True) from None
            held[lock_key] = 1
            self._held.accounts = held
            try:
                yield
            finally:
                del held[lock_key]
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def save_snapshot(self, account: str, parent: str, entries: tuple[RemoteEntry, ...], started: str, finished: str) -> str:
        key = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?)", (key, account, parent,
                json.dumps([asdict(e) for e in entries]), started, finished))
        return key

    def snapshot(self, account: str, key: str) -> dict:
        row = self.db.execute("SELECT parent,data,started,finished FROM snapshots WHERE id=? AND account=?", (key, account)).fetchone()
        if row is None:
            raise VfsError("SNAPSHOT_UNKNOWN", "Snapshot does not belong to this account scope.")
        return {'parent_fid': row[0], 'entries': tuple(RemoteEntry(**e) for e in json.loads(row[1])),
                'started_at': row[2], 'finished_at': row[3], 'complete': True}

    def cached_entry(self, account: str, fid: str) -> RemoteEntry | None:
        rows = self.db.execute("SELECT data FROM snapshots WHERE account=? ORDER BY rowid DESC", (account,))
        for row in rows:
            for entry in json.loads(row[0]):
                if same_browse_identity(entry['fid'], fid):
                    return RemoteEntry(**entry)
        return None

    def put_document(self, account: str, kind: str, key: str, data: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO documents VALUES(?,?,?,?) ON CONFLICT(account,kind,key) DO UPDATE SET data=excluded.data",
                            (account, kind, key, json.dumps(data)))

    def get_document(self, account: str, kind: str, key: str) -> dict | None:
        row = self.db.execute("SELECT data FROM documents WHERE account=? AND kind=? AND key=?", (account, kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def list_documents(self, account: str, kind: str) -> tuple[dict, ...]:
        return tuple(json.loads(r[0]) for r in self.db.execute("SELECT data FROM documents WHERE account=? AND kind=? ORDER BY rowid", (account, kind)))

    def create_job(self, account: str, action: str, items: list[dict]) -> str:
        key = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO jobs VALUES(?,?,?,?)", (key, account, action, 'pending'))
            self.db.executemany("INSERT INTO job_items VALUES(?,?,?,?,?)", [(key, i, json.dumps(item), 'pending', None) for i, item in enumerate(items)])
        return key

    def set_item(self, job: str, ordinal: int, state: str, result: dict | None = None):
        with self.db:
            self.db.execute("UPDATE job_items SET state=?,result=? WHERE job=? AND ordinal=?", (state, json.dumps(result) if result is not None else None, job, ordinal))

    def set_job(self, job: str, state: str):
        with self.db:
            self.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job))

    def job(self, account: str, key: str) -> dict:
        row = self.db.execute("SELECT action,state FROM jobs WHERE id=? AND account=?", (key, account)).fetchone()
        if row is None:
            raise VfsError("JOB_UNKNOWN", "Job is not in this account scope.")
        items = tuple({'ordinal': r[0], 'input': json.loads(r[1]), 'state': r[2],
                       'result': json.loads(r[3]) if r[3] else None} for r in self.db.execute(
                           "SELECT ordinal,data,state,result FROM job_items WHERE job=? ORDER BY ordinal", (key,)))
        return {'job_id': key, 'account': account, 'action': row[0], 'state': row[1], 'items': items}

    def jobs(self, account: str) -> tuple[dict, ...]:
        return tuple(self.job(account, r[0]) for r in self.db.execute("SELECT id FROM jobs WHERE account=? ORDER BY rowid DESC", (account,)))

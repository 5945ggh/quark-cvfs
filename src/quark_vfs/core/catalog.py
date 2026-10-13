"""Fresh scoped enumeration, exact path resolution and immutable local pagination."""
import hashlib
import os
import stat
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

from ..models import (
    FileRef,
    Page,
    Provider,
    RemoteEntry,
    VfsError,
    same_browse_identity,
)
from ..storage import Store


def _scoped(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        reference = args[0] if args else kwargs.get('account', kwargs.get('ref', kwargs.get('directory')))
        account = reference.account if isinstance(reference, FileRef) else reference
        with self.store.account_lock(account), self.scope(account):
            return method(self, *args, **kwargs)
    return wrapped


def secure_bytes(path: Path, directory: Path, *, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, tuple[int, ...]]:
    """Read bounded regular files via no-follow descriptors on every component."""
    path, directory = Path(path).absolute(), Path(directory).absolute()
    if '..' in path.parts or '..' in directory.parts:
        raise VfsError('UNSAFE_LOCAL_PATH', 'Parent traversal is forbidden.')
    try:
        path.relative_to(directory)
    except ValueError:
        raise VfsError('UNSAFE_LOCAL_PATH', 'File is outside the allowed local directory.') from None
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise VfsError('PATH_CHECK_UNAVAILABLE', 'Secure local reads unavailable.')
    handles: list[int] = []
    try:
        handles.append(os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY))
        for part in path.parts[1:-1]:
            handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
        handles.append(os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handles[-1]))
        before = os.fstat(handles[-1])
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise VfsError('UNSAFE_LOCAL_PATH', 'Expected a bounded regular file.')
        with os.fdopen(os.dup(handles[-1]), 'rb') as stream:
            data = stream.read(max_bytes + 1)
        after = os.fstat(handles[-1])
        def fingerprint(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if fingerprint(before) != fingerprint(after) or len(data) != before.st_size:
            raise VfsError('SOURCE_CHANGED', 'Local source changed while reading.')
        return data, fingerprint(after)
    except OSError:
        raise VfsError('UNSAFE_LOCAL_PATH', 'Cannot securely read the local file.') from None
    finally:
        for handle in reversed(handles):
            os.close(handle)


class Catalog:
    def __init__(self, provider: Provider, store: Store, workspace: Path):
        self.provider, self.store = provider, store
        self.workspace = Path(workspace).absolute()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._active_scopes: dict[str, int] = {}

    @contextmanager
    def scope(self, account: str):
        if account not in self._active_scopes:
            self.assert_scope(account)
        self._active_scopes[account] = self._active_scopes.get(account, 0) + 1
        try:
            yield
        finally:
            self._active_scopes[account] -= 1
            if not self._active_scopes[account]:
                del self._active_scopes[account]

    def assert_scope(self, account: str, *, fresh: bool = False) -> None:
        with self.store.account_lock(account):
            self._verify_scope(account, fresh=fresh)

    def _verify_scope(self, account: str, *, fresh: bool = False) -> None:
        if not fresh and account in self._active_scopes:
            return
        binding = self.store.binding(account)
        management = self.store.get_document(account, 'management', 'root')
        parent = binding.marker_parent_fid or (management.get('fid') if management else None)
        if not parent:
            raise VfsError('SCOPE_MARKER_LOCATION_UNKNOWN', 'Scope verification requires the marker parent directory.')
        rows = self.provider.list_directory(account, parent)
        candidates = [entry for entry in rows if same_browse_identity(entry.fid, binding.marker_fid)
                      and entry.name == binding.marker_name and entry.kind == 'file']
        if (len(candidates) != 1 or candidates[0].size is None or candidates[0].size > 65536
                or (binding.marker_size is not None and candidates[0].size != binding.marker_size)):
            raise VfsError('SCOPE_MISMATCH', 'Scope marker identity or bounded size changed; it was not downloaded.')
        with tempfile.TemporaryDirectory(prefix='scope-', dir=self.workspace) as temporary:
            staging = Path(temporary)
            result = self.provider.download_file(account, candidates[0].fid, staging)
            if result.name != binding.marker_name or Path(result.path) != staging / binding.marker_name:
                raise VfsError('SCOPE_MISMATCH', 'Account marker path or name changed.')
            data, _ = secure_bytes(result.path, staging, max_bytes=65536)
            if hashlib.sha256(data).hexdigest() != binding.marker_sha256:
                raise VfsError('SCOPE_MISMATCH', 'Account marker content does not match the bound scope.')

    @_scoped
    def entries(self, account: str, parent_fid: str) -> tuple[RemoteEntry, ...]:
        self.assert_scope(account)
        started = datetime.now(UTC).isoformat()
        raw = self.provider.list_directory(account, parent_fid)
        seen: set[str] = set()
        entries = []
        for entry in raw:
            # Tail identity is only for local aggregation of this scoped browse response.
            key = entry.fid.rsplit('|', 1)[-1] if '|' in entry.fid and entry.fid.rsplit('|', 1)[-1] else entry.fid
            if key in seen:
                raise VfsError('UNKNOWN_SCHEMA', 'Duplicate identities in the direct enumeration.')
            seen.add(key)
            entries.append(replace(entry, parent_fid=parent_fid,
                                   raw_parent_fid=entry.raw_parent_fid or entry.parent_fid,
                                   observed_at=datetime.now(UTC).isoformat()))
        result = tuple(entries)
        self.last_snapshot = self.store.save_snapshot(account, parent_fid, result, started, datetime.now(UTC).isoformat())
        return result

    def await_entries(self, account: str, parent_fid: str,
                      observed: Callable[[tuple[RemoteEntry, ...]], bool]) -> tuple[RemoteEntry, ...]:
        """Bounded read-only reconciliation after an acknowledged mutation only."""
        for attempt in range(4):
            rows = self.entries(account, parent_fid)
            if observed(rows):
                return rows
            if attempt < 3:
                time.sleep(0.1 * (2 ** attempt))
        raise VfsError('OUTCOME_UNKNOWN', 'Acknowledged change was not visible within bounded read-only reconciliation.')

    @_scoped
    def list(self, directory: FileRef, *, page_size: int = 100, page_token: str | None = None) -> Page:
        self.assert_scope(directory.account)
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise VfsError('INVALID_PAGE_SIZE', 'Page size must be between 1 and 1000.')
        if page_token is None:
            entry = self.resolve(directory)
            if entry.kind != 'directory':
                raise VfsError('NOT_DIRECTORY', 'Listing requires a directory.')
            self.entries(directory.account, entry.fid)
            key, offset = self.last_snapshot, 0
            self.store.put_document(directory.account, 'snapshot_reference', key, asdict(directory))
        else:
            try:
                key, value = page_token.split(':')
                offset = int(value)
                if offset < 0:
                    raise ValueError
            except ValueError:
                raise VfsError('INVALID_PAGE_TOKEN', 'Invalid local snapshot token.') from None
        snapshot = self.store.snapshot(directory.account, key)
        # Token scope must match this reference; never treat it as an upstream cursor.
        if directory.fid is not None and not same_browse_identity(snapshot['parent_fid'], directory.fid):
            raise VfsError('INVALID_PAGE_TOKEN', 'Token belongs to another directory.')
        if directory.path is not None:
            original = self.store.get_document(directory.account, 'snapshot_reference', key)
            if original is None or original.get('path') != directory.path:
                raise VfsError('INVALID_PAGE_TOKEN', 'Token belongs to another requested path.')
        rows = snapshot['entries']
        if offset > len(rows):
            raise VfsError('INVALID_PAGE_TOKEN', 'Offset exceeds the snapshot.')
        end = offset + page_size
        return Page(key, rows[offset:end], f'{key}:{end}' if end < len(rows) else None, True,
                    snapshot['started_at'], snapshot['finished_at'], directory.account, snapshot['parent_fid'])

    @_scoped
    def resolve(self, ref: FileRef) -> RemoteEntry:
        self.assert_scope(ref.account)
        if ref.fid == '0' or ref.path == '/':
            return RemoteEntry('0', '0', '/', 'directory')
        if ref.path is not None:
            parts = ref.path.split('/')[1:]
            if any(part in ('', '.', '..') or '\x00' in part for part in parts):
                raise VfsError('INVALID_REFERENCE', 'Path components must be explicit names.')
            path_parent = '0'
            entry = None
            for index, part in enumerate(parts):
                matches = [e for e in self.entries(ref.account, path_parent) if e.name == part]
                if not matches:
                    raise VfsError('LOCATION_UNKNOWN', 'Path component is not currently visible.')
                if len(matches) != 1:
                    raise VfsError('AMBIGUOUS_PATH', 'Path component has multiple exact matches.')
                entry = matches[0]
                if index < len(parts) - 1 and entry.kind != 'directory':
                    raise VfsError('NOT_DIRECTORY', 'Intermediate path component is not a directory.')
                path_parent = entry.fid
            assert entry is not None
            return entry
        parent = ref.parent_fid_hint
        if parent is None:
            cached = self.store.cached_entry(ref.account, ref.fid or '')
            parent = cached.parent_fid if cached else None
        if parent is None:
            raise VfsError('LOCATION_UNKNOWN', 'A fresh lookup requires a reliable parent directory.')
        matches = [e for e in self.entries(ref.account, parent) if same_browse_identity(e.fid, ref.fid or '')]
        if len(matches) != 1:
            raise VfsError('LOCATION_UNKNOWN', 'Object is not currently identifiable in its known parent.')
        return matches[0]

    @_scoped
    def stat(self, ref: FileRef, *, fresh: bool = True) -> RemoteEntry:
        self.assert_scope(ref.account)
        if not fresh and ref.fid is not None:
            entry = self.store.cached_entry(ref.account, ref.fid)
            if entry is not None:
                return entry  # observed_at explicitly identifies this cached observation.
        return self.resolve(ref)

    @_scoped
    def search(self, account: str, keyword: str, parent_fid: str | None = None) -> dict:
        if not keyword:
            raise VfsError('INVALID_QUERY', 'Search requires a keyword.')
        self.assert_scope(account)
        return {'entries': tuple(self.provider.search(account, keyword, parent_fid)),
                'complete': False, 'query_parent_fid': parent_fid}

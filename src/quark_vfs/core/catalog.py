"""Fresh scoped enumeration, exact path resolution and immutable local pagination."""
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import time
from collections import deque
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
from ..storage import Store, validate_binding_fields


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
        stage = 'scope_binding'
        try:
            binding = self.store.binding(account)
            validate_binding_fields(binding, account)
            parent = binding.marker_parent_fid
            if parent is None:
                try:
                    management = self.store.get_document(account, 'management', 'root')
                    if management is not None and (not isinstance(management, dict)
                                                  or not isinstance(management.get('fid'), str)
                                                  or not management['fid'] or '\x00' in management['fid']):
                        raise ValueError
                    parent = management['fid'] if management is not None else None
                except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
                    raise VfsError('STATE_UNREADABLE', 'Local management directory identity cannot be read.') from None
            if not parent:
                raise VfsError('SCOPE_MARKER_LOCATION_UNKNOWN', 'Scope verification requires the marker parent directory.')
            stage = 'scope_directory_read'
            rows = tuple(self.provider.list_directory(account, parent))
            stage = 'scope_marker_observation'
            candidates = [entry for entry in rows if same_browse_identity(entry.fid, binding.marker_fid)
                          and entry.name == binding.marker_name and entry.kind == 'file']
            if not candidates and not any(entry.name == binding.marker_name or same_browse_identity(entry.fid, binding.marker_fid) for entry in rows):
                raise VfsError('SCOPE_MARKER_MISSING', 'The bound marker was absent from a successful directory observation.')
            if (len(candidates) != 1 or candidates[0].size is None or candidates[0].size > 65536
                    or (binding.marker_size is not None and candidates[0].size != binding.marker_size)):
                raise VfsError('SCOPE_MISMATCH', 'Scope marker identity or bounded size changed; it was not downloaded.')
            with tempfile.TemporaryDirectory(prefix='scope-', dir=self.workspace) as temporary:
                staging = Path(temporary)
                stage = 'scope_marker_download'
                result = self.provider.download_file(account, candidates[0].fid, staging)
                stage = 'scope_marker_content'
                if result.name != binding.marker_name or Path(result.path) != staging / binding.marker_name:
                    raise VfsError('SCOPE_MISMATCH', 'Account marker path or name changed.')
                data, _ = secure_bytes(result.path, staging, max_bytes=65536)
                if hashlib.sha256(data).hexdigest() != binding.marker_sha256:
                    raise VfsError('SCOPE_MISMATCH', 'Account marker content does not match the bound scope.')
        except VfsError as error:
            safe = VfsError(error.code, 'Scope verification failed; inspect the safe stage and codes before recovery.', retryable=error.retryable)
            safe.stage = stage
            for field in ('upstream_code', 'exit_code'):
                value = getattr(error, field, None)
                if type(value) is int:
                    setattr(safe, field, value)
            raise safe from None
        except OSError:
            safe = VfsError('LOCAL_IO_ERROR', 'Local scope verification staging failed; raw details are suppressed.')
            safe.stage = stage
            raise safe from None

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

    def tree(self, directory: FileRef, *, depth: int = 2, max_directories: int = 100,
             max_entries: int = 1000) -> dict:
        """Observe a bounded breadth-first tree, with an immutable snapshot per directory.

        Entry limits bound displayed descendants; each attempted directory is read fully.
        The starting reference is resolved once within one verified account scope.
        """
        for value, lower, upper in ((depth, 0, 32), (max_directories, 1, 1000),
                                    (max_entries, 1, 10000)):
            if type(value) is not int or not lower <= value <= upper:
                raise VfsError('INVALID_TREE_LIMIT', 'Tree limits are outside their supported integer ranges.')
        started = datetime.now(UTC).isoformat()
        stats = {'entries': 0, 'files': 0, 'directories': 0, 'directories_attempted': 0,
                     'directories_listed': 0, 'known_file_bytes': 0, 'files_without_size': 0}
        complete = True
        with self.store.account_lock(directory.account), self.scope(directory.account):
            entry = self.resolve(directory)
            if entry.kind != 'directory':
                raise VfsError('NOT_DIRECTORY', 'Tree requires a directory.')
            root = dict(asdict(entry), depth=0, children=[], listing_status='depth_limit')
            pending = deque([root] if depth else [])
            identities = [entry.fid]
            aborted = False
            while pending:
                node = pending.popleft()
                if aborted:
                    node['listing_status'] = 'aborted'
                    complete = False
                    continue
                if stats['entries'] >= max_entries:
                    node['listing_status'] = 'entry_limit'
                    complete = False
                    continue
                if stats['directories_attempted'] >= max_directories:
                    node['listing_status'] = 'directory_limit'
                    complete = False
                    continue
                stats['directories_attempted'] += 1
                try:
                    rows = self.entries(directory.account, node['fid'])
                except (VfsError, OSError) as error:
                    node['listing_status'] = 'failed'
                    code = error.code if isinstance(error, VfsError) else 'LOCAL_IO_ERROR'
                    node['error'] = {'code': code, 'message': 'Directory observation failed; raw details are suppressed.',
                                         'stage': 'tree_directory_read', 'retryable': False}
                    for field in ('upstream_code', 'exit_code'):
                        numeric_code = getattr(error, field, None)
                        if type(numeric_code) is int:
                            node['error'][field] = numeric_code
                    complete = False
                    aborted = code == 'AUTH_REQUIRED' or code.startswith('SCOPE_')
                    continue
                stats['directories_listed'] += 1
                snapshot = self.store.snapshot(directory.account, self.last_snapshot)
                node.update(snapshot_id=self.last_snapshot, listing_started_at=snapshot['started_at'],
                            listing_finished_at=snapshot['finished_at'], listing_status='complete')
                ordered = sorted(rows, key=lambda row: (row.kind != 'directory', row.name, row.fid))
                available = max_entries - stats['entries']
                if len(ordered) > available:
                    node.update(listing_status='entry_limit', omitted_entries=len(ordered) - available)
                    complete = False
                for child_entry in ordered[:available]:
                    child = dict(asdict(child_entry), depth=node['depth'] + 1)
                    node['children'].append(child)
                    stats['entries'] += 1
                    if child_entry.kind == 'file':
                        stats['files'] += 1
                        if child_entry.size is None:
                            stats['files_without_size'] += 1
                        else:
                            stats['known_file_bytes'] += child_entry.size
                        continue
                    stats['directories'] += 1
                    child.update(children=[], listing_status='depth_limit')
                    repeated = any(same_browse_identity(child_entry.fid, fid) for fid in identities)
                    if repeated:
                        child['listing_status'] = 'repeated'
                        complete = False
                    else:
                        identities.append(child_entry.fid)
                        if child['depth'] < depth:
                            pending.append(child)
        return {'root': root, 'requested_depth': depth,
                    'limits': {'max_directories': max_directories, 'max_entries': max_entries}, 'stats': stats,
                    'complete_for_depth': complete, 'started_at': started,
                    'finished_at': datetime.now(UTC).isoformat(), 'consistency': 'per_directory'}

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

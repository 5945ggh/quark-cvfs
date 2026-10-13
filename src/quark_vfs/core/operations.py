"""Fresh explicit mutations with collision checks and per-item durable outcomes."""
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import PurePosixPath

from ..models import (
    BatchResult,
    Expected,
    FileRef,
    RemoteEntry,
    VfsError,
    same_browse_identity,
)
from ..storage import Store
from .catalog import Catalog
from .jobs import Jobs


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not name or name in ('.', '..') or any(c in name for c in ('/', '\\', '\x00')):
        raise VfsError('INVALID_NAME', 'Name must be one explicit file component.')


def choose_name(name: str, rows: Sequence[RemoteEntry], conflict: str) -> str:
    validate_name(name)
    if conflict not in ('fail', 'keep_both'):
        raise VfsError('UNSUPPORTED_CONFLICT', 'Only fail and keep_both are available.')
    occupied = {row.name for row in rows}
    if name not in occupied:
        return name
    if conflict == 'fail':
        raise VfsError('NAME_CONFLICT', 'Destination name already exists.')
    path = PurePosixPath(name)
    for number in range(1, 1001):
        candidate = f'{path.stem} ({number}){path.suffix}'
        if candidate not in occupied:
            return candidate
    raise VfsError('NAME_CONFLICT', 'No free keep_both name within the bounded search.')


def check_expected(entry: RemoteEntry, expected: Expected | None) -> None:
    if expected is not None:
        observed = Expected.from_entry(entry)
        same_parent = (observed.parent_fid is None and expected.parent_fid is None) or (
            observed.parent_fid is not None and expected.parent_fid is not None
            and same_browse_identity(observed.parent_fid, expected.parent_fid))
        if not same_parent or (observed.name, observed.kind, observed.size, observed.modified, observed.content_hash) != (
                expected.name, expected.kind, expected.size, expected.modified, expected.content_hash):
            raise VfsError('EXPECTED_CHANGED', 'Fresh source metadata differs from the explicit expectation.')


class Operations:
    def __init__(self, catalog: Catalog, store: Store):
        self.catalog, self.store = catalog, store
        self.jobs = Jobs(store)

    def assert_mutable(self, account: str, fid: str) -> None:
        self.catalog.assert_scope(account)
        protected = {self.store.binding(account).marker_fid, '0'}
        protected.update(doc['fid'] for doc in self.store.list_documents(account, 'management') if isinstance(doc.get('fid'), str))
        protected.update(doc['trash_root_fid'] for doc in self.store.list_documents(account, 'trash') if isinstance(doc.get('trash_root_fid'), str))
        if any(same_browse_identity(fid, known) for known in protected):
            raise VfsError('PROTECTED_SYSTEM_ENTRY', 'Namespace markers and management roots cannot be moved or renamed.')

    def mkdir(self, parent: FileRef, name: str, *, conflict: str = 'fail') -> RemoteEntry:
        with self.store.account_lock(parent.account), self.catalog.scope(parent.account):
            self.catalog.assert_scope(parent.account, fresh=True)
            directory = self.catalog.resolve(parent)
            if directory.kind != 'directory':
                raise VfsError('NOT_DIRECTORY', 'Explicit parent must be a directory.')
            final = choose_name(name, self.catalog.entries(parent.account, directory.fid), conflict)
            key = self.store.create_job(parent.account, 'mkdir', [{'parent_fid': directory.fid, 'name': final}])
            self.store.set_job(key, 'running')
            self.store.set_item(key, 0, 'running')
            native_id = None
            try:
                self.catalog.assert_scope(parent.account, fresh=True)
                native_id = self.catalog.provider.mkdir(parent.account, directory.fid, final)
                self.store.set_item(key, 0, 'running', {'native_id': native_id, 'parent_fid': directory.fid, 'name': final})
                rows = self.catalog.await_entries(parent.account, directory.fid,
                    lambda observed: len([e for e in observed if e.name == final and e.kind == 'directory']) == 1)
                matches = [e for e in rows if e.name == final and e.kind == 'directory']
                if len(matches) != 1:
                    raise VfsError('OUTCOME_UNKNOWN', 'Fresh enumeration could not identify the new directory.')
                entry = matches[0]
                self.store.set_item(key, 0, 'succeeded', {'entry': asdict(entry), 'native_id': native_id})
                self.store.set_job(key, 'succeeded')
                return entry
            except Exception as error:
                self.store.set_item(key, 0, 'needs_reconciliation', {'error_code': getattr(error, 'code', 'OUTCOME_UNKNOWN'), 'native_id': native_id})
                self.store.set_job(key, 'needs_reconciliation')
                if isinstance(error, VfsError):
                    error.job_id = key
                    raise
                raise VfsError(getattr(error, 'code', 'OUTCOME_UNKNOWN'), 'Directory creation stopped; inspect durable job facts.', job_id=key) from None

    def _rename(self, account: str, entry: RemoteEntry, name: str) -> RemoteEntry:
        if entry.parent_fid is None or entry.fid == '0':
            raise VfsError('LOCATION_UNKNOWN', 'Rename requires a known parent and a nonroot object.')
        fresh = self.catalog.resolve(FileRef(account, fid=entry.fid, parent_fid_hint=entry.parent_fid))
        check_expected(fresh, Expected.from_entry(entry))
        entry = fresh
        if entry.parent_fid is None:
            raise VfsError('LOCATION_UNKNOWN', 'Fresh rename source lacks a known parent.')
        self.catalog.assert_scope(account, fresh=True)
        outcome = self.catalog.provider.rename(account, entry.fid, entry.name, name)
        self.jobs.record({'rename_acknowledged': outcome.acknowledged, 'rename_source': asdict(entry), 'rename_target_name': name})
        if not outcome.acknowledged:
            raise VfsError('OUTCOME_UNKNOWN', 'Rename was not acknowledged.')
        rows = self.catalog.await_entries(account, entry.parent_fid,
            lambda observed: len([e for e in observed if e.name == name and e.kind == entry.kind and e.size == entry.size]) == 1
            and not any(e.name == entry.name and same_browse_identity(e.fid, entry.fid) for e in observed))
        matches = [e for e in rows if e.name == name and e.kind == entry.kind and e.size == entry.size]
        if len(matches) != 1:
            raise VfsError('OUTCOME_UNKNOWN', 'Rename could not be uniquely observed in its parent.')
        return matches[0]

    def rename(self, ref: FileRef, name: str, *, conflict: str = 'fail', expected: Expected | None = None) -> RemoteEntry:
        with self.store.account_lock(ref.account), self.catalog.scope(ref.account):
            entry = self.catalog.resolve(ref)
            self.assert_mutable(ref.account, entry.fid)
            self._check_ancestry(ref.account, entry, RemoteEntry('0', '0', '/', 'directory'))
            check_expected(entry, expected)
            if entry.parent_fid is None or entry.fid == '0':
                raise VfsError('LOCATION_UNKNOWN', 'Root or parentless objects cannot be renamed.')
            rows = [e for e in self.catalog.entries(ref.account, entry.parent_fid) if not same_browse_identity(e.fid, entry.fid)]
            name = choose_name(name, rows, conflict)
            if name == entry.name:
                return entry
            result = self.jobs.run(ref.account, 'rename', [{'source': asdict(entry), 'new_name': name}],
                                   lambda _: {'entry': asdict(self._rename(ref.account, entry, name))})
            if result.state != 'succeeded':
                raise VfsError('OUTCOME_UNKNOWN', 'Rename requires reconciliation; inspect its durable job.', job_id=result.job_id)
            return RemoteEntry(**result.items[0]['result']['entry'])

    def _check_ancestry(self, account: str, source: RemoteEntry, target: RemoteEntry) -> None:
        if source.kind != 'directory':
            return
        pending = [source.fid]
        visited: set[str] = set()
        while pending:
            fid = pending.pop()
            if same_browse_identity(fid, target.fid):
                raise VfsError('DIRECTORY_CYCLE', 'A directory cannot move into itself or a descendant.')
            if fid in visited or len(visited) >= 1000:
                raise VfsError('ANCESTRY_UNKNOWN', 'Directory ancestry exceeds the bounded verified scope.')
            visited.add(fid)
            try:
                rows = self.catalog.entries(account, fid)
                for entry in rows:
                    self.assert_mutable(account, entry.fid)
                pending.extend(e.fid for e in rows if e.kind == 'directory')
            except VfsError as error:
                if error.code == 'PROTECTED_SYSTEM_ENTRY':
                    raise
                raise VfsError('ANCESTRY_UNKNOWN', 'Cannot confirm directory ancestry.') from None
            except Exception:
                raise VfsError('ANCESTRY_UNKNOWN', 'Cannot confirm directory ancestry.') from None

    def move(self, sources: Sequence[FileRef], target: FileRef, *, conflict: str = 'fail',
             expected: dict[str, Expected] | None = None) -> BatchResult:
        if any(ref.account != target.account for ref in sources):
            raise VfsError('ACCOUNT_MISMATCH', 'Cross-account movement is not supported.')
        if len(sources) > 1000:
            raise VfsError('OPERATION_LIMIT', 'Explicit move batch is too large.')
        with self.store.account_lock(target.account), self.catalog.scope(target.account):
            destination = self.catalog.resolve(target)
            if destination.kind != 'directory':
                raise VfsError('NOT_DIRECTORY', 'Move requires an explicit target directory.')
            fixed_target = FileRef(target.account, fid=destination.fid, parent_fid_hint=destination.parent_fid)

            def execute(item: dict) -> dict:
                ref = FileRef(**item['source'])
                destination = self.catalog.resolve(fixed_target)
                entry = self.catalog.resolve(ref)
                self.assert_mutable(target.account, entry.fid)
                check_expected(entry, Expected(**item['expected']) if item.get('expected') else None)
                if entry.parent_fid is None or entry.fid == '0':
                    raise VfsError('LOCATION_UNKNOWN', 'Move requires a known nonroot source.')
                self._check_ancestry(target.account, entry, destination)
                if same_browse_identity(entry.parent_fid, destination.fid):
                    return {'entry': asdict(entry), 'already_in_target': True}
                name = choose_name(entry.name, self.catalog.entries(target.account, destination.fid), conflict)
                if name != entry.name:
                    entry = self._rename(target.account, entry, name)
                if entry.parent_fid is None:
                    raise VfsError('LOCATION_UNKNOWN', 'Renamed object parent is unverified.')
                fresh = self.catalog.resolve(FileRef(target.account, fid=entry.fid, parent_fid_hint=entry.parent_fid))
                check_expected(fresh, Expected.from_entry(entry))
                entry = fresh
                self.catalog.assert_scope(target.account, fresh=True)
                response = self.catalog.provider.move(target.account, entry.fid, destination.fid)
                self.jobs.record({'move_acknowledged': response.acknowledged, 'move_source': asdict(entry), 'move_target_fid': destination.fid})
                if not response.acknowledged:
                    raise VfsError('OUTCOME_UNKNOWN', 'Move result is not confirmed.')
                def moved_visible(rows):
                    candidates = [e for e in rows if e.name == name and e.kind == entry.kind and e.size == entry.size]
                    return len(candidates) == 1 and not any(same_browse_identity(e.fid, entry.fid)
                        for e in self.catalog.entries(target.account, entry.parent_fid or '0'))
                visible = self.catalog.await_entries(target.account, destination.fid, moved_visible)
                candidates = [e for e in visible
                              if e.name == name and e.kind == entry.kind and e.size == entry.size]
                if len(candidates) != 1:
                    raise VfsError('OUTCOME_UNKNOWN', 'Moved object cannot be uniquely observed in its destination.')
                old = self.catalog.entries(target.account, entry.parent_fid)
                if any(same_browse_identity(e.fid, entry.fid) for e in old):
                    raise VfsError('OUTCOME_UNKNOWN', 'Original source is still present after move.')
                return {'entry': asdict(candidates[0]), 'notices': list(response.notices), 'identity_stability': 'unverified'}

            def preflight(item: dict) -> None:
                destination = self.catalog.resolve(fixed_target)
                entry = self.catalog.resolve(FileRef(**item['source']))
                self.assert_mutable(target.account, entry.fid)
                check_expected(entry, Expected(**item['expected']) if item.get('expected') else None)
                if entry.fid == '0' or entry.parent_fid is None:
                    raise VfsError('LOCATION_UNKNOWN', 'Move requires a located nonroot source.')
                self._check_ancestry(target.account, entry, destination)
                if entry.parent_fid != destination.fid:
                    choose_name(entry.name, self.catalog.entries(target.account, destination.fid), conflict)

            def expectation(ref: FileRef, fresh: RemoteEntry):
                if not expected:
                    return asdict(Expected.from_entry(fresh))
                if ref.fid in expected:
                    return asdict(expected[ref.fid])
                candidates = [value for fid, value in expected.items() if same_browse_identity(fid, fresh.fid)]
                if len(candidates) > 1:
                    raise VfsError('INVALID_EXPECTATIONS', 'Multiple expectations refer to one browse identity.')
                return asdict(candidates[0]) if candidates else asdict(Expected.from_entry(fresh))

            items = []
            identities: list[str] = []
            for ref in sources:
                fresh = self.catalog.resolve(ref)
                if any(same_browse_identity(fresh.fid, seen) for seen in identities):
                    raise VfsError('INVALID_REFERENCE', 'Duplicate move sources refer to the same observed object.')
                identities.append(fresh.fid)
                fixed = FileRef(target.account, fid=fresh.fid, parent_fid_hint=fresh.parent_fid)
                items.append({'source': asdict(fixed), 'target': asdict(fixed_target), 'conflict': conflict,
                              'expected': expectation(ref, fresh)})
            return self.jobs.run(target.account, 'move', items, execute, preflight=preflight)

    def resume(self, account: str, key: str) -> dict:
        """Only never-dispatched mutation items may be rechecked and executed."""
        job = self.store.job(account, key)
        action = job['action']
        if action not in ('mkdir', 'rename', 'move'):
            raise VfsError('RESUME_UNAVAILABLE', 'No mutation dispatcher for this job type.')

        def preflight(item: dict) -> None:
            self.catalog.assert_scope(account, fresh=True)
            if action == 'mkdir':
                choose_name(item['name'], self.catalog.entries(account, item['parent_fid']), 'fail')
            else:
                source = item['source']
                if action == 'rename':
                    entry = RemoteEntry(**source)
                    ref = FileRef(account, fid=entry.fid, parent_fid_hint=entry.parent_fid)
                    fresh = self.catalog.resolve(ref)
                    check_expected(fresh, Expected.from_entry(entry))
                    rows = [e for e in self.catalog.entries(account, fresh.parent_fid or '0') if not same_browse_identity(e.fid, fresh.fid)]
                    choose_name(item['new_name'], rows, 'fail')
                else:
                    ref = FileRef(**source)
                    target = FileRef(**item['target'])
                    if ref.account != account or target.account != account:
                        raise VfsError('ACCOUNT_MISMATCH', 'Persisted move account does not match.')
                    fresh = self.catalog.resolve(ref)
                    destination = self.catalog.resolve(target)
                    check_expected(fresh, Expected(**item['expected']) if item.get('expected') else None)
                    self._check_ancestry(account, fresh, destination)
                    choose_name(fresh.name, self.catalog.entries(account, destination.fid), item['conflict'])

        def execute(item: dict) -> dict:
            if action == 'mkdir':
                # A located cached parent is required for the public mkdir interface.
                parent = FileRef(account, fid=item['parent_fid'])
                return {'entry': asdict(self.mkdir(parent, item['name']))}
            if action == 'rename':
                entry = RemoteEntry(**item['source'])
                ref = FileRef(account, fid=entry.fid, parent_fid_hint=entry.parent_fid)
                return {'entry': asdict(self.rename(ref, item['new_name'], expected=Expected.from_entry(entry)))}
            result = self.move([FileRef(**item['source'])], FileRef(**item['target']), conflict=item['conflict'],
                               expected={item['source']['fid']: Expected(**item['expected'])} if item.get('expected') else None)
            if result.state != 'succeeded':
                raise VfsError('RECONCILIATION_REQUIRED', 'Restarted mutation requires reconciliation.')
            return {'restarted_job_id': result.job_id, 'entry': result.items[0]['result']['entry']}

        return self.jobs.resume(account, key, execute=execute, preflight=preflight)

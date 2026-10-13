"""Explicit manifests, trusted-object binding and no-replace local publication."""
import ctypes
import hashlib
import os
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path

from ..models import (
    BatchResult,
    Expected,
    FileRef,
    RemoteEntry,
    TransferReceipt,
    VfsError,
    same_browse_identity,
)
from ..storage import Store
from .catalog import Catalog, secure_bytes
from .jobs import Jobs
from .operations import check_expected, choose_name, validate_name

MAX_TRANSFER_BYTES = 128 * 1024 * 1024


def _directory_fd(path: Path) -> int:
    path = path.absolute()
    if '..' in path.parts or not hasattr(os, 'O_NOFOLLOW'):
        raise VfsError('UNSAFE_LOCAL_PATH', 'Secure local directory opening unavailable.')
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise VfsError('UNSAFE_LOCAL_PATH', 'Local directory path contains a link or is unavailable.') from None


def publish_no_replace(source: Path, target: Path, *, directory: bool = False) -> None:
    """Files use atomic hardlink creation; directories use platform no-replace rename."""
    validate_name(target.name)
    source_fd, target_fd = _directory_fd(source.parent), _directory_fd(target.parent)
    try:
        if not directory:
            opened = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_fd)
            try:
                original = os.fstat(opened)
                if not stat.S_ISREG(original.st_mode):
                    raise VfsError('UNSAFE_LOCAL_PATH', 'Only regular staged files can be published.')
                os.link(source.name, target.name, src_dir_fd=source_fd, dst_dir_fd=target_fd, follow_symlinks=False)
                published = os.stat(target.name, dir_fd=target_fd, follow_symlinks=False)
                if not stat.S_ISREG(published.st_mode) or (published.st_dev, published.st_ino) != (original.st_dev, original.st_ino):
                    # Its identity no longer proves ownership. Never remove a
                    # concurrent replacement in an attempt to undo publication.
                    raise VfsError('PUBLICATION_OUTCOME_UNKNOWN', 'Published identity changed; preserve target and staging for reconciliation.')
            finally:
                os.close(opened)
            return
        libc = ctypes.CDLL(None, use_errno=True)
        if hasattr(libc, 'renameat2'):
            rename = libc.renameat2
            flags = 1  # Linux RENAME_NOREPLACE
        elif hasattr(libc, 'renameatx_np'):
            rename = libc.renameatx_np
            flags = 4  # Darwin RENAME_EXCL
        else:
            raise VfsError('PUBLICATION_UNAVAILABLE', 'No verified no-replace directory publication primitive.')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(source_fd, os.fsencode(source.name), target_fd, os.fsencode(target.name), flags) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    except FileExistsError:
        raise VfsError('NAME_CONFLICT', 'Local destination appeared before publication; no overwrite occurred.') from None
    except OSError:
        raise VfsError('PUBLICATION_FAILED', 'No-replace publication failed; staged content was retained.') from None
    finally:
        os.close(source_fd)
        os.close(target_fd)


class Transfers:
    def __init__(self, catalog: Catalog, store: Store, workspace: Path, *, excluded_local_roots: Sequence[Path] = ()):
        self.catalog, self.store = catalog, store
        self.workspace = Path(workspace).absolute()
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs = Jobs(store)
        self.excluded_local_roots = tuple(Path(root).resolve() for root in excluded_local_roots)

    def _parent(self, parent: FileRef) -> RemoteEntry:
        entry = self.catalog.resolve(parent)
        if entry.kind != 'directory':
            raise VfsError('NOT_DIRECTORY', 'Transfers require an explicit remote parent directory.')
        return entry

    def _upload_file(self, source: Path, parent: FileRef, conflict: str, *, expected_sha256: str | None = None,
                     record: Callable[[dict], None] | None = None) -> TransferReceipt:
        source = source.absolute()
        if source.is_file() and source.stat().st_size > MAX_TRANSFER_BYTES:
            raise VfsError('TRANSFER_SIZE_LIMIT', 'This initial transfer implementation supports files up to 128 MiB.')
        try:
            content, fingerprint = secure_bytes(source, source.parent, max_bytes=MAX_TRANSFER_BYTES)
        except VfsError:
            raise
        if expected_sha256 is not None and hashlib.sha256(content).hexdigest() != expected_sha256:
            raise VfsError('SOURCE_CHANGED', 'Source bytes no longer match the authorized upload manifest.')
        def observed(**facts):
            if record is not None:
                record(facts)
        directory = self._parent(parent)
        name = choose_name(source.name, self.catalog.entries(parent.account, directory.fid), conflict)
        # The CLI derives the remote name from the local basename. Send an immutable
        # private copy, not the potentially changing user's source path.
        stage = Path(tempfile.mkdtemp(prefix='upload-', dir=self.workspace))
        prepared = stage / name
        with prepared.open('xb') as stream:
            os.chmod(prepared, 0o600)
            stream.write(content)
        observed(source_fingerprint=fingerprint, source_sha256=hashlib.sha256(content).hexdigest(),
                 upload_stage=str(stage), immutable_source=str(prepared), target_parent_fid=directory.fid, target_name=name)
        self.catalog.assert_scope(parent.account, fresh=True)
        result = self.catalog.provider.upload_file(parent.account, prepared, directory.fid)
        observed(native_upload_id=result.native_id, record_id=result.record_id, upload_outcome=result.outcome,
                 upload_name=result.name, upload_size=result.size)
        after, after_fingerprint = secure_bytes(source, source.parent, max_bytes=MAX_TRANSFER_BYTES)
        if after != content or fingerprint != after_fingerprint:
            raise VfsError('SOURCE_CHANGED', 'Local source changed while uploading; reconcile the uploaded object.')
        if result.outcome != 'success' or result.name != name or result.size != len(content):
            raise VfsError('UPLOAD_PARTIAL', 'Upload item is not fully confirmed; it will not be automatically retried.')
        visible = self.catalog.await_entries(parent.account, directory.fid,
            lambda rows: len([e for e in rows if e.name == name and e.kind == 'file' and e.size == len(content)]) == 1)
        candidates = [e for e in visible
                      if e.name == name and e.kind == 'file' and e.size == len(content)]
        if len(candidates) != 1:
            raise VfsError('OUTCOME_UNKNOWN', 'Upload native identity cannot be uniquely bound to a fresh remote object.')
        remote = candidates[0]
        verify_stage = Path(tempfile.mkdtemp(prefix='upload-check-', dir=self.workspace))
        observed(fresh_candidate=asdict(remote), verification_stage=str(verify_stage))
        downloaded = self.catalog.provider.download_file(parent.account, remote.fid, verify_stage)
        if downloaded.name != name or Path(downloaded.path) != verify_stage / name:
            raise VfsError('UNSAFE_LOCAL_PATH', 'Upload verification path is outside staging.')
        verified, _ = secure_bytes(downloaded.path, verify_stage, max_bytes=MAX_TRANSFER_BYTES)
        if verified != content:
            raise VfsError('CONTENT_MISMATCH', 'Upload candidate did not reproduce the source bytes.')
        digest = hashlib.sha256(content).hexdigest()
        # Bind trusted content only to this scope, exact observed identity and metadata.
        self.store.put_document(parent.account, 'trusted_hash', remote.fid,
                                {'sha256': digest, 'entry': asdict(remote), 'native_id': result.native_id})
        observed(trusted_sha256=digest, candidate_bytes_verified=True)
        shutil.rmtree(stage)
        shutil.rmtree(verify_stage)
        observed(temporary_copies_removed=True)
        return TransferReceipt(remote, None, ('size_checked', 'trusted_hash_verified'), digest,
                               result.record_id, result.notices + downloaded.notices)

    def upload_file(self, source: Path, parent: FileRef, *, conflict: str = 'fail') -> TransferReceipt:
        with self.store.account_lock(parent.account), self.catalog.scope(parent.account):
            if source.is_file() and source.stat().st_size > MAX_TRANSFER_BYTES:
                raise VfsError('TRANSFER_SIZE_LIMIT', 'This initial transfer implementation supports files up to 128 MiB.')
            data, _ = secure_bytes(source, source.absolute().parent, max_bytes=MAX_TRANSFER_BYTES)
            digest = hashlib.sha256(data).hexdigest()
            directory = self._parent(parent)
            fixed_parent = FileRef(parent.account, fid=directory.fid, parent_fid_hint=directory.parent_fid)
            key = self.store.create_job(parent.account, 'upload', [{'source': str(source.absolute()), 'parent': asdict(fixed_parent),
                'conflict': conflict, 'relative': source.name, 'directory': False, 'source_sha256': digest}])
            self.store.set_job(key, 'running')
            self.store.set_item(key, 0, 'running')
            evidence: dict = {}
            def record(facts):
                evidence.update(facts)
                self.store.set_item(key, 0, 'running', {'phase': 'dispatched', 'evidence': evidence})
            try:
                receipt = self._upload_file(source, fixed_parent, conflict, expected_sha256=digest, record=record)
                assert receipt.entry is not None
                self.store.set_item(key, 0, 'succeeded', {'entry': asdict(receipt.entry), 'sha256': receipt.sha256, 'evidence': evidence})
                self.store.set_job(key, 'succeeded')
                return TransferReceipt(receipt.entry, receipt.local_path, receipt.verification_levels, receipt.sha256,
                                       receipt.record_id, receipt.notices, receipt.excluded, key)
            except Exception as error:
                self.store.set_item(key, 0, 'needs_reconciliation', {'error_code': getattr(error, 'code', 'OUTCOME_UNKNOWN'), 'evidence': evidence})
                self.store.set_job(key, 'needs_reconciliation')
                if isinstance(error, VfsError):
                    error.job_id = key
                    raise
                raise VfsError(getattr(error, 'code', 'OUTCOME_UNKNOWN'), 'Upload stopped; inspect durable job facts.', job_id=key) from None

    def upload(self, local_paths: Sequence[Path], parent: FileRef, *, conflict: str = 'fail', exclude_secrets: bool = True) -> BatchResult:
        manifest: list[dict] = []
        excluded: list[str] = []
        secret_names = {'.env', 'credentials', 'credentials.json', 'auth.json', 'token.json', 'tokens.json',
                        'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa', '.netrc',
                        '.codex', '.aws', '.ssh', '.gnupg', '.quarkclouddrive'}

        def secret(path: Path) -> bool:
            return exclude_secrets and (path.name in secret_names or path.name.startswith('.env.') or path.suffix.lower() in ('.pem', '.key', '.p12', '.pfx', '.ppk'))

        def private(path: Path) -> bool:
            resolved = path.resolve()
            return any(resolved == root or root in resolved.parents for root in self.excluded_local_roots)

        for raw in local_paths:
            source = Path(raw).absolute()
            if source.is_symlink() or not source.exists():
                raise VfsError('UNSAFE_LOCAL_PATH', 'Explicit upload source is missing or a symbolic link.')
            source_fd = _directory_fd(source.parent)
            os.close(source_fd)
            if secret(source) or private(source):
                excluded.append(source.name)
                manifest.append({'source': str(source), 'relative': source.name, 'excluded': True})
                continue
            candidates = [source] if source.is_file() else []
            if source.is_dir():
                for current, names, filenames in os.walk(source, topdown=True, followlinks=False):
                    current_path = Path(current)
                    for name in sorted(names):
                        child = current_path / name
                        candidates.append(child)
                        if secret(child) or private(child) or child.is_symlink():
                            names.remove(name)
                    candidates.extend(current_path / name for name in sorted(filenames))
            if source.is_dir():
                manifest.append({'source': str(source), 'relative': source.name, 'directory': True})
            for path in candidates:
                if path.is_symlink():
                    raise VfsError('UNSAFE_LOCAL_PATH', 'Upload manifests never follow symbolic links.')
                relative = str(Path(source.name) / path.relative_to(source)) if source.is_dir() else source.name
                ancestors = path.relative_to(source).parents if source.is_dir() else ()
                if private(path) or secret(path) or any(secret(source / p) for p in ancestors if p != Path('.')):
                    excluded.append(relative)
                    manifest.append({'source': str(path), 'relative': relative, 'excluded': True})
                    continue
                if path.is_file() and path.stat().st_size > MAX_TRANSFER_BYTES:
                    raise VfsError('TRANSFER_SIZE_LIMIT', 'Upload manifest contains a file larger than 128 MiB.')
                if not path.is_file() and not path.is_dir():
                    raise VfsError('UNSAFE_LOCAL_PATH', 'Manifest entries must be regular files or directories.')
                manifest.append({'source': str(path), 'relative': relative, 'directory': path.is_dir()})
        if len(manifest) > 10000:
            raise VfsError('OPERATION_LIMIT', 'Upload manifest exceeds the bounded item limit.')
        if not manifest:
            raise VfsError('SOURCE_MISSING', 'Specify at least one local source.')
        if all(item.get('excluded') for item in manifest):
            raise VfsError('EXCLUDED_SOURCE', 'All explicit sources were excluded by the visible privacy policy; no upload was attempted.')
        for item in manifest:
            item['parent'] = asdict(parent)
            item['conflict'] = conflict
            if not item.get('directory') and not item.get('excluded'):
                content, _ = secure_bytes(Path(item['source']), Path(item['source']).parent, max_bytes=MAX_TRANSFER_BYTES)
                item['source_sha256'] = hashlib.sha256(content).hexdigest()
        with self.store.account_lock(parent.account), self.catalog.scope(parent.account):
            destination = self._parent(parent)
            fixed_parent = FileRef(parent.account, fid=destination.fid, parent_fid_hint=destination.parent_fid)
            for item in manifest:
                item['parent'] = asdict(fixed_parent)
            directories: dict[str, RemoteEntry] = {'': destination}

            def execute(item: dict) -> dict:
                if item.get('excluded'):
                    return {'outcome': 'excluded', 'excluded': [item['relative']]}
                relative = Path(item['relative'])
                remote_parent = directories.get(str(relative.parent) if str(relative.parent) != '.' else '')
                if remote_parent is None:
                    raise VfsError('LOCATION_UNKNOWN', 'A manifest parent directory was not successfully created.')
                ref = FileRef(parent.account, fid=remote_parent.fid, parent_fid_hint=remote_parent.parent_fid)
                if item['directory']:
                    rows = self.catalog.entries(parent.account, remote_parent.fid)
                    name = choose_name(relative.name, rows, conflict)
                    self.catalog.assert_scope(parent.account, fresh=True)
                    native = self.catalog.provider.mkdir(parent.account, remote_parent.fid, name)
                    self.jobs.record({'native_directory_id': native, 'target_parent_fid': remote_parent.fid, 'target_name': name})
                    visible = self.catalog.await_entries(parent.account, remote_parent.fid,
                        lambda rows: len([e for e in rows if e.name == name and e.kind == 'directory']) == 1)
                    matches = [e for e in visible if e.name == name and e.kind == 'directory']
                    if len(matches) != 1:
                        raise VfsError('OUTCOME_UNKNOWN', 'Manifest directory creation is not uniquely observed.')
                    directories[str(relative)] = matches[0]
                    return {'entry': asdict(matches[0]), 'excluded': excluded}
                receipt = self._upload_file(Path(item['source']), ref, conflict, expected_sha256=item['source_sha256'], record=self.jobs.record)
                assert receipt.entry is not None
                return {'entry': asdict(receipt.entry), 'sha256': receipt.sha256, 'excluded': excluded}

            def preflight(item: dict) -> None:
                if item.get('excluded'):
                    return
                if not item.get('directory'):
                    content, _ = secure_bytes(Path(item['source']), Path(item['source']).parent, max_bytes=MAX_TRANSFER_BYTES)
                    if hashlib.sha256(content).hexdigest() != item['source_sha256']:
                        raise VfsError('SOURCE_CHANGED', 'Manifest source changed before dispatch.')
                relative = Path(item['relative'])
                remote_parent = directories.get(str(relative.parent) if str(relative.parent) != '.' else '')
                if remote_parent is None:
                    raise VfsError('LOCATION_UNKNOWN', 'Manifest parent was not successfully created.')
                choose_name(relative.name, self.catalog.entries(parent.account, remote_parent.fid), conflict)

            return self.jobs.run(parent.account, 'upload', manifest, execute, preflight=preflight)

    def resume_upload(self, account: str, key: str) -> dict:
        """Dispatch only pending or strict preflight-failed manifest items."""
        self.catalog.assert_scope(account, fresh=True)
        job = self.store.job(account, key)
        if job['action'] != 'upload':
            raise VfsError('RESUME_UNAVAILABLE', 'This dispatcher only resumes explicit upload manifests.')
        directories: dict[str, RemoteEntry] = {}
        for item in job['items']:
            request = item['input']
            if item['state'] == 'succeeded' and request.get('directory') and item['result']:
                directories[request['relative']] = RemoteEntry(**item['result']['entry'])

        def parent_for(item: dict) -> RemoteEntry:
            ref = FileRef(**item['parent'])
            if ref.account != account:
                raise VfsError('ACCOUNT_MISMATCH', 'Upload checkpoint scope does not match.')
            relative = Path(item.get('relative', Path(item['source']).name))
            prefix = str(relative.parent) if str(relative.parent) != '.' else ''
            if not prefix:
                return self._parent(ref)
            if prefix not in directories:
                raise VfsError('LOCATION_UNKNOWN', 'Upload checkpoint lacks its completed parent directory.')
            old = directories[prefix]
            return self.catalog.resolve(FileRef(account, fid=old.fid, parent_fid_hint=old.parent_fid))

        def preflight(item: dict) -> None:
            self.catalog.assert_scope(account, fresh=True)
            if item.get('excluded'):
                return
            parent = parent_for(item)
            source = Path(item['source'])
            if not item.get('directory'):
                data, _ = secure_bytes(source, source.parent, max_bytes=MAX_TRANSFER_BYTES)
                if not item.get('source_sha256') or hashlib.sha256(data).hexdigest() != item['source_sha256']:
                    raise VfsError('SOURCE_CHANGED', 'Persisted upload manifest source no longer matches.')
            choose_name(Path(item.get('relative', source.name)).name, self.catalog.entries(account, parent.fid), item.get('conflict', 'fail'))

        def execute(item: dict) -> dict:
            if item.get('excluded'):
                return {'outcome': 'excluded', 'excluded': [item['relative']]}
            parent = parent_for(item)
            ref = FileRef(account, fid=parent.fid, parent_fid_hint=parent.parent_fid)
            if item.get('directory'):
                entry = self.catalog.resolve(FileRef(account, fid=parent.fid, parent_fid_hint=parent.parent_fid))
                name = choose_name(Path(item['relative']).name, self.catalog.entries(account, entry.fid), item.get('conflict', 'fail'))
                self.catalog.assert_scope(account, fresh=True)
                native = self.catalog.provider.mkdir(account, entry.fid, name)
                self.jobs.record({'native_directory_id': native, 'target_parent_fid': entry.fid, 'target_name': name})
                visible = self.catalog.await_entries(account, entry.fid,
                    lambda rows: len([e for e in rows if e.name == name and e.kind == 'directory']) == 1)
                candidates = [e for e in visible if e.name == name and e.kind == 'directory']
                if len(candidates) != 1:
                    raise VfsError('OUTCOME_UNKNOWN', 'Resumed upload directory is not uniquely observed.')
                directories[item['relative']] = candidates[0]
                return {'entry': asdict(candidates[0])}
            receipt = self._upload_file(Path(item['source']), ref, item.get('conflict', 'fail'), expected_sha256=item['source_sha256'], record=self.jobs.record)
            assert receipt.entry is not None
            return {'entry': asdict(receipt.entry), 'sha256': receipt.sha256}

        return self.jobs.resume(account, key, execute=execute, preflight=preflight)

    def _download_file(self, ref: FileRef, entry: RemoteEntry, staging: Path, *, trusted_sha256: str | None = None) -> TransferReceipt:
        validate_name(entry.name)
        if entry.size is not None and entry.size > MAX_TRANSFER_BYTES:
            raise VfsError('TRANSFER_SIZE_LIMIT', 'This initial transfer implementation supports files up to 128 MiB.')
        response = self.catalog.provider.download_file(ref.account, entry.fid, staging)
        if response.name != entry.name or Path(response.path) != staging / entry.name:
            raise VfsError('UNSAFE_LOCAL_PATH', 'Provider download path is outside its private staging directory.')
        content, _ = secure_bytes(response.path, staging, max_bytes=MAX_TRANSFER_BYTES)
        if entry.size is not None and len(content) != entry.size:
            raise VfsError('CONTENT_MISMATCH', 'Downloaded size differs from the fresh remote metadata.')
        digest = hashlib.sha256(content).hexdigest()
        levels = ['local_fingerprint_recorded']
        if entry.size is not None:
            levels.append('size_checked')
        if trusted_sha256 is not None:
            if digest != trusted_sha256:
                raise VfsError('CONTENT_MISMATCH', 'Downloaded content does not match the explicitly trusted hash.')
            levels.append('trusted_hash_verified')
        # Check the known location again. Unknown hash remains only a local fingerprint.
        fresh = self.catalog.resolve(ref)
        if (fresh.name, fresh.kind, fresh.size, fresh.modified, fresh.content_hash) != (entry.name, entry.kind, entry.size, entry.modified, entry.content_hash):
            raise VfsError('EXPECTED_CHANGED', 'Remote metadata changed during download.')
        return TransferReceipt(entry, Path(response.path), tuple(levels), digest, response.record_id, response.notices)

    def download(self, ref: FileRef, target: Path, *, conflict: str = 'fail', trusted_sha256: str | None = None) -> TransferReceipt:
        target = Path(target).absolute()
        with self.store.account_lock(ref.account), self.catalog.scope(ref.account):
            entry = self.catalog.resolve(ref)
            if entry.kind == 'directory' and trusted_sha256 is not None:
                raise VfsError('TRUSTED_HASH_UNAVAILABLE', 'No directory digest schema is verified; a file SHA-256 cannot validate a directory.')
            fixed_ref = FileRef(ref.account, fid=entry.fid, parent_fid_hint=entry.parent_fid)
            validate_name(target.name)
            fd = _directory_fd(target.parent)
            os.close(fd)
            occupied = [RemoteEntry('local-' + str(i), 'local', name, 'file') for i, name in enumerate(os.listdir(target.parent))]
            target = target.with_name(choose_name(target.name, occupied, conflict))
            # Adjacent staging guarantees the no-replace publication is on one filesystem.
            stage = Path(tempfile.mkdtemp(prefix='.quark-vfs-', dir=target.parent))
            key = self.store.create_job(ref.account, 'download', [{'source': asdict(fixed_ref), 'target': str(target),
                'expected': asdict(Expected.from_entry(entry)), 'trusted_sha256': trusted_sha256}])
            self.store.set_job(key, 'running')
            self.store.set_item(key, 0, 'running')
            try:
                if entry.kind == 'file':
                    receipt = self._download_file(fixed_ref, entry, stage, trusted_sha256=trusted_sha256)
                    assert receipt.local_path is not None
                    publish_no_replace(receipt.local_path, target)
                else:
                    payload = stage / 'payload'
                    payload.mkdir(mode=0o700)
                    count = [0]
                    excluded: list[str] = []
                    self._download_tree(ref.account, entry.fid, payload, count, set(), excluded)
                    publish_no_replace(payload, target, directory=True)
                    receipt = TransferReceipt(entry, target, ('directory_manifest_completed',), excluded=tuple(excluded))
                self.store.set_item(key, 0, 'succeeded', {'local_path': str(target), 'sha256': receipt.sha256,
                                                        'verification_levels': list(receipt.verification_levels)})
                self.store.set_job(key, 'succeeded')
                shutil.rmtree(stage)
                return TransferReceipt(entry, target, receipt.verification_levels, receipt.sha256, receipt.record_id,
                                       receipt.notices, receipt.excluded, key)
            except Exception as error:
                self.store.set_item(key, 0, 'needs_reconciliation', {'error_code': getattr(error, 'code', 'OUTCOME_UNKNOWN'), 'staging': str(stage)})
                self.store.set_job(key, 'needs_reconciliation')
                if isinstance(error, VfsError):
                    error.job_id = key
                    raise
                raise VfsError(getattr(error, 'code', 'OUTCOME_UNKNOWN'), 'Download stopped; inspect durable job facts.', job_id=key) from None

    def resume_download(self, account: str, key: str) -> dict:
        """Recheck a persisted download and restart into new staging, never an offset resume."""
        self.catalog.assert_scope(account, fresh=True)
        job = self.store.job(account, key)
        if job['action'] != 'download':
            raise VfsError('RESUME_UNAVAILABLE', 'This dispatcher only restarts downloads.')

        def preflight(item: dict) -> None:
            self.catalog.assert_scope(account, fresh=True)
            ref = FileRef(**item['source'])
            if ref.account != account:
                raise VfsError('ACCOUNT_MISMATCH', 'Persisted download scope changed.')
            target = Path(item['target'])
            if not target.is_absolute() or os.path.lexists(target):
                raise VfsError('NAME_CONFLICT', 'Download restart requires an absent explicit target.')
            fd = _directory_fd(target.parent)
            os.close(fd)
            entry = self.catalog.resolve(ref)
            check_expected(entry, Expected(**item['expected']))

        def execute(item: dict) -> dict:
            receipt = self.download(FileRef(**item['source']), Path(item['target']),
                                    conflict='fail', trusted_sha256=item.get('trusted_sha256'))
            return {'local_path': str(receipt.local_path), 'sha256': receipt.sha256,
                    'restarted_job_id': receipt.job_id, 'restart_mode': 'new_staging_not_offset_resume'}

        return self.jobs.resume(account, key, execute=execute, preflight=preflight)

    def _download_tree(self, account: str, fid: str, output: Path, count: list[int], seen: set[str], excluded: list[str]) -> None:
        if fid in seen or count[0] > 10000:
            raise VfsError('OPERATION_LIMIT', 'Directory traversal cycle or item limit.')
        seen.add(fid)
        rows = self.catalog.entries(account, fid)
        local_names: set[str] = set()
        managed = {doc['fid'] for doc in self.store.list_documents(account, 'management') if isinstance(doc.get('fid'), str)}
        managed.update(doc['trash_root_fid'] for doc in self.store.list_documents(account, 'trash') if isinstance(doc.get('trash_root_fid'), str))
        for entry in rows:
            count[0] += 1
            if count[0] > 10000:
                raise VfsError('OPERATION_LIMIT', 'Directory manifest exceeds 10000 entries.')
            validate_name(entry.name)
            normalized = unicodedata.normalize('NFC', entry.name).casefold()
            if normalized in local_names:
                raise VfsError('NAME_CONFLICT', 'Remote names cannot be mapped uniquely to portable local names.')
            local_names.add(normalized)
            if entry.kind == 'directory' and any(same_browse_identity(entry.fid, managed_fid) for managed_fid in managed):
                excluded.append(entry.name)
                continue
            if entry.kind == 'directory':
                child = output / entry.name
                child.mkdir(mode=0o700)
                self._download_tree(account, entry.fid, child, count, seen, excluded)
            else:
                private = Path(tempfile.mkdtemp(prefix='.file-', dir=output))
                receipt = self._download_file(FileRef(account, fid=entry.fid, parent_fid_hint=fid), entry, private)
                assert receipt.local_path is not None
                publish_no_replace(receipt.local_path, output / entry.name)
                shutil.rmtree(private)
        fresh = self.catalog.entries(account, fid)
        def observed(entries):
            return sorted((e.fid.rsplit('|', 1)[-1] if '|' in e.fid and e.fid.rsplit('|', 1)[-1] else e.fid,
                           e.name, e.kind, e.size, e.modified, e.content_hash) for e in entries)
        if observed(fresh) != observed(rows):
            raise VfsError('SOURCE_CHANGED', 'Remote directory changed during its download manifest.')

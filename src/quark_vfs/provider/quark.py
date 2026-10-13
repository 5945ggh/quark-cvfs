"""Official structured adapter; fresh object semantics belong to the shared core."""
import json
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..models import DownloadResult, MutationResult, RemoteEntry, UploadResult
from .auth import sanitize_notices
from .protocol import (
    ProtocolError,
    ProtocolTranscript,
    parse_ndjson,
    read_artifact_jsonl,
)
from .quark_cli import (
    AcceptanceCall,
    OfficialResult,
    ProviderCall,
    ProviderError,
    run_official,
)


class QuarkProvider:
    def __init__(self, config: Config, *, runner: Callable = run_official):
        self.config = config
        self.runner = runner
        self.last_notices: tuple[str, ...] = ()

    def account_identity(self) -> None:
        # The supported public account response exposes nickname, not a stable account ID.
        return None

    def _response(self, request: ProviderCall | AcceptanceCall, action: str) -> tuple[dict, ProtocolTranscript, int]:
        output: OfficialResult = self.runner(self.config, request)
        try:
            transcript = parse_ndjson(output.stdout.decode('utf-8', errors='strict'))
        except (UnicodeError, ProtocolError):
            raise ProviderError('OUTCOME_UNKNOWN', 'Official output schema is unrecognized; do not repeat side effects.') from None
        self.last_notices = sanitize_notices(transcript.notices)
        if len(transcript.results) != 1:
            raise ProviderError('OUTCOME_UNKNOWN', 'Official final result is missing.', notices=self.last_notices)
        result = transcript.results[0]
        if result.get('action') == 'not_authenticated' and result.get('code') == -1408:
            raise ProviderError('AUTH_REQUIRED', 'Official browser authorization is required.', notices=self.last_notices)
        if result.get('action') != action or type(result.get('code')) is not int:
            raise ProviderError('OUTCOME_UNKNOWN', 'Official final result schema is unrecognized.', notices=self.last_notices)
        return result, transcript, output.exit_code

    def _successful(self, request: ProviderCall | AcceptanceCall, action: str) -> tuple[dict, ProtocolTranscript]:
        result, transcript, exit_code = self._response(request, action)
        if result['code'] != 0 or exit_code != 0:
            error = ProviderError('UPSTREAM_FAILED' if result['code'] != 0 else 'OUTCOME_UNKNOWN',
                                  'Official operation was not confirmed; inspect recorded state before retrying.', notices=self.last_notices)
            error.upstream_code = result['code']
            error.exit_code = exit_code
            raise error
        if not isinstance(result.get('data'), dict):
            raise ProviderError('UNKNOWN_SCHEMA', 'Official result data schema is unrecognized.', notices=self.last_notices)
        return result['data'], transcript

    def _task_directory(self) -> Path:
        path = self.config.cache_dir.resolve() / ('provider-' + str(uuid.uuid4()))
        path.mkdir(mode=0o700)
        return path

    def _entry(self, row: dict, scope: str | None) -> RemoteEntry:
        if (not isinstance(row.get('fid'), str) or not row['fid']
                or not isinstance(row.get('filename'), str) or not row['filename']
                or not isinstance(row.get('parent_fid'), str) or not row['parent_fid']
                or row.get('file_type') not in ('0', '1')):
            raise ProviderError('UNKNOWN_SCHEMA', 'Remote entry identity schema is unrecognized.')
        size = row.get('size')
        if size is not None and (type(size) is not int or size < 0):
            raise ProviderError('UNKNOWN_SCHEMA', 'Remote entry size is unrecognized.')
        opaque_hash = row.get('content_hash')
        if opaque_hash is not None and not isinstance(opaque_hash, str):
            raise ProviderError('UNKNOWN_SCHEMA', 'Remote opaque hash is unrecognized.')
        modified = row.get('updated_at')
        if modified is not None and (type(modified) not in (int, str) or modified == ''):
            raise ProviderError('UNKNOWN_SCHEMA', 'Remote modification timestamp is unrecognized.')
        return RemoteEntry(fid=row['fid'], parent_fid=scope, name=row['filename'],
                           kind='directory' if row['file_type'] == '0' else 'file', size=size,
                           content_hash=opaque_hash, modified=str(modified) if modified is not None else None,
                           observed_at=datetime.now(UTC).isoformat(),
                           raw_parent_fid=row['parent_fid'])

    def list_directory(self, account: str, parent_fid: str) -> Sequence[RemoteEntry]:
        output = self._task_directory()
        try:
            data, _ = self._successful(ProviderCall('browse', parent_fid=parent_fid, local_path=output), 'browse')
            if (data.get('parent_fid') != parent_fid or data.get('hasMore') is not False
                    or type(data.get('count')) is not int or not isinstance(data.get('file_path'), str)):
                raise ProviderError('INCOMPLETE_ENUMERATION', 'Full direct enumeration has not been confirmed.')
            rows = read_artifact_jsonl(data['file_path'], allowed_output_dir=output,
                                       expected_count=data['count'], required_fields=('fid', 'filename', 'file_type', 'parent_fid'))
            entries = tuple(self._entry(row, parent_fid) for row in rows)
            if len({entry.fid for entry in entries}) != len(entries):
                raise ProviderError('INCOMPLETE_ENUMERATION', 'Directory contains duplicate complete identities.')
            return entries
        except ProtocolError as error:
            raise ProviderError(error.code, 'Full directory artifact could not be safely verified.') from None
        finally:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ProviderError('PATH_CHECK_UNAVAILABLE', 'Secure output cleanup is unavailable.')
            shutil.rmtree(output)

    def search(self, account: str, keyword: str, parent_fid: str | None = None) -> Sequence[RemoteEntry]:
        output_root = self.config.official_root / 'codex' / 'search'
        before = self._search_outputs(output_root)
        _, transcript = self._successful(ProviderCall('search', keyword=keyword, parent_fid=parent_fid), 'search')
        if len(transcript.artifacts) != 1:
            raise ProviderError('UNKNOWN_SCHEMA', 'Search full result artifact is missing.')
        artifact = transcript.artifacts[0]
        data = artifact.get('data')
        if (artifact.get('action') != 'search' or type(artifact.get('code')) is not int or artifact['code'] != 0
                or not isinstance(data, dict) or type(data.get('count')) is not int
                or data.get('format') != 'jsonl' or not isinstance(data.get('file_path'), str)):
            raise ProviderError('UNKNOWN_SCHEMA', 'Search artifact schema is unrecognized.')
        try:
            candidate = Path(data['file_path'])
            after = self._search_outputs(output_root)
            new_outputs = {path for path in after if path not in before or before[path] != after[path]}
            if new_outputs != {candidate}:
                raise ProviderError('OUTCOME_UNKNOWN', 'Search artifact cannot be uniquely associated with this invocation.')
            rows = read_artifact_jsonl(data['file_path'], allowed_output_dir=output_root,
                                       expected_count=data['count'], required_fields=('fid', 'filename', 'file_type', 'parent_fid'))
        except ProtocolError as error:
            raise ProviderError(error.code, 'Search artifact could not be safely verified.') from None
        # Scope filters do not prove direct-child placement or search completeness.
        return tuple(self._entry(row, None) for row in rows)

    def _search_outputs(self, root: Path) -> dict[Path, tuple[int, ...]]:
        if not root.exists():
            return {}
        descriptor = self._secure_directory(root)
        os.close(descriptor)
        files: dict[Path, tuple[int, ...]] = {}
        for directory, _, names, current in os.fwalk(root, follow_symlinks=False):
            for name in names:
                if not name.endswith('.jsonl'):
                    continue
                metadata = os.stat(name, dir_fd=current, follow_symlinks=False)
                if stat.S_ISREG(metadata.st_mode):
                    files[Path(directory) / name] = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
        return files

    @staticmethod
    def _secure_directory(path: Path) -> int:
        if not path.is_absolute() or '..' in path.parts or not hasattr(os, 'O_NOFOLLOW'):
            raise ProviderError('UNSAFE_LOCAL_PATH', 'Local directory must be a safe absolute path.')
        descriptors = []
        try:
            descriptors.append(os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY))
            for part in path.parts[1:]:
                descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptors[-1]))
            return os.dup(descriptors[-1])
        except OSError:
            raise ProviderError('UNSAFE_LOCAL_PATH', 'Local directory contains a link or cannot be securely opened.') from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def mkdir(self, account: str, parent_fid: str, name: str) -> str:
        data, _ = self._successful(ProviderCall('create-folder', parent_fid=parent_fid, name=name), 'create-folder')
        if not isinstance(data.get('fid'), str) or not data['fid'] or data['fid'] == '0':
            raise ProviderError('OUTCOME_UNKNOWN', 'Directory response identity is missing; do not repeat creation.')
        return data['fid']  # Source domain: create-folder service response, not a browse identity promise.

    def upload_file(self, account: str, source: Path, parent_fid: str) -> UploadResult:
        if source.is_symlink() or not source.is_file() or not source.is_absolute():
            raise ProviderError('UNSAFE_LOCAL_PATH', 'Upload source must be an explicit absolute regular file.')
        result, transcript, exit_code = self._response(ProviderCall('upload', parent_fid=parent_fid, local_path=source), 'upload')
        if len(transcript.lists) != 1:
            raise ProviderError('OUTCOME_UNKNOWN', 'Single-file upload did not provide exactly one item outcome.')
        item = transcript.lists[0]
        data = item.get('data')
        if (item.get('action') != 'upload' or type(item.get('code')) is not int or not isinstance(data, dict)
                or (item['code'] == 0 and data.get('fileName') != source.name)
                or (item['code'] != 0 and data.get('fileName') not in (None, source.name))):
            raise ProviderError('OUTCOME_UNKNOWN', 'Upload item schema is unrecognized.')
        native_id = data.get('fileId')
        record_id = data.get('recordId')
        size = data.get('fileSize')
        if ((native_id is not None and (not isinstance(native_id, str) or not native_id))
                or (record_id is not None and (not isinstance(record_id, str) or not record_id))
                or (size is not None and (type(size) is not int or size < 0))):
            raise ProviderError('OUTCOME_UNKNOWN', 'Upload item identity schema is unrecognized.')
        final = result.get('data')
        success = (item['code'] == 0 and native_id is not None and size == source.stat().st_size
                   and result['code'] == 0 and exit_code == 0 and isinstance(final, dict)
                   and type(final.get('fileCount')) is int and final['fileCount'] == 1
                   and type(final.get('successCount')) is int and final['successCount'] == 1
                   and final.get('fids') == [native_id])
        outcome = 'success' if success else ('partial' if item['code'] == 0 else 'failed')
        return UploadResult(native_id, record_id, size, source.name, outcome, self.last_notices)

    def _safe_download_result(self, data: dict, staging: Path) -> DownloadResult:
        name, value = data.get('fileName'), data.get('filePath')
        if (not isinstance(name, str) or not name or name in ('.', '..')
                or any(c in name for c in ('/', '\\', '\x00')) or not isinstance(value, str)
                or Path(value) != staging / name or not staging.is_absolute()):
            raise ProviderError('UNSAFE_DOWNLOAD_PATH', 'Returned download path is not the requested staging file.')
        handles = []
        try:
            handles.append(os.open(staging.anchor, os.O_RDONLY | os.O_DIRECTORY))
            for part in staging.parts[1:]:
                handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            handles.append(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handles[-1]))
            if not stat.S_ISREG(os.fstat(handles[-1]).st_mode):
                raise ProviderError('UNSAFE_DOWNLOAD_PATH', 'Downloaded object is not a regular file.')
        except OSError:
            raise ProviderError('UNSAFE_DOWNLOAD_PATH', 'Downloaded file cannot be safely opened.') from None
        finally:
            for handle in reversed(handles):
                os.close(handle)
        record = data.get('recordId')
        if record is not None and not isinstance(record, str):
            raise ProviderError('UNKNOWN_SCHEMA', 'Download task identity is unrecognized.')
        return DownloadResult(Path(value), name, record, self.last_notices)

    def download_file(self, account: str, fid: str, staging: Path) -> DownloadResult:
        descriptor = self._secure_directory(staging)
        try:
            if os.listdir(descriptor):
                raise ProviderError('UNSAFE_LOCAL_PATH', 'Download staging must be a fresh empty absolute directory.')
        finally:
            os.close(descriptor)
        data, _ = self._successful(ProviderCall('download', fids=(fid,), local_path=staging), 'download')
        return self._safe_download_result(data, staging)

    def move(self, account: str, fid: str, target_fid: str) -> MutationResult:
        data, _ = self._successful(ProviderCall('move', fids=(fid,), target_fid=target_fid), 'move')
        if data.get('fids') != [fid] or data.get('targetFid') != target_fid:
            raise ProviderError('OUTCOME_UNKNOWN', 'Move acknowledgement does not match the request.')
        return MutationResult(True, self.last_notices)

    def rename(self, account: str, fid: str, old_name: str, new_name: str) -> MutationResult:
        if not old_name or not new_name or new_name in ('.', '..') or len(new_name.encode('utf-16-le')) // 2 > 255 or any(c in new_name for c in ('/', '\\', '\x00')):
            raise ProviderError('INVALID_ARGUMENT', 'Rename requires an explicit old name and a safe new filename.')
        output = self._task_directory()
        batch = uuid.uuid4().hex[:16]
        items = output / 'items.json'
        try:
            with items.open('x', encoding='utf-8') as stream:
                os.chmod(items, 0o600)
                json.dump({'schema_version': 1, 'items': [{'fid': fid, 'old_name': old_name, 'new_name': new_name}]}, stream)
            data, _ = self._successful(ProviderCall('rename', local_path=items, batch_id=batch), 'rename')
            if (data.get('batch_id') == batch and type(data.get('total')) is int and data['total'] == 1
                    and all(type(data.get(key)) is int and data[key] == expected for key, expected in
                            (('success_count', 0), ('fail_count', 1), ('unknown_count', 0), ('pending_count', 0)))):
                error = ProviderError('UPSTREAM_FAILED', 'The single rename item was explicitly reported as failed.', notices=self.last_notices)
                error.upstream_code = 0
                error.exit_code = 0
                raise error
            if (data.get('batch_id') != batch or data.get('result_status') != 'SUCCESS'
                    or type(data.get('total')) is not int or data['total'] != 1
                    or any(type(data.get(key)) is not int or data[key] != expected for key, expected in
                           (('success_count', 1), ('fail_count', 0), ('unknown_count', 0), ('pending_count', 0)))):
                raise ProviderError('OUTCOME_UNKNOWN', 'Rename batch is partial or unconfirmed; do not repeat it.')
            return MutationResult(True, self.last_notices)
        finally:
            if shutil.rmtree.avoids_symlink_attacks:
                shutil.rmtree(output)

    def download_records(self) -> tuple[dict, ...]:
        data, transcript = self._successful(AcceptanceCall('download-list'), 'download-list')
        rows = []
        for event in transcript.lists:
            row = event.get('data')
            if event.get('action') != 'download-list' or event.get('code') != 0 or not isinstance(row, dict) or not isinstance(row.get('recordId'), str):
                raise ProviderError('UNKNOWN_SCHEMA', 'Download record list schema is unrecognized.')
            rows.append(row)
        if type(data.get('totalCount')) is not int or data['totalCount'] != len(rows):
            raise ProviderError('UNKNOWN_SCHEMA', 'Download record list count is unrecognized.')
        return tuple(rows)

    def upload_records(self) -> tuple[dict, ...]:
        data, transcript = self._successful(AcceptanceCall('upload-list'), 'upload-list')
        rows = []
        for event in transcript.lists:
            row = event.get('data')
            if (event.get('action') != 'upload-list' or type(event.get('code')) is not int or event['code'] != 0
                    or not isinstance(row, dict) or not isinstance(row.get('recordId'), str) or not row['recordId']):
                raise ProviderError('UNKNOWN_SCHEMA', 'Upload record list schema is unrecognized.')
            rows.append(row)
        if type(data.get('totalCount')) is not int or data['totalCount'] != len(rows):
            raise ProviderError('UNKNOWN_SCHEMA', 'Upload record list count is unrecognized.')
        return tuple(rows)


def decode_upload_probe(output: OfficialResult, expected_names: tuple[str, ...], *, record_names: dict[str, str] | None = None) -> tuple[str, tuple[UploadResult, ...], int, tuple[str, ...]]:
    """M0-only partial observation, retaining each known successful native ID."""
    try:
        transcript = parse_ndjson(output.stdout.decode('utf-8', errors='strict'))
    except (UnicodeError, ProtocolError):
        raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload response framing is unrecognized.') from None
    notices = sanitize_notices(transcript.notices)
    if len(transcript.results) != 1:
        raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload terminal result is missing.', notices=notices)
    final = transcript.results[0]
    if final.get('action') != 'upload' or type(final.get('code')) is not int:
        raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload terminal schema is unrecognized.', notices=notices)
    observations = []
    seen = set()
    for event in transcript.lists:
        data = event.get('data')
        if event.get('action') != 'upload' or type(event.get('code')) is not int or not isinstance(data, dict):
            raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload item schema is unrecognized.', notices=notices)
        name = data.get('fileName')
        if name is None and event['code'] != 0 and record_names is not None:
            record = data.get('recordId')
            name = record_names.get(record) if isinstance(record, str) else None
        if name not in expected_names or name in seen:
            raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload item is not associated with an exact input filename.', notices=notices)
        seen.add(name)
        native, record, size = data.get('fileId'), data.get('recordId'), data.get('fileSize')
        if ((native is not None and (not isinstance(native, str) or not native))
                or (record is not None and (not isinstance(record, str) or not record))
                or (size is not None and (type(size) is not int or size < 0))
                or (event['code'] == 0 and (native is None or size is None))):
            raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload identity fields are unrecognized.', notices=notices)
        observations.append(UploadResult(native, record, size, name, 'success' if event['code'] == 0 else 'failed', notices))
    success = sum(item.outcome == 'success' for item in observations)
    failed = sum(item.outcome == 'failed' for item in observations)
    if seen != set(expected_names):
        outcome = 'no_items_reported' if not observations and final['code'] != 0 else 'unknown'
    elif success and failed:
        outcome = 'partial'
    elif success == len(expected_names) and final['code'] == 0 and output.exit_code == 0:
        outcome = 'success'
    elif failed == len(expected_names):
        outcome = 'failed'
    else:
        outcome = 'unknown'
    return outcome, tuple(observations), final['code'], notices


def upload_probe_diagnostics(output: OfficialResult) -> dict:
    """Field types only: no raw messages, links, names, environment or credentials."""
    frames = []
    invalid_lines = 0
    fields = {'fileName', 'fileId', 'recordId', 'fileSize', 'fids', 'successCount', 'failedCount', 'fileCount', 'totalSize'}
    for line in output.stdout.decode('utf-8', errors='replace').splitlines():
        if not line.strip():
            continue
        try:
            frame = json.loads(line)
        except (ValueError, RecursionError):
            invalid_lines += 1
            continue
        if not isinstance(frame, dict):
            invalid_lines += 1
            continue
        data = frame.get('data')
        description: dict[str, Any] = {'type': frame.get('type') if frame.get('type') in ('list', 'result', 'progress', 'artifact') else 'unknown',
                       'action': frame.get('action') if frame.get('action') in ('upload', 'upload-list', 'not_authenticated') else 'unknown',
                       'code_type': type(frame.get('code')).__name__, 'data_type': type(data).__name__}
        if type(frame.get('code')) is int:
            description['code'] = frame['code']
        if isinstance(data, dict):
            description['field_types'] = {key: type(data[key]).__name__ for key in sorted(fields & data.keys())}
            description['other_field_count'] = len(data.keys() - fields)
        frames.append(description)
        if len(frames) >= 64:
            break
    return {'exit_code': output.exit_code, 'frames': frames, 'invalid_line_count': invalid_lines}


def known_upload_acknowledgements(output: OfficialResult, expected_sizes: dict[str, int]) -> tuple[UploadResult, ...]:
    """Retain positive item evidence even when another frame prevents full decoding."""
    known = []
    for line in output.stdout.decode('utf-8', errors='replace').splitlines():
        try:
            transcript = parse_ndjson(line)
        except ProtocolError:
            continue
        for event in transcript.lists:
            data = event.get('data')
            if (event.get('action') == 'upload' and type(event.get('code')) is int and event['code'] == 0
                    and isinstance(data, dict) and isinstance(data.get('fileName'), str)
                    and data['fileName'] in expected_sizes and type(data.get('fileSize')) is int
                    and data['fileSize'] == expected_sizes[data['fileName']]
                    and isinstance(data.get('fileId'), str) and data['fileId']):
                record = data.get('recordId')
                known.append(UploadResult(data['fileId'], record if isinstance(record, str) else None,
                                          data['fileSize'], data['fileName'], 'success'))
    return tuple(known)

"""Explicit synthetic M0 round trip, stopping at every uncertain boundary."""
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any

from .config import Config
from .provider.protocol import ProtocolError, parse_ndjson, read_artifact_jsonl
from .provider.quark_cli import (
    ProviderError,
    VerificationCall,
    run_official,
    validate_runtime,
)


class VerificationError(ProviderError):
    def __init__(self, code: str, summary: dict):
        super().__init__(code, 'Verification stopped; no automatic retry was made. Preserve the recorded test objects for inspection.')
        self.summary = summary


def _verified_file(path: str, directory: Path, expected_name: str, expected: bytes | None) -> tuple[bytes, tuple[int, ...]]:
    root = directory
    candidate = Path(path)
    if not candidate.is_absolute() or candidate != root / expected_name:
        raise ProviderError('UNSAFE_DOWNLOAD_PATH', 'Download path is outside the fresh staging directory.')
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise ProviderError('PATH_CHECK_UNAVAILABLE', 'Secure verification opening is unavailable.')
    handles = []
    try:
        if not root.is_absolute() or '..' in root.parts:
            raise ProviderError('UNSAFE_DOWNLOAD_PATH', 'Verification directory must be canonical and absolute.')
        handles.append(os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY))
        for part in root.parts[1:]:
            handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
        fd = os.open(expected_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handles[-1])
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_size > 65536
                    or (expected is not None and before.st_size != len(expected))):
                raise ProviderError('CONTENT_MISMATCH', 'Downloaded file type or size differs from synthetic source.')
            with os.fdopen(os.dup(fd), 'rb') as stream:
                actual = stream.read(before.st_size + 1)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ProviderError('SOURCE_CHANGED', 'Downloaded file changed during verification.')
            if expected is not None and (actual != expected or hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest()):
                raise ProviderError('CONTENT_MISMATCH', 'Downloaded content differs from the trusted synthetic source.')
            return actual, (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        finally:
            os.close(fd)
    except OSError:
        raise ProviderError('UNSAFE_DOWNLOAD_PATH', 'Cannot securely open the downloaded file.') from None
    finally:
        for handle in reversed(handles):
            os.close(handle)


def verify_download(path: str, directory: Path, expected_name: str, expected: bytes) -> bytes:
    return _verified_file(path, directory, expected_name, expected)[0]


class Workflow:
    def __init__(self, config: Config, resume: str | None = None):
        self.config = config
        if resume is not None:
            try:
                if str(uuid.UUID(resume)) != resume:
                    raise ValueError
            except ValueError:
                raise ProviderError('RESUME_NOT_ALLOWED', 'Resume accepts only a canonical UUID checkpoint.') from None
        self.id = resume if resume is not None else str(uuid.uuid4())
        self.resume = resume is not None
        self.name = 'QuarkVFS-M0-' + self.id
        self.trash_name = 'QuarkVFS-M0-Trash-' + self.id
        self.root = config.state_dir.resolve() / ('verification-' + self.id)
        self.summary: dict[str, Any] = {'verification_id': self.id, 'test_directory_name': self.name,
                        'trash_directory_name': self.trash_name, 'test_location': 'not_created',
                        'completed_steps': [], 'verification_levels': [], 'upstream_notices': [],
                        'authorization_scope': 'unknown', 'capability_gates_changed': False,
                        'automatic_retries': 0}
        self.sequence = 0
        self.current_step = 'initialization'
        self.verified_objects: dict[str, tuple[str, bytes]] = {}

    def source_content(self) -> bytes:
        return ('# Quark VFS M0\n\nSynthetic UTF-8 document: 中文。\nVerification: ' + self.id + '\n').encode()

    def resume_checkpoint(self) -> str:
        raw, _ = _verified_file(str(self.root / 'journal.jsonl'), self.root, 'journal.jsonl', None)
        try:
            records = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
            if not records or any(not isinstance(record, dict) for record in records):
                raise ValueError
            if records[-1].get('event') != 'outcome_unknown' or records[-1].get('step') != 'source_round_trip_enumerate':
                raise ValueError
            creates = [r for r in records if r.get('step') == 'create_test_directory' and r.get('event') == 'fact']
            empties = [r for r in records if r.get('step') == 'confirm_test_empty' and r.get('event') == 'fact']
            uploads = [r for r in records if r.get('step') == 'source_round_trip_upload' and r.get('event') == 'fact']
            create_intents = [r for r in records if r.get('step') == 'create_test_directory' and r.get('event') == 'intent']
            upload_intents = [r for r in records if r.get('action') == 'upload' and r.get('event') == 'intent']
            for step, action in (('create_test_directory', 'create-folder'), ('source_round_trip_upload', 'upload')):
                responses = [r for r in records if r.get('step') == step and r.get('event') == 'business_response_observed']
                if (len(responses) != 1 or type(responses[0].get('code')) is not int or responses[0]['code'] != 0
                        or responses[0].get('exit_code', 0) != 0 or responses[0].get('action', action) != action):
                    raise ValueError
            if not all(len(group) == 1 for group in (creates, empties, uploads, create_intents, upload_intents)):
                raise ValueError
            fid = creates[0].get('created_fid')
            if (not isinstance(fid, str) or not fid or fid == '0'
                    or create_intents[0].get('name') != self.name or create_intents[0].get('parent_fid') != '0'
                    or empties[0].get('row_count') != 0 or empties[0].get('queried_parent_fid') != fid
                    or empties[0].get('complete') is not True
                    or upload_intents[0].get('parent_fid') != fid
                    or upload_intents[0].get('local_path') != str(self.root / 'source.md')
                    or uploads[0].get('size') != len(self.source_content())
                    or not isinstance(uploads[0].get('uploaded_fid'), str)
                    or any(r.get('step') in ('create_trash_directory', 'move_test_to_trash', 'processed_round_trip_upload') for r in records)):
                raise ValueError
            _verified_file(str(self.root / 'source.md'), self.root, 'source.md', self.source_content())
            self.log('resume_validated', 'source_checkpoint', upload_response_file_id=uploads[0]['uploaded_fid'])
            self.sequence = 100
            self.summary['resumed_source_upload'] = True
            return fid
        except (ValueError, KeyError, UnicodeError):
            raise ProviderError('RESUME_NOT_ALLOWED', 'Only the recorded successful-source-upload checkpoint may be resumed; no cloud request was made.') from None

    def log(self, event: str, step: str, **facts) -> None:
        path = self.root / 'journal.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps({'event': event, 'step': step, **facts}) + '\n').encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def call(self, step: str, request: VerificationCall):
        self.current_step = step
        self.log('intent', step, action=request.action, parent_fid=request.parent_fid,
                 fid=request.fid, target_fid=request.target_fid, name=request.name,
                 local_path=str(request.local_path) if request.local_path is not None else None)
        output = run_official(self.config, request)
        transcript = parse_ndjson(output.stdout.decode('utf-8', errors='strict'))
        if len(transcript.results) != 1:
            raise ProviderError('OUTCOME_UNKNOWN', 'Missing final workflow result.')
        result = transcript.results[0]
        if isinstance(result.get('action'), str) and type(result.get('code')) is int:
            self.log('business_response_observed', step, exit_code=output.exit_code,
                     action=result['action'], code=result['code'])
        for notice in transcript.notices:
            # Notices cannot be treated as an unrestricted output channel for secrets.
            if isinstance(notice, dict) and isinstance(notice.get('kind'), str) and isinstance(notice.get('text'), str):
                notice = notice['text']
            if isinstance(notice, str) and not re.search(r'(?i)token|secret|authorization|bearer|https?://|["\']?code["\']?\s*[:=]', notice):
                self.summary['upstream_notices'].append(notice[:2048])
            else:
                self.summary['upstream_notices'].append('Upstream notice received; sensitive or unknown content withheld.')
        if (result.get('action') != request.action or type(result.get('code')) is not int
                or result['code'] != 0 or output.exit_code != 0 or not isinstance(result.get('data'), dict)):
            raise ProviderError('OUTCOME_UNKNOWN', 'Workflow business or process outcome is unverified.')
        return result['data'], transcript

    def directory(self, name: str, step: str) -> str:
        data, _ = self.call(step, VerificationCall('create-folder', parent_fid='0', name=name))
        fid = data.get('fid')
        if not isinstance(fid, str) or not fid or fid == '0':
            raise ProviderError('UNKNOWN_SCHEMA', 'New directory identity is invalid.')
        self.log('fact', step, created_fid=fid)
        return fid

    def browse(self, fid: str, step: str) -> tuple[dict, ...]:
        self.sequence += 1
        output = self.root / ('browse-' + str(self.sequence))
        output.mkdir(mode=0o700)
        data, _ = self.call(step, VerificationCall('browse', parent_fid=fid, local_path=output))
        if (data.get('parent_fid') != fid or data.get('hasMore') is not False
                or type(data.get('count')) is not int or not isinstance(data.get('file_path'), str)):
            raise ProviderError('INCOMPLETE_ENUMERATION', 'Directory completeness could not be confirmed.')
        rows = read_artifact_jsonl(data['file_path'], allowed_output_dir=output,
                                   expected_count=data['count'], required_fields=('fid', 'parent_fid', 'filename', 'file_type'))
        seen = set()
        for row in rows:
            if (not isinstance(row['fid'], str) or not row['fid'] or row['fid'] in seen
                    or not isinstance(row['parent_fid'], str) or not row['parent_fid']
                    or not isinstance(row['filename'], str)
                    or row['file_type'] not in ('0', '1')):
                raise ProviderError('UNKNOWN_SCHEMA', 'Directory row identity or schema is unrecognized.')
            seen.add(row['fid'])
        # The terminal parent_fid above binds this complete direct enumeration to
        # the requested directory. Row parent_fid is opaque provider metadata;
        # it is not assumed to share the CLI's complete-FID representation.
        self.log('fact', step, row_count=len(rows), queried_parent_fid=fid, complete=True)
        return rows

    def transfer(self, directory_fid: str, name: str, content: bytes, step: str) -> bytes:
        source = self.root / name
        with source.open('xb') as stream:
            os.chmod(source, 0o600)
            stream.write(content)
        _, fingerprint = _verified_file(str(source), self.root, name, content)
        data, transcript = self.call(step + '_upload', VerificationCall('upload', parent_fid=directory_fid, local_path=source))
        try:
            _, after_fingerprint = _verified_file(str(source), self.root, name, content)
        except ProviderError:
            raise ProviderError('SOURCE_CHANGED', 'Synthetic source changed during upload; stop and inspect the retained object.') from None
        if fingerprint != after_fingerprint:
            raise ProviderError('SOURCE_CHANGED', 'Synthetic source metadata changed during upload.')
        items = transcript.lists
        if len(items) != 1:
            raise ProviderError('UPLOAD_PARTIAL', 'Upload did not report exactly one successful synthetic item.')
        item = items[0]
        detail = item.get('data')
        if (item.get('action') != 'upload' or type(item.get('code')) is not int or item['code'] != 0
                or not isinstance(detail, dict) or detail.get('fileName') != name
                or type(detail.get('fileSize')) is not int or detail['fileSize'] != len(content)
                or not isinstance(detail.get('fileId'), str) or not detail['fileId']
                or type(data.get('fileCount')) is not int or data['fileCount'] != 1
                or type(data.get('successCount')) is not int or data['successCount'] != 1
                or data.get('fids') != [detail['fileId']]):
            raise ProviderError('UPLOAD_PARTIAL', 'Upload item and final counts or identities disagree.')
        self.log('fact', step + '_upload', uploaded_fid=detail['fileId'], upload_response_file_id=detail['fileId'], size=len(content))
        return self.download_candidate(directory_fid, name, content, step)

    def download_candidate(self, directory_fid: str, name: str, content: bytes, step: str, *, require_single: bool = False) -> bytes:
        rows = self.browse(directory_fid, step + '_enumerate')
        matches = [row for row in rows if row['filename'] == name]
        if ((require_single and len(rows) != 1) or len(matches) != 1 or matches[0]['file_type'] != '1'
                or type(matches[0].get('size')) is not int or matches[0]['size'] != len(content)):
            raise ProviderError('OUTCOME_UNKNOWN', 'Fresh enumeration did not uniquely confirm the uploaded synthetic document.')
        remote_fid = matches[0]['fid']
        self.log('candidate_observed', step, fresh_remote_fid=remote_fid, size=len(content))
        return self.download_known(remote_fid, name, content, step)

    def download_known(self, remote_fid: str, name: str, content: bytes, step: str) -> bytes:
        staging = self.root / (step + '-download-' + str(uuid.uuid4()))
        staging.mkdir(mode=0o700)
        downloaded, _ = self.call(step + '_download', VerificationCall('download', fid=remote_fid, local_path=staging))
        if downloaded.get('fileName') != name or not isinstance(downloaded.get('filePath'), str):
            raise ProviderError('UNKNOWN_SCHEMA', 'Downloaded path schema is unrecognized.')
        actual = verify_download(downloaded['filePath'], staging, name, content)
        self.log('fact', step + '_verify', fresh_remote_fid=remote_fid, size=len(content), sha256=hashlib.sha256(content).hexdigest(), bound_verified=True)
        self.verified_objects[name] = (remote_fid, content)
        if step in ('source_round_trip', 'processed_round_trip'):
            self.summary['completed_steps'].append(step)
        return actual

    @staticmethod
    def browse_identity(fid: str) -> str:
        tail = fid.rsplit('|', 1)[-1]
        return tail if '|' in fid and tail else fid

    def confirm_payloads(self, directory_fid: str, step: str, *, download: bool) -> None:
        rows = self.browse(directory_fid, step)
        if len(rows) != 2 or {row['filename'] for row in rows} != {'source.md', 'processed.md'}:
            raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic directory contents changed; no further move will be attempted.')
        for row in rows:
            prior, expected = self.verified_objects[row['filename']]
            if (row['file_type'] != '1' or type(row.get('size')) is not int or row['size'] != len(expected)
                    or (not download and self.browse_identity(row['fid']) != self.browse_identity(prior))):
                raise ProviderError('OUTCOME_UNKNOWN', 'Previously verified synthetic payload identity or metadata changed.')
            if download:
                self.download_known(row['fid'], row['filename'], expected, step + '_' + row['filename'])

    def run(self) -> dict:
        validate_runtime(self.config)
        if not self.resume:
            self.root.mkdir(mode=0o700)
        try:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ProviderError('PATH_CHECK_UNAVAILABLE', 'Secure temporary root-list cleanup is unavailable.')
            if self.resume:
                test_fid = self.resume_checkpoint()
            else:
                try:
                    rows = self.browse('0', 'root_collision_check')
                finally:
                    # Fixed operation-owned directory, never an upstream-supplied cleanup path.
                    output = self.root / 'browse-1'
                    if output.exists():
                        shutil.rmtree(output)
                if any(row['filename'] in (self.name, self.trash_name) for row in rows):
                    raise ProviderError('NAME_CONFLICT', 'Synthetic UUID directory name already exists.')
                test_fid = self.directory(self.name, 'create_test_directory')
            self.summary['test_location'] = '/' + self.name
            if not self.resume and self.browse(test_fid, 'confirm_test_empty'):
                raise ProviderError('NAME_CONFLICT', 'Created test directory is not empty; no upload was attempted.')
            source = self.source_content()
            if self.resume:
                downloaded_source = self.download_candidate(test_fid, 'source.md', source, 'source_round_trip', require_single=True)
            else:
                downloaded_source = self.transfer(test_fid, 'source.md', source, 'source_round_trip')
            processed = downloaded_source + ('\nProcessed locally: ' + str(len(downloaded_source.decode('utf-8').splitlines())) + ' source lines.\n').encode()
            self.transfer(test_fid, 'processed.md', processed, 'processed_round_trip')
            self.summary['verification_levels'] = ['size_checked', 'trusted_sha256_verified', 'bytes_equal']
            self.confirm_payloads(test_fid, 'confirm_source_before_move', download=False)
            trash_fid = self.directory(self.trash_name, 'create_trash_directory')
            if self.browse(trash_fid, 'confirm_trash_empty'):
                raise ProviderError('NAME_CONFLICT', 'New trash container is not empty; no move was attempted.')
            moved, _ = self.call('move_test_to_trash', VerificationCall('move', fid=test_fid, target_fid=trash_fid))
            if moved.get('fids') != [test_fid] or moved.get('targetFid') != trash_fid:
                raise ProviderError('OUTCOME_UNKNOWN', 'Move response identity does not match the synthetic test.')
            rows = self.browse(trash_fid, 'confirm_trash_location')
            if len(rows) != 1 or rows[0]['filename'] != self.name or rows[0]['file_type'] != '0':
                raise ProviderError('OUTCOME_UNKNOWN', 'Fresh trash enumeration did not confirm the moved synthetic directory.')
            observed_test_fid = rows[0]['fid']
            self.log('candidate_observed', 'confirm_trash_location', fresh_remote_fid=observed_test_fid)
            self.confirm_payloads(observed_test_fid, 'confirm_moved_payloads', download=True)
            self.summary['test_location'] = '/' + self.trash_name + '/' + self.name
            self.summary['completed_steps'].append('moved_to_verified_trash')
            self.log('complete', 'workflow')
            return self.summary
        except (ProviderError, ProtocolError, OSError, UnicodeError) as error:
            self.summary['stopped'] = True
            self.summary['last_known_location_only'] = True
            self.summary['stopped_at_step'] = self.current_step
            self.log('outcome_unknown', self.current_step, error_code=getattr(error, 'code', 'LOCAL_IO_ERROR'))
            raise VerificationError(getattr(error, 'code', 'LOCAL_IO_ERROR'), self.summary) from None


def verify_workflow(config: Config, *, resume: str | None = None) -> dict:
    return Workflow(config, resume).run()

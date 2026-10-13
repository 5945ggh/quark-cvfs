"""Explicit, synthetic M0 probes. No permanent deletion, retries or gate changes."""
import argparse
import hashlib
import json
import os
import stat
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quark_vfs.config import Config, load_config
from quark_vfs.models import VfsError
from quark_vfs.provider.protocol import parse_ndjson
from quark_vfs.provider.quark import (
    QuarkProvider,
    decode_upload_probe,
    known_upload_acknowledgements,
    upload_probe_diagnostics,
)
from quark_vfs.provider.quark_cli import (
    AcceptanceCall,
    ProviderError,
    run_official,
    validate_runtime,
)
from quark_vfs.verification import verify_download


class AcceptanceHarness:
    def __init__(self, config: Config, provider: QuarkProvider | None = None, *, progress: Callable | None = None, resume_identities: str | None = None):
        self.config = config
        self.provider = provider if provider is not None else QuarkProvider(config)
        if resume_identities is not None:
            try:
                if str(uuid.UUID(resume_identities)) != resume_identities:
                    raise ValueError
            except ValueError:
                raise ProviderError('RESUME_NOT_ALLOWED', 'Identity resume requires a canonical recorded UUID.') from None
        self.id = resume_identities if resume_identities is not None else str(uuid.uuid4())
        self.resume_identities = resume_identities is not None
        self.resume_phase = 'collision_move'
        self.root = config.state_dir.resolve() / ('m0-acceptance-' + self.id)
        self.name = 'QuarkVFS-M0-Acceptance-' + self.id
        self.trash_name = 'QuarkVFS-M0-Acceptance-Trash-' + self.id
        self.account = 'm0-synthetic-probe'
        self.summary: dict[str, Any] = {'acceptance_id': self.id, 'test_directory_name': self.name,
                                        'test_location': 'not_created', 'phase': None, 'checks': {},
                                        'gates_changed': False, 'automatic_retries': 0,
                                        'upstream_notices': []}
        self.progress = progress
        self.completed: list[str] = []
        self.current_step = 'initialization'
        self.counter = 0
        self.payloads: dict[str, bytes] = {}

    def log(self, event: str, step: str, **facts) -> None:
        descriptor = os.open(self.root / 'journal.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (json.dumps({'event': event, 'step': step, **facts}) + '\n').encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def perform(self, step: str, operation: Callable, **facts):
        self.current_step = step
        self.log('intent', step, **facts)
        result = operation()
        for notice in getattr(result, 'notices', getattr(self.provider, 'last_notices', ())):
            if notice not in self.summary['upstream_notices']:
                self.summary['upstream_notices'].append(notice)
        self.log('response_observed', step)
        self.completed.append(step)
        return result

    def browse(self, fid: str, step: str):
        result = self.perform(step, lambda: self.provider.list_directory(self.account, fid), queried_parent_fid=fid)
        self.log('fact', step, count=len(result), complete=True)
        return result

    def directory(self, name: str, step: str) -> str:
        fid = self.perform(step, lambda: self.provider.mkdir(self.account, '0', name), parent_fid='0', name=name)
        self.log('fact', step, create_response_id=fid)
        if self.browse(fid, step + '_empty'):
            raise ProviderError('NAME_CONFLICT', 'Test directory is not empty; no synthetic upload was attempted.')
        return fid

    def upload(self, parent: str, name: str, content: bytes):
        path = self.root / name
        with path.open('xb') as stream:
            os.chmod(path, 0o600)
            stream.write(content)
        expected = hashlib.sha256(content).hexdigest()
        result = self.perform('upload_' + name, lambda: self.provider.upload_file(self.account, path, parent),
                              parent_fid=parent, name=name, local_path=str(path), sha256=expected)
        self.log('item_outcome', 'upload_' + name, outcome=result.outcome,
                 upload_response_file_id=result.native_id, record_id=result.record_id, size=result.size)
        if result.outcome != 'success':
            raise ProviderError('UPLOAD_PARTIAL', 'A synthetic upload did not fully succeed; retain successful items and do not replay the batch.')
        if path.read_bytes() != content:
            raise ProviderError('SOURCE_CHANGED', 'Synthetic upload source changed.')
        self.payloads[name] = content
        return result

    def unique(self, parent: str, name: str, content: bytes):
        for attempt in range(5):
            if attempt:
                time.sleep((.2, .4, .8, 1.6)[attempt - 1])
            rows = self.browse(parent, 'locate_' + name)
            matches = [row for row in rows if row.name == name and row.kind == 'file' and row.size == len(content)]
            self.log('readback_observation', 'locate_' + name, attempt=attempt + 1, matching_count=len(matches))
            if len(matches) == 1:
                return matches[0]
        raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic candidate was not uniquely observed after bounded read-only reconciliation.')

    def download(self, entry, content: bytes, label: str):
        staging = self.root / (label + '-' + str(uuid.uuid4()))
        staging.mkdir(mode=0o700)
        result = self.perform(label, lambda: self.provider.download_file(self.account, entry.fid, staging), fid=entry.fid, staging=str(staging))
        actual = verify_download(str(result.path), staging, entry.name, content)
        self.log('content_verified', label, fresh_remote_fid=entry.fid, size=len(actual), sha256=hashlib.sha256(actual).hexdigest())
        return staging, result

    def pagination(self, parent: str) -> None:
        for number in range(105):
            name = f'tiny-{number:03d}.md'
            self.upload(parent, name, ('M0 ' + str(number) + '\n').encode())
            if self.progress is not None and (number + 1) % 10 == 0:
                self.progress({'event': 'synthetic_upload_progress', 'completed': number + 1, 'total': 105})
        rows = self.browse(parent, 'complete_105_items')
        if len(rows) != 105 or {row.name for row in rows} != set(self.payloads):
            raise ProviderError('INCOMPLETE_ENUMERATION', 'More-than-one-page enumeration omitted or added synthetic items.')
        for row in rows:
            if row.kind != 'file' or row.size != len(self.payloads[row.name]):
                raise ProviderError('UNKNOWN_SCHEMA', 'Synthetic enumeration metadata differs from the generated manifest.')
        for name in ('tiny-000.md', 'tiny-052.md', 'tiny-104.md'):
            row = next(row for row in rows if row.name == name)
            self.download(row, self.payloads[name], 'verify_' + name)
        self.summary['checks']['pagination_105'] = 'passed'
        self.summary['checks']['sample_bytes_sha256'] = 'passed'

    def records_and_tail(self, parent: str) -> None:
        name = 'record-' + self.id + '.md'
        content = b'Synthetic short file.\n'
        self.upload(parent, name, content)
        entry = self.unique(parent, name, content)
        before = self.perform('download_records_before', self.provider.download_records)
        staging, _ = self.download(entry, content, 'record_staging')
        after = self.perform('download_records_after', self.provider.download_records)
        prior = {row['recordId'] for row in before}
        matches = [row for row in after if row['recordId'] not in prior and row.get('fileName') == name and row.get('fileSize') == len(content)]
        if len(matches) != 1:
            raise ProviderError('RECORD_ASSOCIATION_UNKNOWN', 'Download task delta did not uniquely identify the synthetic transfer.')
        record = matches[0]['recordId']
        self.log('record_associated', 'download_records_after', record_id=record, fresh_remote_fid=entry.fid)
        self.summary['checks']['download_record_association'] = 'passed'
        data, _ = self.perform('completed_task_resume', lambda: self.provider._successful(AcceptanceCall('download-resume', record_id=record, local_path=staging), 'download-resume'), record_id=record, staging=str(staging))
        result = self.provider._safe_download_result(data, staging)
        verify_download(str(result.path), staging, name, content)
        self.summary['checks']['completed_task_resume_same_staging'] = 'passed'
        self.summary['checks']['interrupted_download_resume'] = 'not_tested'
        tail = self.root / 'short-tail'
        tail.mkdir(mode=0o700)
        with (tail / name).open('xb') as stream:
            stream.write(content + b'OLD TAIL MUST NOT SURVIVE' * 4)
        data, _ = self.perform('short_file_overwrite_probe', lambda: self.provider._successful(AcceptanceCall('download-overwrite', fid=entry.fid, local_path=tail), 'download'), fid=entry.fid, staging=str(tail))
        result = self.provider._safe_download_result(data, tail)
        try:
            verify_download(str(result.path), tail, name, content)
        except ProviderError as error:
            if error.code != 'CONTENT_MISMATCH':
                raise
            self.log('upstream_bug_observed', 'short_file_overwrite_probe', error_code=error.code)
            self.summary['checks']['short_file_overwrite'] = 'failed_content_mismatch'
        else:
            self.summary['checks']['short_file_overwrite'] = 'passed_sample_only'

    def child_directory(self, parent: str, name: str) -> str:
        response_id = self.perform('create_child_' + name, lambda: self.provider.mkdir(self.account, parent, name),
                                   parent_fid=parent, name=name)
        self.log('fact', 'create_child_' + name, create_response_id=response_id)
        rows = self.browse(parent, 'locate_child_' + name)
        candidates = [row for row in rows if row.name == name and row.kind == 'directory']
        if len(candidates) != 1 or self.browse(candidates[0].fid, 'child_empty_' + name):
            raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic child directory was not uniquely empty.')
        return candidates[0].fid

    def upload_child(self, parent: str, name: str, content: bytes, label: str):
        directory = self.root / ('child-source-' + str(uuid.uuid4()))
        directory.mkdir(mode=0o700)
        source = directory / name
        with source.open('xb') as stream:
            os.chmod(source, 0o600)
            stream.write(content)
        result = self.perform(label, lambda: self.provider.upload_file(self.account, source, parent),
                              parent_fid=parent, name=name, local_path=str(source))
        self.log('item_outcome', label, upload_response_file_id=result.native_id, record_id=result.record_id, outcome=result.outcome)
        if result.outcome != 'success' or source.read_bytes() != content:
            raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic child upload is partial or source changed.')
        return self.unique(parent, name, content)

    def identities_collisions(self, parent: str) -> None:
        same = self.child_directory(parent, 'idempotent-folder')
        self.perform('repeat_same_folder_name', lambda: self.provider.mkdir(self.account, parent, 'idempotent-folder'),
                     parent_fid=parent, name='idempotent-folder')
        rows = self.browse(parent, 'confirm_idempotent_folder')
        if len([row for row in rows if row.name == 'idempotent-folder' and row.kind == 'directory']) != 1:
            raise ProviderError('OUTCOME_UNKNOWN', 'Same-name directory creation did not remain unique.')
        self.summary['checks']['mkdir_same_name'] = 'single_directory_observed'
        left = self.child_directory(parent, 'move-source')
        right = self.child_directory(parent, 'move-target')
        source_a = b'Synthetic left payload.\n'
        source_b = b'Synthetic right payload, different length.\n'
        a = self.upload_child(left, 'collision.md', source_a, 'upload_collision_left')
        b = self.upload_child(right, 'collision.md', source_b, 'upload_collision_right')
        self.download(a, source_a, 'verify_collision_left')
        self.download(b, source_b, 'verify_collision_right')
        try:
            self.perform('move_name_collision', lambda: self.provider.move(self.account, a.fid, right),
                         fid=a.fid, target_fid=right)
        except ProviderError as error:
            if error.code != 'UPSTREAM_FAILED':
                raise
            self.log('business_rejected', 'move_name_collision', upstream_code=error.upstream_code)
            unchanged_left = self.unique(left, 'collision.md', source_a)
            unchanged_right = self.unique(right, 'collision.md', source_b)
            self.download(unchanged_left, source_a, 'verify_rejected_move_left')
            self.download(unchanged_right, source_b, 'verify_rejected_move_right')
            self.summary['checks']['move_name_collision'] = 'rejected_both_contents_preserved'
        else:
            left_rows = self.browse(left, 'observe_move_source')
            right_rows = self.browse(right, 'observe_move_target')
            if left_rows or len(right_rows) != 2 or any(row.kind != 'file' for row in right_rows):
                raise ProviderError('OUTCOME_UNKNOWN', 'Collision move changed synthetic payload membership unexpectedly.')
            for expected in (source_a, source_b):
                candidates = [row for row in right_rows if row.size == len(expected)]
                if len(candidates) != 1:
                    raise ProviderError('OUTCOME_UNKNOWN', 'Collision move contents cannot be uniquely verified.')
                self.download(candidates[0], expected, 'verify_duplicate_move_' + str(len(expected)))
            self.summary['checks']['move_name_collision'] = ('allowed_duplicate_names_both_contents_preserved'
                                                             if all(row.name == 'collision.md' for row in right_rows)
                                                             else 'kept_both_with_provider_names_content_verified')
        self.rename_probes(same)
        self.payloads = {'idempotent-folder': b'', 'move-source': b'', 'move-target': b''}

    def rename_probes(self, same: str) -> None:
        source_a = b'Synthetic left payload.\n'
        source_b = b'Synthetic right payload, different length.\n'
        original = self.upload_child(same, 'original.md', source_a, 'upload_rename_source')
        self.perform('rename_distinct_name', lambda: self.provider.rename(self.account, original.fid, 'original.md', 'renamed.md'),
                     fid=original.fid, old_name='original.md', new_name='renamed.md')
        renamed = self.unique(same, 'renamed.md', source_a)
        self.download(renamed, source_a, 'verify_renamed_content')
        self.log('identity_observation', 'rename_distinct_name', before_browse_fid=original.fid, after_browse_fid=renamed.fid)
        self.summary['checks']['rename_content'] = 'bytes_verified_ids_recorded_without_stability_promise'
        occupied = self.upload_child(same, 'occupied.md', source_b, 'upload_rename_target')
        self.download(occupied, source_b, 'verify_rename_target')
        self.rename_collision(same, renamed)

    def mixed_permissions_probe(self, parent: str) -> None:
        allowed = self.root / 'mixed-readable.md'
        blocked = self.root / 'mixed-unreadable.md'
        allowed_content = b'Synthetic readable mixed item.\n'
        blocked_content = b'Synthetic unreadable mixed item.\n'
        for path, content in ((allowed, allowed_content), (blocked, blocked_content)):
            with path.open('xb') as stream:
                os.chmod(path, 0o600)
                stream.write(content)
        os.chmod(blocked, 0)
        try:
            # Check the actual execution UID rather than pretending chmod denied a privileged reader.
            if os.access(blocked, os.R_OK):
                self.summary['checks']['mixed_upload_partial'] = 'not_exercised_read_denial_ineffective'
                return
            request = AcceptanceCall('upload-mixed', fid=parent, local_paths=(allowed, blocked))
            output = self.perform('mixed_two_path_upload', lambda: run_official(self.config, request),
                                  parent_fid=parent, local_paths=[str(allowed), str(blocked)])
            self.log('safe_protocol_diagnostics', 'mixed_two_path_upload', **upload_probe_diagnostics(output))
            for item in known_upload_acknowledgements(output, {allowed.name: len(allowed_content), blocked.name: len(blocked_content)}):
                self.log('known_success_acknowledged', 'mixed_two_path_upload', name=item.name,
                         native_id=item.native_id, record_id=item.record_id, size=item.size)
            record_names = {}
            # Failed upload events expose recordId alone. Only exact recordId joins
            # may supply the filename; never infer it from event order or leftovers.
            frames = parse_ndjson(output.stdout.decode('utf-8'))
            failed_ids = {event['data']['recordId'] for event in frames.lists
                          if event.get('action') == 'upload' and type(event.get('code')) is int and event['code'] != 0
                          and isinstance(event.get('data'), dict) and isinstance(event['data'].get('recordId'), str)}
            if failed_ids:
                records = self.perform('mixed_failure_exact_record_lookup', self.provider.upload_records)
                for record_id in failed_ids:
                    matches = [row for row in records if row.get('recordId') == record_id and row.get('fileName') in (allowed.name, blocked.name)]
                    if len(matches) == 1:
                        record_names[record_id] = matches[0]['fileName']
            outcome, items, upstream_code, notices = decode_upload_probe(output, (allowed.name, blocked.name), record_names=record_names)
            self.summary['upstream_notices'].extend(notices)
            self.log('mixed_outcome', 'mixed_two_path_upload', outcome=outcome, upstream_code=upstream_code, exit_code=output.exit_code)
            for item in items:
                self.log('item_outcome', 'mixed_two_path_upload', name=item.name, outcome=item.outcome,
                         native_id=item.native_id, record_id=item.record_id, size=item.size)
            rows = self.browse(parent, 'mixed_fresh_reconciliation')
            if outcome == 'no_items_reported' and not rows:
                self.summary['checks']['mixed_upload_partial'] = 'whole_batch_rejected_before_any_observed_object'
            elif outcome == 'partial':
                successful = {item.name for item in items if item.outcome == 'success'}
                if {row.name for row in rows} != successful:
                    raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload item outcomes disagree with fresh synthetic contents.')
                contents = {allowed.name: allowed_content, blocked.name: blocked_content}
                for row in rows:
                    expected = contents[row.name]
                    if row.kind != 'file' or row.size != len(expected):
                        raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload metadata is inconsistent.')
                    self.download(row, expected, 'mixed_verify_' + row.name)
                    self.payloads[row.name] = expected
                self.summary['checks']['mixed_upload_partial'] = 'partial_success_retained_and_bytes_verified_no_batch_retry'
            elif outcome == 'failed' and not rows:
                self.summary['checks']['mixed_upload_partial'] = 'all_items_failed_no_observed_objects'
            else:
                raise ProviderError('OUTCOME_UNKNOWN', 'Mixed upload did not yield a verified partial or all-rejected outcome.')
        finally:
            os.chmod(blocked, 0o600)

    def rename_collision(self, same: str, renamed) -> None:
        source_a = b'Synthetic left payload.\n'
        source_b = b'Synthetic right payload, different length.\n'
        try:
            self.perform('rename_name_collision', lambda: self.provider.rename(self.account, renamed.fid, 'renamed.md', 'occupied.md'),
                         fid=renamed.fid, old_name='renamed.md', new_name='occupied.md')
        except ProviderError as error:
            if error.code != 'UPSTREAM_FAILED':
                raise
            self.log('business_rejected', 'rename_name_collision', upstream_code=error.upstream_code)
            a = self.unique(same, 'renamed.md', source_a)
            b = self.unique(same, 'occupied.md', source_b)
            self.download(a, source_a, 'verify_rejected_rename_source')
            self.download(b, source_b, 'verify_rejected_rename_target')
            self.summary['checks']['rename_name_collision'] = 'rejected_both_contents_preserved'
        else:
            rows = self.browse(same, 'observe_rename_collision')
            if len(rows) != 2 or any(row.name != 'occupied.md' or row.kind != 'file' for row in rows):
                raise ProviderError('OUTCOME_UNKNOWN', 'Rename collision did not preserve both generated payloads.')
            for expected in (source_a, source_b):
                candidates = [row for row in rows if row.size == len(expected)]
                if len(candidates) != 1:
                    raise ProviderError('OUTCOME_UNKNOWN', 'Rename collision content cannot be uniquely verified.')
                self.download(candidates[0], expected, 'verify_duplicate_rename_' + str(len(expected)))
            self.summary['checks']['rename_name_collision'] = 'allowed_duplicate_names_both_contents_preserved'

    def resume_identity_checkpoint(self) -> str:
        directory = QuarkProvider._secure_directory(self.root)
        descriptor = None
        try:
            descriptor = os.open('journal.jsonl', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            metadata = os.fstat(descriptor)
            if metadata.st_size > 262144:
                raise ProviderError('RESUME_NOT_ALLOWED', 'Identity checkpoint exceeds its permitted journal size.')
            with os.fdopen(os.dup(descriptor), 'rb') as stream:
                raw = stream.read(262145)
            records = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
            if not records or any(not isinstance(record, dict) for record in records):
                raise ValueError
            last_step = records[-1].get('step')
            if records[-1].get('event') != 'outcome_unknown' or last_step not in ('observe_move_target', 'locate_occupied.md'):
                raise ValueError
            def one(event, step):
                values = [record for record in records if record.get('event') == event and record.get('step') == step]
                if len(values) != 1:
                    raise ValueError
                return values[0]
            create = one('fact', 'create_test_directory')
            intent = one('intent', 'create_test_directory')
            one('response_observed', 'create_test_directory')
            one('response_observed', 'move_name_collision')
            move = one('intent', 'move_name_collision')
            source = one('fact', 'observe_move_source')
            target = one('fact', 'observe_move_target')
            left = one('item_outcome', 'upload_collision_left')
            right = one('item_outcome', 'upload_collision_right')
            parent = create.get('create_response_id')
            if (not isinstance(parent, str) or not parent or parent == '0'
                    or intent.get('parent_fid') != '0' or intent.get('name') != self.name
                    or not isinstance(move.get('fid'), str) or not isinstance(move.get('target_fid'), str)
                    or source.get('count') != 0 or source.get('complete') is not True
                    or target.get('count') != 2 or target.get('complete') is not True
                    or left.get('outcome') != 'success' or right.get('outcome') != 'success'
                    or any('preservation' in str(record.get('step', '')) for record in records)):
                raise ValueError
            if last_step == 'observe_move_target':
                if any('rename' in str(record.get('step', '')) for record in records):
                    raise ValueError
            else:
                first_upload = one('item_outcome', 'upload_rename_source')
                second_upload = one('item_outcome', 'upload_rename_target')
                one('response_observed', 'rename_distinct_name')
                verified = one('content_verified', 'verify_renamed_content')
                if (first_upload.get('outcome') != 'success' or second_upload.get('outcome') != 'success'
                        or verified.get('sha256') != hashlib.sha256(b'Synthetic left payload.\n').hexdigest()
                        or any(record.get('step') == 'rename_name_collision' for record in records)):
                    raise ValueError
                self.resume_phase = 'rename_target_uploaded'
            return parent
        except (ValueError, UnicodeError):
            raise ProviderError('RESUME_NOT_ALLOWED', 'Only the confirmed collision-move checkpoint may continue; no upload or move was retried.') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(directory)

    def resume_identity_readback(self, parent: str) -> None:
        rows = self.browse(parent, 'resume_confirm_generated_namespace')
        names = {'idempotent-folder', 'move-source', 'move-target'}
        if len(rows) != 3 or {row.name for row in rows} != names or any(row.kind != 'directory' for row in rows):
            raise ProviderError('RESUME_NOT_ALLOWED', 'Recorded synthetic namespace changed; no continuation write was made.')
        directories = {row.name: row.fid for row in rows}
        if self.browse(directories['move-source'], 'resume_confirm_source_empty'):
            raise ProviderError('RESUME_NOT_ALLOWED', 'Moved source is no longer empty.')
        target = self.browse(directories['move-target'], 'resume_confirm_target_payloads')
        if len(target) != 2 or any(row.kind != 'file' for row in target):
            raise ProviderError('RESUME_NOT_ALLOWED', 'Moved target contains unexpected objects.')
        for content in (b'Synthetic left payload.\n', b'Synthetic right payload, different length.\n'):
            candidates = [row for row in target if row.size == len(content)]
            if len(candidates) != 1:
                raise ProviderError('RESUME_NOT_ALLOWED', 'Moved payload candidate is not unique.')
            self.download(candidates[0], content, 'resume_verify_moved_' + str(len(content)))
        same = directories['idempotent-folder']
        self.summary['checks']['move_name_collision'] = 'kept_both_with_provider_names_content_verified'
        self.summary['checks']['resumed_without_repeating_move_or_uploads'] = True
        if self.resume_phase == 'rename_target_uploaded':
            source_a = b'Synthetic left payload.\n'
            source_b = b'Synthetic right payload, different length.\n'
            a = self.unique(same, 'renamed.md', source_a)
            b = self.unique(same, 'occupied.md', source_b)
            rows = self.browse(same, 'resume_confirm_only_rename_pair')
            if len(rows) != 2 or {row.name for row in rows} != {'renamed.md', 'occupied.md'}:
                raise ProviderError('RESUME_NOT_ALLOWED', 'Recorded rename namespace contains unexpected objects.')
            self.download(a, source_a, 'resume_verify_rename_source')
            self.download(b, source_b, 'resume_verify_rename_target')
            self.rename_collision(same, a)
            self.summary['checks']['rename_content'] = 'existing_pair_bytes_reverified_no_reupload'
        else:
            if self.browse(same, 'resume_confirm_rename_unstarted'):
                raise ProviderError('RESUME_NOT_ALLOWED', 'Rename test has already started or contains unexpected objects.')
            self.rename_probes(same)
        self.payloads = dict.fromkeys(names, b'')

    def preserve(self, parent: str) -> None:
        before = self.browse(parent, 'confirm_preservation_source_manifest')
        def matches_manifest(rows):
            if len(rows) != len(self.payloads) or {row.name for row in rows} != set(self.payloads):
                return False
            return all(row.kind == ('file' if self.payloads[row.name] else 'directory')
                       and (not self.payloads[row.name] or row.size == len(self.payloads[row.name])) for row in rows)
        if not matches_manifest(before):
            raise ProviderError('OUTCOME_UNKNOWN', 'Source contains unexpected objects; preservation was not dispatched.')
        collision_contents = []
        for row in before:
            if row.kind != 'directory':
                continue
            children = self.browse(row.fid, 'confirm_preservation_child_manifest')
            known = [b'Synthetic left payload.\n', b'Synthetic right payload, different length.\n']
            expected = known if row.name == 'idempotent-folder' else [content for content in known if any(child.size == len(content) for child in children)]
            if row.name in ('move-source', 'move-target'):
                collision_contents.extend(expected)
            if row.name not in ('move-source', 'move-target', 'idempotent-folder') or len(children) != len(expected):
                raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic child manifest changed; preservation was not dispatched.')
            for content in expected:
                candidates = [child for child in children if child.kind == 'file' and child.size == len(content)]
                if len(candidates) != 1:
                    raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic child identity is ambiguous; preservation was not dispatched.')
                self.download(candidates[0], content, 'confirm_preservation_child_bytes_' + row.name + '_' + str(len(content)))
        if 'move-source' in self.payloads and sorted(collision_contents) != sorted([b'Synthetic left payload.\n', b'Synthetic right payload, different length.\n']):
            raise ProviderError('OUTCOME_UNKNOWN', 'Collision pair is incomplete; preservation was not dispatched.')
        trash = self.directory(self.trash_name, 'create_preservation_container')
        self.perform('move_to_preservation', lambda: self.provider.move(self.account, parent, trash), fid=parent, target_fid=trash)
        confirmed = False
        for attempt in range(5):
            if attempt:
                time.sleep((.2, .4, .8, 1.6)[attempt - 1])
            rows = self.browse(trash, 'confirm_preservation_directory')
            matches = [row for row in rows if row.kind == 'directory' and row.name == self.name]
            if len(rows) != 1 or len(matches) != 1:
                continue
            content_rows = self.browse(matches[0].fid, 'confirm_preserved_payloads')
            if matches_manifest(content_rows):
                confirmed = True
                break
        if not confirmed:
            raise ProviderError('OUTCOME_UNKNOWN', 'Preservation directory and synthetic payload manifest were not freshly confirmed.')
        self.summary['test_location'] = '/' + self.trash_name + '/' + self.name
        self.summary['checks']['preserved_to_test_container'] = 'passed'

    def run(self, phase: str) -> dict:
        validate_runtime(self.config)
        if not self.resume_identities:
            self.root.mkdir(mode=0o700)
        self.summary['phase'] = phase
        try:
            if self.resume_identities:
                if phase != 'identities-collisions':
                    raise ProviderError('RESUME_NOT_ALLOWED', 'Identity checkpoint continuation is limited to its collision phase.')
                parent = self.resume_identity_checkpoint()
            else:
                root_rows = self.browse('0', 'root_collision_check')
                if any(row.name in (self.name, self.trash_name) for row in root_rows):
                    raise ProviderError('NAME_CONFLICT', 'Random synthetic directory name is already present.')
                parent = self.directory(self.name, 'create_test_directory')
            self.summary['test_location'] = '/' + self.name
            if phase == 'pagination':
                self.pagination(parent)
            elif phase == 'download-records-tail':
                self.records_and_tail(parent)
            elif phase == 'identities-collisions':
                if self.resume_identities:
                    self.resume_identity_readback(parent)
                else:
                    self.identities_collisions(parent)
            elif phase == 'rename':
                same = self.child_directory(parent, 'idempotent-folder')
                self.rename_probes(same)
                self.payloads = {'idempotent-folder': b''}
            elif phase == 'interrupted-resume':
                run_interrupted_probe(self, parent)
            elif phase == 'mixed-partial':
                self.mixed_permissions_probe(parent)
            else:
                raise ProviderError('INVALID_ARGUMENT', 'Unsupported acceptance phase.')
            self.preserve(parent)
            self.log('complete', phase)
            self.summary['ok'] = True
        except (VfsError, OSError, ValueError) as error:
            self.log('outcome_unknown', self.current_step, error_code=getattr(error, 'code', 'LOCAL_IO_ERROR'),
                     upstream_code=getattr(error, 'upstream_code', None), exit_code=getattr(error, 'exit_code', None))
            self.summary.update(ok=False, stopped_at_step=self.current_step,
                                error_code=getattr(error, 'code', 'LOCAL_IO_ERROR'), last_known_location_only=True)
        return self.summary


def verify_large_file(path: Path, staging: Path, name: str, expected: bytes) -> None:
    if path != staging / name:
        raise ProviderError('UNSAFE_DOWNLOAD_PATH', 'Synthetic download escaped its staging directory.')
    directory = QuarkProvider._secure_directory(staging)
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != len(expected):
            raise ProviderError('CONTENT_MISMATCH', 'Resumed synthetic file length is incorrect.')
        digest = hashlib.sha256()
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            for block in iter(lambda: stream.read(65536), b''):
                digest.update(block)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ProviderError('SOURCE_CHANGED', 'Resumed file changed during verification.')
        if digest.digest() != hashlib.sha256(expected).digest():
            raise ProviderError('CONTENT_MISMATCH', 'Resumed bytes differ from the trusted generated source.')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def run_interrupted_probe(harness: AcceptanceHarness, parent: str) -> None:
    name = 'interrupt-' + harness.id + '.bin'
    content = bytes(range(256)) * (8 * 1024 * 1024 // 256)
    harness.upload(parent, name, content)
    entry = harness.unique(parent, name, content)
    before = harness.perform('interrupt_records_before', harness.provider.download_records)
    staging = harness.root / 'interrupted-staging'
    staging.mkdir(mode=0o700)
    output = harness.perform('interrupt_own_partial_download',
                             lambda: run_official(harness.config, AcceptanceCall('download-interrupt', fid=entry.fid,
                                                  local_path=staging, expected_name=name, expected_size=len(content))),
                             fid=entry.fid, staging=str(staging), expected_size=len(content))
    harness.log('interruption_observation', 'interrupt_own_partial_download',
                exit_code=output.exit_code, interrupted_size=output.interrupted_size)
    after = harness.perform('interrupt_records_after', harness.provider.download_records)
    prior = {row['recordId'] for row in before}
    candidates = [row for row in after if row['recordId'] not in prior and row.get('fileName') == name and row.get('fileSize') == len(content)]
    if len(candidates) != 1:
        raise ProviderError('RECORD_ASSOCIATION_UNKNOWN', 'Synthetic task was not uniquely associated; no resume was attempted.')
    record = candidates[0]['recordId']
    if output.interrupted_size is None:
        transcript = parse_ndjson(output.stdout.decode('utf-8'))
        if (output.exit_code != 0 or len(transcript.results) != 1 or transcript.results[0].get('action') != 'download'
                or type(transcript.results[0].get('code')) is not int or transcript.results[0]['code'] != 0):
            raise ProviderError('OUTCOME_UNKNOWN', 'Download ended without a partial checkpoint or confirmed completion.')
        data = transcript.results[0].get('data')
        if not isinstance(data, dict):
            raise ProviderError('UNKNOWN_SCHEMA', 'Completed synthetic download data is unrecognized.')
        completed = harness.provider._safe_download_result(data, staging)
        verify_large_file(completed.path, staging, name, content)
        harness.summary['checks']['interrupted_download_resume'] = 'not_observed_transfer_completed'
        return
    directory = QuarkProvider._secure_directory(staging)
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not 0 < metadata.st_size < len(content):
            raise ProviderError('OUTCOME_UNKNOWN', 'Stopped download is not strictly partial; no resume was attempted.')
        harness.log('partial_checkpoint_observed', 'interrupt_own_partial_download', record_id=record,
                    size=metadata.st_size, expected_size=len(content), exit_code=output.exit_code)
    finally:
        os.close(directory)
    data, _ = harness.perform('resume_interrupted_same_staging',
                             lambda: harness.provider._successful(AcceptanceCall('download-resume', record_id=record, local_path=staging), 'download-resume'),
                             record_id=record, staging=str(staging))
    result = harness.provider._safe_download_result(data, staging)
    verify_large_file(result.path, staging, name, content)
    harness.log('content_verified', 'resume_interrupted_same_staging', sha256=hashlib.sha256(content).hexdigest(), size=len(content))
    harness.summary['checks']['interrupted_download_resume'] = 'passed_same_record_same_staging_trusted_sha256'


# This finite registry identifies the already-authorized synthetic cases only.
PRESERVATION_CASES = {
    '9a1f345c-af8e-4044-a2f4-5346224663bf': 'short',
    '7e51eb1a-6df8-46c2-bcd8-b916443773fa': 'interrupt',
    '32cc40ab-a3ac-4820-823c-ed7f64c4ce9f': 'mixed',
    '5b17cbf8-77d5-48eb-94ba-1b593a3fd386': 'mixed_already_dispatched',
}


def _private_records(directory: Path) -> list[dict]:
    parent = QuarkProvider._secure_directory(directory)
    descriptor = None
    try:
        descriptor = os.open('journal.jsonl', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 262144:
            raise ProviderError('RESUME_NOT_ALLOWED', 'Known-case journal must be a bounded regular file.')
        with os.fdopen(os.dup(descriptor), 'rb') as stream:
            raw = stream.read(262145)
        records = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
        if any(not isinstance(record, dict) for record in records):
            raise ProviderError('RESUME_NOT_ALLOWED', 'Known-case journal schema is unrecognized.')
        return records
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def preserve_known_case(config: Config, case: str, *, provider: QuarkProvider | None = None) -> dict:
    if case not in PRESERVATION_CASES:
        raise ProviderError('RESUME_NOT_ALLOWED', 'Cleanup is limited to the explicitly registered synthetic cases.')
    validate_runtime(config)
    adapter = provider if provider is not None else QuarkProvider(config)
    account = 'm0-synthetic-preservation'
    name = 'QuarkVFS-M0-Acceptance-' + case
    trash_name = 'QuarkVFS-M0-Acceptance-Trash-' + case
    old = config.state_dir.resolve() / ('m0-acceptance-' + case)
    records = _private_records(old)
    created = [row for row in records if row.get('event') == 'fact' and row.get('step') == 'create_test_directory']
    intents = [row for row in records if row.get('event') == 'intent' and row.get('step') == 'create_test_directory']
    if len(created) != 1 or len(intents) != 1 or intents[0].get('name') != name or intents[0].get('parent_fid') != '0':
        raise ProviderError('RESUME_NOT_ALLOWED', 'Known synthetic directory creation is not recorded.')
    state = config.state_dir.resolve() / ('m0-preservation-' + case)
    state.mkdir(mode=0o700, exist_ok=True)
    previous = _private_records(state) if (state / 'journal.jsonl').exists() else []
    def log(event, step, **facts):
        descriptor = os.open(state / 'journal.jsonl', os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, (json.dumps({'event': event, 'step': step, **facts}) + '\n').encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    def await_rows(parent_fid, check, step):
        for attempt in range(5):
            if attempt:
                time.sleep((.2, .4, .8, 1.6)[attempt - 1])
            rows = adapter.list_directory(account, parent_fid)
            log('readback_observation', step, attempt=attempt + 1, count=len(rows))
            if check(rows):
                return rows
        raise ProviderError('OUTCOME_UNKNOWN', 'Known-case read-only reconciliation did not confirm the expected namespace.')
    kind = PRESERVATION_CASES[case]
    if kind == 'short':
        payloads = {'record-' + case + '.md': b'Synthetic short file.\n'}
    elif kind == 'interrupt':
        payloads = {'interrupt-' + case + '.bin': bytes(range(256)) * (8 * 1024 * 1024 // 256)}
    else:
        payloads = {'mixed-readable.md': b'Synthetic readable mixed item.\n'}
    def verify_payloads(directory_fid, label):
        rows = await_rows(directory_fid,
                          lambda rows: len(rows) == len(payloads) and {row.name for row in rows} == set(payloads), label)
        for row in rows:
            expected = payloads[row.name]
            if row.kind != 'file' or row.size != len(expected):
                raise ProviderError('OUTCOME_UNKNOWN', 'Known-case payload metadata changed.')
            staging = state / (label + '-' + str(uuid.uuid4()))
            staging.mkdir(mode=0o700)
            log('read_intent', label, fid=row.fid, staging=str(staging))
            downloaded = adapter.download_file(account, row.fid, staging)
            verify_large_file(downloaded.path, staging, row.name, expected)
            log('content_verified', label, sha256=hashlib.sha256(expected).hexdigest(), size=len(expected))
    dispatched = (any(row.get('event') == 'intent' and row.get('step') == 'move_to_preservation' for row in records)
                  or any(row.get('event') == 'intent' for row in previous))
    try:
        root = adapter.list_directory(account, '0')
        sources = [row for row in root if row.name == name and row.kind == 'directory']
        targets = [row for row in root if row.name == trash_name and row.kind == 'directory']
        if dispatched:
            # Unknown writes are only reconciled. Never repeat create/move from this path.
            if len(targets) != 1:
                raise ProviderError('OUTCOME_UNKNOWN', 'Dispatched preservation container is not uniquely visible; no write was repeated.')
            target = targets[0]
        else:
            if len(sources) != 1 or targets:
                raise ProviderError('RESUME_NOT_ALLOWED', 'Known source or new container name does not uniquely match the recorded case.')
            source = sources[0]
            verify_payloads(source.fid, 'verify_before_preservation')
            log('intent', 'create_preservation_container', parent_fid='0', name=trash_name)
            native = adapter.mkdir(account, '0', trash_name)
            log('response_observed', 'create_preservation_container', create_response_id=native)
            root = await_rows('0', lambda rows: len([row for row in rows if row.name == trash_name and row.kind == 'directory']) == 1, 'confirm_new_container')
            target = next(row for row in root if row.name == trash_name and row.kind == 'directory')
            if adapter.list_directory(account, target.fid):
                raise ProviderError('NAME_CONFLICT', 'New preservation container is not empty; no move was made.')
            log('intent', 'move_to_preservation', fid=source.fid, target_fid=target.fid)
            acknowledgement = adapter.move(account, source.fid, target.fid)
            log('response_observed', 'move_to_preservation', acknowledged=acknowledgement.acknowledged)
        children = await_rows(target.fid,
                              lambda rows: len(rows) == 1 and rows[0].name == name and rows[0].kind == 'directory',
                              'confirm_preserved_directory')
        verify_payloads(children[0].fid, 'verify_after_preservation')
        await_rows('0', lambda rows: not any(row.name == name for row in rows), 'confirm_source_absent')
        log('complete', 'preservation')
        return {'ok': True, 'case_id': case, 'test_location': '/' + trash_name + '/' + name,
                'already_dispatched_read_only': dispatched, 'automatic_retries': 0, 'payload_bytes_sha256_verified': True}
    except (VfsError, OSError, ValueError) as error:
        log('outcome_unknown', 'preservation', error_code=getattr(error, 'code', 'LOCAL_IO_ERROR'))
        return {'ok': False, 'case_id': case, 'error_code': getattr(error, 'code', 'LOCAL_IO_ERROR'),
                'automatic_retries': 0, 'requires_reconciliation': True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--phase', choices=('pagination', 'download-records-tail', 'identities-collisions', 'rename', 'interrupted-resume', 'mixed-partial', 'preserve-known'), required=True)
    parser.add_argument('--case-id', choices=tuple(PRESERVATION_CASES), help='Explicit registered synthetic case for preserve-known; never an arbitrary FID')
    parser.add_argument('--resume-identities', help='Continue only the recorded collision-move checkpoint UUID without repeating its uploads or move')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.phase == 'preserve-known':
            if args.case_id is None or args.resume_identities is not None:
                raise ProviderError('INVALID_ARGUMENT', 'Known preservation requires a registered case-id only.')
            result = preserve_known_case(config, args.case_id)
        else:
            if args.case_id is not None:
                raise ProviderError('INVALID_ARGUMENT', 'Case-id is only accepted for known preservation.')
            harness = AcceptanceHarness(config, progress=lambda event: print(json.dumps(event), flush=True), resume_identities=args.resume_identities)
            result = harness.run(args.phase)
    except (VfsError, OSError, ValueError):
        result = {'ok': False, 'error_code': 'ACCEPTANCE_START_FAILED', 'automatic_retries': 0}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

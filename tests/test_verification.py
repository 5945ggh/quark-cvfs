import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_vfs.config import Config
from quark_vfs.provider.quark_cli import OfficialResult, ProviderError
from quark_vfs.verification import (
    VerificationError,
    Workflow,
    verify_download,
    verify_workflow,
)


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.rows = {'0': []}
        self.contents = {}
        self.next_id = 0
        self.partial = False
        self.corrupt = False
        self.fail_download = False
        self.different_upload_id = False

    def __call__(self, config, call):
        self.calls.append(call.action)
        lists = []
        if call.action == 'browse':
            rows = self.rows[call.parent_fid]
            artifact = call.local_path / 'synthetic.jsonl'
            artifact.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            data = {'file_path': str(artifact), 'count': len(rows), 'hasMore': False, 'parent_fid': call.parent_fid}
        elif call.action == 'create-folder':
            self.next_id += 1
            fid = 'folder-' + str(self.next_id)
            self.rows[fid] = []
            self.rows['0'].append({'fid': fid, 'parent_fid': '0', 'filename': call.name, 'file_type': '0'})
            data = {'fid': fid}
        elif call.action == 'upload':
            self.next_id += 1
            fid = 'file-' + str(self.next_id)
            content = call.local_path.read_bytes()
            self.contents[fid] = content
            self.rows[call.parent_fid].append({'fid': fid, 'parent_fid': call.parent_fid,
                                               'filename': call.local_path.name, 'size': len(content), 'file_type': '1'})
            upload_id = 'upload-response-' + fid if self.different_upload_id else fid
            detail = {'fileId': upload_id, 'fileName': call.local_path.name, 'fileSize': len(content)}
            lists = [{'type': 'list', 'action': 'upload', 'code': 1 if self.partial else 0, 'data': detail}]
            data = {'fids': [upload_id], 'fileCount': 1, 'successCount': 1}
        elif call.action == 'download':
            if self.fail_download:
                return OfficialResult(b'unknown transport response', 1)
            row = next(row for rows in self.rows.values() for row in rows if row['fid'] == call.fid)
            path = call.local_path / row['filename']
            content = self.contents[call.fid]
            path.write_bytes(b'x' * len(content) if self.corrupt else content)
            data = {'filePath': str(path), 'fileName': row['filename']}
        elif call.action == 'move':
            row = next(row for row in self.rows['0'] if row['fid'] == call.fid)
            self.rows['0'].remove(row)
            row['parent_fid'] = call.target_fid
            self.rows[call.target_fid].append(row)
            data = {'fids': [call.fid], 'targetFid': call.target_fid}
        else:
            raise AssertionError(call.action)
        events = lists + [{'type': 'result', 'action': call.action, 'code': 0, 'data': data}]
        return OfficialResult('\n'.join(json.dumps(event) for event in events).encode(), 0)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = Config(self.root / 'node', self.root / 'scripts' / 'quark-drive.cjs', self.root, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def run_fake(self, provider, resume=None):
        with patch('quark_vfs.verification.validate_runtime'), patch('quark_vfs.verification.run_official', side_effect=provider):
            return verify_workflow(self.config, resume=resume)

    def test_trusted_round_trip_and_verified_trash(self):
        provider = FakeProvider()
        value = self.run_fake(provider)
        self.assertEqual(value['completed_steps'], ['source_round_trip', 'processed_round_trip', 'moved_to_verified_trash'])
        self.assertIn('trusted_sha256_verified', value['verification_levels'])
        self.assertEqual(provider.calls.count('upload'), 2)
        self.assertEqual(provider.calls.count('download'), 4)
        self.assertNotIn('file-', json.dumps(value))
        self.assertEqual(list(self.root.glob('verification-*/browse-1')), [])
        journal = next(self.root.glob('verification-*/journal.jsonl'))
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_partial_upload_stops_without_retry_or_download(self):
        provider = FakeProvider()
        provider.partial = True
        with self.assertRaises(VerificationError) as error:
            self.run_fake(provider)
        self.assertEqual(error.exception.code, 'UPLOAD_PARTIAL')
        self.assertEqual(provider.calls.count('upload'), 1)
        self.assertNotIn('download', provider.calls)
        self.assertNotIn('move', provider.calls)

    def test_wrong_same_size_bytes_stop_before_second_upload(self):
        provider = FakeProvider()
        provider.corrupt = True
        with self.assertRaises(VerificationError) as error:
            self.run_fake(provider)
        self.assertEqual(error.exception.code, 'CONTENT_MISMATCH')
        self.assertEqual(provider.calls.count('upload'), 1)
        self.assertNotIn('move', provider.calls)

    def test_unknown_transport_stops_without_repeating_side_effect(self):
        provider = FakeProvider()
        provider.fail_download = True
        with self.assertRaises(VerificationError):
            self.run_fake(provider)
        self.assertEqual(provider.calls.count('upload'), 1)
        self.assertEqual(provider.calls.count('download'), 1)

    def test_download_symlink_and_escape_rejected(self):
        staging = self.root / 'staging'
        staging.mkdir()
        outside = self.root / 'outside.md'
        outside.write_bytes(b'synthetic')
        (staging / 'source.md').symlink_to(outside)
        for path in (str(staging / 'source.md'), str(outside)):
            with self.assertRaises(ProviderError) as error:
                verify_download(path, staging, 'source.md', b'synthetic')
            self.assertIn(error.exception.code, ('UNSAFE_DOWNLOAD_PATH', 'CONTENT_MISMATCH'))

    def test_staging_directory_symlink_is_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'source.md').write_bytes(b'synthetic')
        staging = self.root / 'staging'
        staging.symlink_to(outside, target_is_directory=True)
        for path in (staging / 'source.md', outside / 'source.md'):
            with self.assertRaises(ProviderError):
                verify_download(str(path), staging, 'source.md', b'synthetic')

    def test_upload_source_changed_stops_before_download(self):
        provider = FakeProvider()

        def changed(config, call):
            result = provider(config, call)
            if call.action == 'upload':
                call.local_path.write_bytes(b'x' * call.local_path.stat().st_size)
            return result

        with self.assertRaises(VerificationError) as error:
            self.run_fake(changed)
        self.assertEqual(error.exception.code, 'SOURCE_CHANGED')
        self.assertNotIn('download', provider.calls)

    def test_root_unknown_schema_also_cleans_real_listing(self):
        provider = FakeProvider()
        provider.rows['0'] = [{'fid': 'private', 'filename': 'private-root-name', 'parent_fid': '0', 'file_type': 'future'}]
        with self.assertRaises(VerificationError):
            self.run_fake(provider)
        self.assertEqual(list(self.root.glob('verification-*/browse-1')), [])
        journal = next(self.root.glob('verification-*/journal.jsonl')).read_text()
        self.assertNotIn('private-root-name', journal)
        self.assertNotIn('create-folder', provider.calls)

    def test_opaque_row_parent_does_not_replace_request_scope(self):
        provider = FakeProvider()
        provider.rows['0'] = [{'fid': 'synthetic-existing', 'parent_fid': 'opaque-parent',
                               'filename': 'synthetic-existing.md', 'file_type': '1', 'size': 1}]
        value = self.run_fake(provider)
        self.assertIn('moved_to_verified_trash', value['completed_steps'])

    def test_terminal_parent_mismatch_still_stops_before_cloud_write(self):
        provider = FakeProvider()

        def wrong_scope(config, call):
            output = provider(config, call)
            if call.action == 'browse':
                event = json.loads(output.stdout)
                event['data']['parent_fid'] = 'wrong-query-scope'
                return OfficialResult(json.dumps(event).encode(), 0)
            return output

        with self.assertRaises(VerificationError) as error:
            self.run_fake(wrong_scope)
        self.assertEqual(error.exception.code, 'INCOMPLETE_ENUMERATION')
        self.assertEqual(provider.calls, ['browse'])

    def test_upload_and_browse_id_domains_are_not_equated(self):
        provider = FakeProvider()
        provider.different_upload_id = True
        value = self.run_fake(provider)
        self.assertIn('moved_to_verified_trash', value['completed_steps'])
        self.assertEqual(provider.calls.count('upload'), 2)

    def test_source_checkpoint_resume_never_uploads_source_again(self):
        provider = FakeProvider()
        provider.different_upload_id = True

        def stop_after_upload(config, call):
            if call.action == 'browse' and provider.contents:
                raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic injected checkpoint stop')
            return provider(config, call)

        with self.assertRaises(VerificationError) as error:
            self.run_fake(stop_after_upload)
        run_id = error.exception.summary['verification_id']
        self.assertEqual(error.exception.summary['stopped_at_step'], 'source_round_trip_enumerate')
        self.assertEqual(provider.calls.count('upload'), 1)
        value = self.run_fake(provider, resume=run_id)
        self.assertTrue(value['resumed_source_upload'])
        self.assertEqual(provider.calls.count('upload'), 2)  # One source, one processed; no retry.
        self.assertIn('moved_to_verified_trash', value['completed_steps'])

    def test_resume_refuses_unknown_upload_outcome(self):
        provider = FakeProvider()
        provider.partial = True
        with self.assertRaises(VerificationError) as error:
            self.run_fake(provider)
        prior_calls = len(provider.calls)
        with self.assertRaises(VerificationError) as refused:
            self.run_fake(provider, resume=error.exception.summary['verification_id'])
        self.assertEqual(refused.exception.code, 'RESUME_NOT_ALLOWED')
        self.assertEqual(len(provider.calls), prior_calls)

    def test_resume_refuses_modified_source_before_network(self):
        provider = FakeProvider()

        def stop_after_upload(config, call):
            if call.action == 'browse' and provider.contents:
                raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic stop')
            return provider(config, call)

        with self.assertRaises(VerificationError) as error:
            self.run_fake(stop_after_upload)
        run_id = error.exception.summary['verification_id']
        source = self.root / ('verification-' + run_id) / 'source.md'
        source.write_bytes(b'x' * source.stat().st_size)
        prior_calls = len(provider.calls)
        with self.assertRaises(VerificationError):
            self.run_fake(provider, resume=run_id)
        self.assertEqual(len(provider.calls), prior_calls)

    def test_browse_identity_rule_only_uses_raw_nonempty_tail(self):
        self.assertEqual(Workflow.browse_identity('prefix|opaque '), 'opaque ')
        self.assertEqual(Workflow.browse_identity('prefix|'), 'prefix|')
        self.assertEqual(Workflow.browse_identity('no-pipe'), 'no-pipe')

    def make_checkpoint(self):
        provider = FakeProvider()

        def stop_after_upload(config, call):
            if call.action == 'browse' and provider.contents:
                raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic checkpoint')
            return provider(config, call)

        with self.assertRaises(VerificationError) as error:
            self.run_fake(stop_after_upload)
        run_id = error.exception.summary['verification_id']
        return provider, run_id, self.root / ('verification-' + run_id) / 'journal.jsonl'

    def test_resume_rejects_later_intent_or_arbitrary_source_path(self):
        for change in ('processed_intent', 'arbitrary_path', 'failed_business'):
            provider, run_id, journal = self.make_checkpoint()
            records = [json.loads(line) for line in journal.read_text().splitlines()]
            if change == 'processed_intent':
                records.insert(-1, {'event': 'intent', 'step': 'processed_round_trip_upload', 'action': 'upload'})
            elif change == 'arbitrary_path':
                next(r for r in records if r.get('step') == 'source_round_trip_upload' and r.get('event') == 'intent')['local_path'] = '/unrelated/private-file'
            else:
                next(r for r in records if r.get('step') == 'source_round_trip_upload' and r.get('event') == 'business_response_observed')['code'] = 123
            journal.write_text(''.join(json.dumps(record) + '\n' for record in records))
            prior_calls = len(provider.calls)
            with self.assertRaises(VerificationError) as error:
                self.run_fake(provider, resume=run_id)
            self.assertEqual(error.exception.code, 'RESUME_NOT_ALLOWED')
            self.assertEqual(len(provider.calls), prior_calls)

    def test_resume_extra_remote_item_stops_without_upload(self):
        provider, run_id, _ = self.make_checkpoint()
        test_fid = next(fid for fid, rows in provider.rows.items() if fid != '0' and rows)
        provider.rows[test_fid].append({'fid': 'unknown-extra', 'parent_fid': test_fid,
                                        'filename': 'unexpected.md', 'file_type': '1', 'size': 1})
        upload_count = provider.calls.count('upload')
        with self.assertRaises(VerificationError):
            self.run_fake(provider, resume=run_id)
        self.assertEqual(provider.calls.count('upload'), upload_count)
        self.assertNotIn('download', provider.calls)

    def moved_identity_provider(self, *, corrupt=False):
        provider = FakeProvider()

        def changed(config, call):
            output = provider(config, call)
            if call.action == 'move':
                rows = provider.rows.pop(call.fid)
                moved_directory_id = 'moved-directory|fresh-object'
                provider.rows[call.target_fid][0]['fid'] = moved_directory_id
                provider.rows[moved_directory_id] = rows
                for row in rows:
                    previous = row['fid']
                    fresh = 'moved-file|' + previous
                    row['fid'] = fresh
                    row['parent_fid'] = 'opaque-moved-parent'
                    content = provider.contents.pop(previous)
                    provider.contents[fresh] = b'x' * len(content) if corrupt else content
            return output

        return changed

    def test_moved_directory_and_file_id_changes_are_content_verified(self):
        value = self.run_fake(self.moved_identity_provider())
        self.assertIn('moved_to_verified_trash', value['completed_steps'])

    def test_moved_same_name_size_changed_content_is_not_verified_trash(self):
        with self.assertRaises(VerificationError) as error:
            self.run_fake(self.moved_identity_provider(corrupt=True))
        self.assertEqual(error.exception.code, 'CONTENT_MISMATCH')
        self.assertNotIn('moved_to_verified_trash', error.exception.summary['completed_steps'])
        self.assertTrue(error.exception.summary['last_known_location_only'])

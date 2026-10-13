import json
import tempfile
import unittest
from pathlib import Path

from quark_vfs.config import Config
from quark_vfs.provider.quark import (
    QuarkProvider,
    decode_upload_probe,
    known_upload_acknowledgements,
    upload_probe_diagnostics,
)
from quark_vfs.provider.quark_cli import (
    OfficialResult,
    ProviderCall,
    ProviderError,
    build_argv,
)


def output(action, data, *, items=(), code=0, exit_code=0, artifacts=()):
    events = [*items, {'type': 'result', 'action': action, 'code': code, 'data': data}, *artifacts]
    return OfficialResult('\n'.join(json.dumps(event) for event in events).encode(), exit_code)


class QuarkProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = Config(self.root / 'node', self.root / 'official' / 'scripts' / 'quark-drive.cjs', self.root, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_105_rows_not_preview_and_opaque_parent(self):
        def runner(config, call):
            artifact = call.local_path / 'entries.jsonl'
            artifact.write_text(''.join(json.dumps({'fid': f'full|{i}', 'parent_fid': f'opaque-{i}',
                                                    'filename': f'file-{i}', 'file_type': '1', 'size': 1}) + '\n' for i in range(105)))
            return output('browse', {'parent_fid': '0', 'count': 105, 'hasMore': False,
                                     'file_path': str(artifact), 'file_list': [{'fid': 'preview'}]})
        provider = QuarkProvider(self.config, runner=runner)
        rows = provider.list_directory('scope', '0')
        self.assertEqual(len(rows), 105)
        self.assertEqual(rows[0].parent_fid, '0')
        self.assertEqual(rows[0].raw_parent_fid, 'opaque-0')
        self.assertEqual(list(self.root.glob('provider-*')), [])

    def test_incomplete_or_scope_mismatch_refuses_snapshot(self):
        for change in ({'hasMore': True}, {'parent_fid': 'wrong'}, {'count': True}):
            def runner(config, call, change=change):
                artifact = call.local_path / 'entries.jsonl'
                artifact.write_text('')
                return output('browse', {'parent_fid': '0', 'count': 0, 'hasMore': False, 'file_path': str(artifact)} | change)
            with self.assertRaises(ProviderError):
                QuarkProvider(self.config, runner=runner).list_directory('scope', '0')

    def test_partial_upload_keeps_successful_native_id_and_record(self):
        source = self.root / 'synthetic.md'
        source.write_bytes(b'x')
        item = {'type': 'list', 'action': 'upload', 'code': 0,
                'data': {'fileId': 'upload-domain-id', 'recordId': 'record', 'fileName': source.name, 'fileSize': 1}}
        provider = QuarkProvider(self.config, runner=lambda *_: output('upload', {}, items=(item,), code=-1, exit_code=1))
        result = provider.upload_file('scope', source, 'complete-parent|opaque')
        self.assertEqual(result.outcome, 'partial')
        self.assertEqual(result.native_id, 'upload-domain-id')
        self.assertEqual(result.record_id, 'record')

    def test_download_symlink_parent_rejected_before_invocation(self):
        outside = self.root / 'outside'
        outside.mkdir()
        link = self.root / 'link'
        link.symlink_to(outside, target_is_directory=True)
        (outside / 'staging').mkdir()
        calls = []
        provider = QuarkProvider(self.config, runner=lambda *args: calls.append(args))
        with self.assertRaises(ProviderError):
            provider.download_file('scope', 'full|fid', link / 'staging')
        self.assertEqual(calls, [])

    def test_returned_download_symlink_is_not_accepted(self):
        staging = self.root / 'staging'
        staging.mkdir()
        outside = self.root / 'outside.md'
        outside.write_bytes(b'x')
        def runner(config, call):
            (staging / 'synthetic.md').symlink_to(outside)
            return output('download', {'fileName': 'synthetic.md', 'filePath': str(staging / 'synthetic.md')})
        with self.assertRaises(ProviderError):
            QuarkProvider(self.config, runner=runner).download_file('scope', 'full|fid', staging)

    def test_rename_explicit_mapping_and_partial_is_unknown(self):
        mappings = []
        def runner(config, call):
            mappings.append(json.loads(call.local_path.read_text()))
            return output('rename', {'batch_id': call.batch_id, 'result_status': 'PARTIAL_SUCCESS',
                                     'total': 1, 'success_count': 0, 'fail_count': 0, 'unknown_count': 1, 'pending_count': 0})
        with self.assertRaises(ProviderError):
            QuarkProvider(self.config, runner=runner).rename('scope', 'full|fid', 'old.md', 'new.md')
        self.assertEqual(mappings[0]['items'], [{'fid': 'full|fid', 'old_name': 'old.md', 'new_name': 'new.md'}])

    def test_no_raw_argument_or_delete_entry(self):
        for action in ('delete', 'raw', 'upload delete'):
            with self.assertRaises(ProviderError):
                build_argv(self.config, ProviderCall(action))
        argv = build_argv(self.config, ProviderCall('move', fids=('full|fid',), target_fid='target|opaque'))
        self.assertIn('full|fid', argv)
        self.assertNotIn('--overwrite', argv)

    def test_account_identity_does_not_hash_nickname(self):
        self.assertIsNone(QuarkProvider(self.config).account_identity())

    def test_mixed_partial_fixture_retains_only_known_success(self):
        items = ({'type': 'list', 'action': 'upload', 'code': 0,
                  'data': {'fileName': 'readable.md', 'fileId': 'native-success', 'recordId': 'record-success', 'fileSize': 12}},
                 {'type': 'list', 'action': 'upload', 'code': -1,
                  'data': {'fileName': 'unreadable.md', 'recordId': 'record-failed', 'fileSize': 12}})
        observation, rows, _, _ = decode_upload_probe(output('upload', {'successCount': 1}, items=items, code=-1, exit_code=1),
                                                      ('readable.md', 'unreadable.md'))
        self.assertEqual(observation, 'partial')
        self.assertEqual(rows[0].outcome, 'success')
        self.assertEqual(rows[0].native_id, 'native-success')
        self.assertEqual(rows[1].outcome, 'failed')
        self.assertIsNone(rows[1].native_id)

    def test_mixed_prevalidation_failure_is_not_partial(self):
        observation, rows, _, _ = decode_upload_probe(output('upload', {}, code=-1, exit_code=1), ('a.md', 'b.md'))
        self.assertEqual(observation, 'no_items_reported')
        self.assertEqual(rows, ())

    def test_failure_record_only_requires_exact_record_join_not_order_guess(self):
        items = ({'type': 'list', 'action': 'upload', 'code': -1, 'data': {'recordId': 'failed-record'}},
                 {'type': 'list', 'action': 'upload', 'code': 0,
                  'data': {'fileName': 'good.md', 'fileSize': 1, 'fileId': 'native-good', 'recordId': 'good-record'}})
        result = output('upload', {'failedCount': 1, 'successCount': 1}, items=items, code=-1, exit_code=1)
        with self.assertRaises(ProviderError):
            decode_upload_probe(result, ('good.md', 'blocked.md'))
        outcome, rows, _, _ = decode_upload_probe(result, ('good.md', 'blocked.md'), record_names={'failed-record': 'blocked.md'})
        self.assertEqual(outcome, 'partial')
        self.assertEqual(rows[0].name, 'blocked.md')
        self.assertEqual(rows[1].native_id, 'native-good')
        known = known_upload_acknowledgements(result, {'good.md': 1, 'blocked.md': 1})
        self.assertEqual(known[0].native_id, 'native-good')

    def test_protocol_diagnostics_cannot_leak_raw_message_token_or_name(self):
        result = output('upload', {'fileName': 'private-name', 'token': 'secret-token'}, code=-1, exit_code=1)
        values = str(upload_probe_diagnostics(result))
        self.assertNotIn('private-name', values)
        self.assertNotIn('secret-token', values)
        self.assertIn('fileName', values)

    def test_single_failed_upload_record_is_retained_without_inventing_size(self):
        source = self.root / 'source.md'
        source.write_bytes(b'x')
        item = {'type': 'list', 'action': 'upload', 'code': -1, 'data': {'recordId': 'failed-record'}}
        result = QuarkProvider(self.config, runner=lambda *_: output('upload', {}, items=(item,), code=-1, exit_code=1)).upload_file('scope', source, 'parent')
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(result.record_id, 'failed-record')
        self.assertIsNone(result.native_id)
        self.assertIsNone(result.size)

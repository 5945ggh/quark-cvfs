import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_vfs.config import Config
from quark_vfs.models import DownloadResult, MutationResult, RemoteEntry, UploadResult
from quark_vfs.provider.quark_cli import OfficialResult, ProviderError
from tools.m0_acceptance import AcceptanceHarness, preserve_known_case


class SyntheticProvider:
    last_notices = ()

    def __init__(self):
        self.rows = {'0': []}
        self.contents = {}
        self.counter = 0
        self.upload_count = 0
        self.moves = 0
        self.fail_at = None
        self.records = []

    def list_directory(self, account, fid):
        return tuple(self.rows[fid])

    def mkdir(self, account, parent, name):
        existing = [row for row in self.rows[parent] if row.kind == 'directory' and row.name == name]
        if existing:
            return existing[0].fid
        self.counter += 1
        fid = 'directory-' + str(self.counter)
        self.rows[fid] = []
        self.rows[parent].append(RemoteEntry(fid, parent, name, 'directory'))
        return fid

    def upload_file(self, account, source, parent):
        self.upload_count += 1
        self.counter += 1
        fid = 'full|file-' + str(self.counter)
        content = source.read_bytes()
        self.contents[fid] = content
        self.rows[parent].append(RemoteEntry(fid, parent, source.name, 'file', len(content)))
        if self.upload_count == self.fail_at:
            return UploadResult('native-domain', 'record', len(content), source.name, 'partial')
        return UploadResult('native-domain-' + str(self.counter), 'record-' + str(self.counter), len(content), source.name, 'success')

    def download_file(self, account, fid, staging):
        entry = next(row for rows in self.rows.values() for row in rows if row.fid == fid)
        path = staging / entry.name
        path.write_bytes(self.contents[fid])
        self.records.append({'recordId': 'download-record-' + str(len(self.records)), 'fileName': entry.name,
                             'fileSize': entry.size, 'fid': fid})
        return DownloadResult(path, entry.name)

    def move(self, account, fid, target):
        parent = next(parent for parent, rows in self.rows.items() if any(row.fid == fid for row in rows))
        entry = next(row for row in self.rows[parent] if row.fid == fid)
        if any(row.name == entry.name for row in self.rows[target]):
            raise ProviderError('UPSTREAM_FAILED', 'Synthetic move collision rejected')
        self.rows[parent].remove(entry)
        self.rows[target].append(RemoteEntry(fid, target, entry.name, entry.kind, entry.size))
        self.moves += 1
        return MutationResult(True)

    def rename(self, account, fid, old_name, new_name):
        parent = next(parent for parent, rows in self.rows.items() if any(row.fid == fid for row in rows))
        entry = next(row for row in self.rows[parent] if row.fid == fid)
        if any(row.name == new_name for row in self.rows[parent]):
            raise ProviderError('UPSTREAM_FAILED', 'Synthetic rename collision rejected')
        self.rows[parent].remove(entry)
        self.rows[parent].append(RemoteEntry(fid, parent, new_name, entry.kind, entry.size))
        return MutationResult(True)

    def download_records(self):
        return tuple(dict(row) for row in self.records)

    def _successful(self, request, action):
        if request.action == 'download-resume':
            record = next(row for row in self.records if row['recordId'] == request.record_id)
            name = record['fileName']
            (request.local_path / name).write_bytes(self.contents[record['fid']])
        elif request.action == 'download-overwrite':
            row = next(row for rows in self.rows.values() for row in rows if row.fid == request.fid)
            name = row.name
            with (request.local_path / name).open('r+b') as stream:
                stream.write(self.contents[request.fid])  # Deliberately reproduce untruncated old-tail risk.
        else:
            raise AssertionError(request.action)
        return {'fileName': name, 'filePath': str(request.local_path / name)}, None

    def _safe_download_result(self, data, staging):
        return DownloadResult(Path(data['filePath']), data['fileName'])


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.config = Config(self.root / 'node', self.root / 'official' / 'scripts' / 'quark-drive.cjs', self.root, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_pagination_105_and_preservation(self):
        provider = SyntheticProvider()
        updates = []
        with patch('tools.m0_acceptance.validate_runtime'):
            value = AcceptanceHarness(self.config, provider, progress=updates.append).run('pagination')
        self.assertTrue(value['ok'])
        self.assertEqual(value['checks']['pagination_105'], 'passed')
        self.assertEqual(provider.upload_count, 105)
        self.assertEqual(provider.moves, 1)
        self.assertEqual(updates[-1]['completed'], 100)
        self.assertFalse(value['gates_changed'])
        self.assertNotIn('full|file', str(value))

    def test_preservation_unknown_extra_object_never_moves(self):
        provider = SyntheticProvider()
        harness = AcceptanceHarness(self.config, provider)
        harness.root.mkdir()
        parent = provider.mkdir('default', '0', harness.name)
        provider.rows[parent].append(RemoteEntry('unexpected', parent, 'external.md', 'file', 1))
        with self.assertRaises(ProviderError):
            harness.preserve(parent)
        self.assertEqual(provider.moves, 0)
        self.assertFalse(any(row.name == harness.trash_name for row in provider.rows['0']))

    def test_preservation_readback_lag_never_repeats_move(self):
        provider = SyntheticProvider()
        original = provider.list_directory
        hidden = []
        def lagged(account, fid):
            if provider.moves and fid != '0' and any(row.kind == 'directory' for row in provider.rows[fid]):
                hidden.append(fid)
                if len(hidden) < 3:
                    return ()
            return original(account, fid)
        provider.list_directory = lagged
        with patch('tools.m0_acceptance.validate_runtime'), patch('tools.m0_acceptance.time.sleep'):
            result = AcceptanceHarness(self.config, provider).run('rename')
        self.assertTrue(result['ok'])
        self.assertEqual(provider.moves, 1)
        self.assertGreaterEqual(len(hidden), 3)

    def test_preservation_readback_exhaustion_keeps_unknown_without_move_retry(self):
        provider = SyntheticProvider()
        original = provider.list_directory
        def absent(account, fid):
            if provider.moves and fid != '0' and any(row.kind == 'directory' for row in provider.rows[fid]):
                return ()
            return original(account, fid)
        provider.list_directory = absent
        with patch('tools.m0_acceptance.validate_runtime'), patch('tools.m0_acceptance.time.sleep'):
            result = AcceptanceHarness(self.config, provider).run('rename')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'OUTCOME_UNKNOWN')
        self.assertEqual(provider.moves, 1)
        self.assertNotIn('preserved_to_test_container', result['checks'])

    def test_partial_item_stops_and_never_replays_batch_or_moves(self):
        provider = SyntheticProvider()
        provider.fail_at = 3
        with patch('tools.m0_acceptance.validate_runtime'):
            value = AcceptanceHarness(self.config, provider).run('pagination')
        self.assertFalse(value['ok'])
        self.assertEqual(value['error_code'], 'UPLOAD_PARTIAL')
        self.assertEqual(provider.upload_count, 3)
        self.assertEqual(provider.moves, 0)
        self.assertEqual(value['automatic_retries'], 0)
        journal = next(self.root.glob('m0-acceptance-*/journal.jsonl'))
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)
        self.assertIn('native-domain', journal.read_text())

    def test_unknown_after_side_effect_stops_without_retry(self):
        provider = SyntheticProvider()
        original = provider.upload_file
        def failed(*args):
            original(*args)
            raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic lost response')
        provider.upload_file = failed
        with patch('tools.m0_acceptance.validate_runtime'):
            value = AcceptanceHarness(self.config, provider).run('pagination')
        self.assertFalse(value['ok'])
        self.assertEqual(provider.upload_count, 1)
        self.assertEqual(provider.moves, 0)

    def test_identity_and_collision_probes_remain_synthetic(self):
        provider = SyntheticProvider()
        with patch('tools.m0_acceptance.validate_runtime'):
            value = AcceptanceHarness(self.config, provider).run('identities-collisions')
        self.assertTrue(value['ok'])
        self.assertEqual(value['checks']['move_name_collision'], 'rejected_both_contents_preserved')
        self.assertEqual(value['checks']['rename_name_collision'], 'rejected_both_contents_preserved')
        self.assertEqual(value['checks']['mkdir_same_name'], 'single_directory_observed')
        self.assertEqual(provider.moves, 1)

    def test_records_and_tail_risk_never_enable_resume_gate(self):
        provider = SyntheticProvider()
        with patch('tools.m0_acceptance.validate_runtime'):
            value = AcceptanceHarness(self.config, provider).run('download-records-tail')
        self.assertTrue(value['ok'])
        self.assertEqual(value['checks']['download_record_association'], 'passed')
        self.assertEqual(value['checks']['completed_task_resume_same_staging'], 'passed')
        self.assertEqual(value['checks']['interrupted_download_resume'], 'not_tested')
        self.assertEqual(value['checks']['short_file_overwrite'], 'failed_content_mismatch')
        self.assertFalse(value['gates_changed'])

    def keep_both_provider(self):
        provider = SyntheticProvider()
        original = provider.move
        def keep_both(account, fid, target):
            parent = next(parent for parent, rows in provider.rows.items() if any(row.fid == fid for row in rows))
            entry = next(row for row in provider.rows[parent] if row.fid == fid)
            if entry.kind == 'file' and any(row.name == entry.name for row in provider.rows[target]):
                provider.rows[parent].remove(entry)
                provider.rows[target].append(RemoteEntry(fid, target, 'collision(1).md', 'file', entry.size))
                provider.moves += 1
                return MutationResult(True)
            return original(account, fid, target)
        provider.move = keep_both
        return provider

    def test_provider_keep_both_new_name_is_content_verified(self):
        provider = self.keep_both_provider()
        with patch('tools.m0_acceptance.validate_runtime'):
            value = AcceptanceHarness(self.config, provider).run('identities-collisions')
        self.assertTrue(value['ok'])
        self.assertEqual(value['checks']['move_name_collision'], 'kept_both_with_provider_names_content_verified')

    def test_confirmed_identity_checkpoint_continues_without_repeating_move_uploads(self):
        provider = self.keep_both_provider()
        first = AcceptanceHarness(self.config, provider)
        original_browse = first.browse
        def stop_target(fid, step):
            rows = original_browse(fid, step)
            if step == 'observe_move_target':
                raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic old same-name-only assumption')
            return rows
        first.browse = stop_target
        with patch('tools.m0_acceptance.validate_runtime'):
            stopped = first.run('identities-collisions')
        self.assertFalse(stopped['ok'])
        self.assertEqual(provider.upload_count, 2)
        self.assertEqual(provider.moves, 1)
        with patch('tools.m0_acceptance.validate_runtime'):
            resumed = AcceptanceHarness(self.config, provider, resume_identities=first.id).run('identities-collisions')
        self.assertTrue(resumed['ok'])
        self.assertEqual(provider.upload_count, 4)  # The two unstarted rename samples only.
        self.assertEqual(provider.moves, 2)  # Original collision move + final container preservation.
        self.assertTrue(resumed['checks']['resumed_without_repeating_move_or_uploads'])

    def test_identity_resume_unknown_checkpoint_rejected_before_cloud(self):
        provider = SyntheticProvider()
        first = AcceptanceHarness(self.config, provider)
        first.root.mkdir(mode=0o700)
        first.log('outcome_unknown', 'unrelated_operation')
        with patch('tools.m0_acceptance.validate_runtime'):
            value = AcceptanceHarness(self.config, provider, resume_identities=first.id).run('identities-collisions')
        self.assertFalse(value['ok'])
        self.assertEqual(value['error_code'], 'RESUME_NOT_ALLOWED')
        self.assertEqual(provider.counter, 0)

    def interrupted_result(self, provider, *, create_record=True):
        def stopped(config, request):
            expected = provider.contents[request.fid]
            (request.local_path / request.expected_name).write_bytes(expected[:262144])
            if create_record:
                provider.records.append({'recordId': 'synthetic-interrupted', 'fileName': request.expected_name,
                                         'fileSize': request.expected_size, 'fid': request.fid})
            return OfficialResult(b'', -9, interrupted_size=262144)
        return stopped

    def test_interrupted_task_unique_record_same_staging_bytes_verified(self):
        provider = SyntheticProvider()
        with patch('tools.m0_acceptance.validate_runtime'), patch('tools.m0_acceptance.run_official', side_effect=self.interrupted_result(provider)):
            result = AcceptanceHarness(self.config, provider).run('interrupted-resume')
        self.assertTrue(result['ok'])
        self.assertEqual(result['checks']['interrupted_download_resume'], 'passed_same_record_same_staging_trusted_sha256')
        self.assertEqual(provider.upload_count, 1)
        self.assertFalse(result['gates_changed'])

    def test_interrupted_task_no_record_never_guesses_or_resumes(self):
        provider = SyntheticProvider()
        def forbidden(*args):
            raise AssertionError('Must not attempt resume with an unknown record')
        provider._successful = forbidden
        with patch('tools.m0_acceptance.validate_runtime'), patch('tools.m0_acceptance.run_official', side_effect=self.interrupted_result(provider, create_record=False)):
            result = AcceptanceHarness(self.config, provider).run('interrupted-resume')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'RECORD_ASSOCIATION_UNKNOWN')
        self.assertEqual(provider.upload_count, 1)
        self.assertEqual(provider.moves, 0)

    def test_post_write_readback_lag_retries_only_reads(self):
        provider = SyntheticProvider()
        harness = AcceptanceHarness(self.config, provider)
        harness.root.mkdir(mode=0o700)
        attempts = []
        expected = RemoteEntry('full|known', 'synthetic-parent', 'known.md', 'file', 1)
        def delayed(account, parent):
            attempts.append(parent)
            return () if len(attempts) < 4 else (expected,)
        provider.list_directory = delayed
        with patch('tools.m0_acceptance.time.sleep'):
            row = harness.unique('synthetic-parent', 'known.md', b'x')
        self.assertEqual(row.fid, 'full|known')
        self.assertEqual(len(attempts), 4)
        self.assertEqual(provider.upload_count, 0)

    def test_uploaded_rename_pair_checkpoint_does_not_reupload_on_resume(self):
        provider = self.keep_both_provider()
        first = AcceptanceHarness(self.config, provider)
        original_browse = first.browse
        def first_stop(fid, step):
            rows = original_browse(fid, step)
            if step == 'observe_move_target':
                raise ProviderError('OUTCOME_UNKNOWN', 'Synthetic old collision assumption')
            return rows
        first.browse = first_stop
        with patch('tools.m0_acceptance.validate_runtime'):
            first.run('identities-collisions')
        resumed = AcceptanceHarness(self.config, provider, resume_identities=first.id)
        original_list = provider.list_directory
        def temporarily_missing(account, fid):
            rows = original_list(account, fid)
            if provider.upload_count == 4:
                return tuple(row for row in rows if row.name != 'occupied.md')
            return rows
        provider.list_directory = temporarily_missing
        with patch('tools.m0_acceptance.validate_runtime'), patch('tools.m0_acceptance.time.sleep'):
            stopped = resumed.run('identities-collisions')
        self.assertFalse(stopped['ok'])
        self.assertEqual(stopped['stopped_at_step'], 'locate_occupied.md')
        self.assertEqual(provider.upload_count, 4)
        provider.list_directory = original_list
        with patch('tools.m0_acceptance.validate_runtime'):
            final = AcceptanceHarness(self.config, provider, resume_identities=first.id).run('identities-collisions')
        self.assertTrue(final['ok'])
        self.assertEqual(provider.upload_count, 4)
        self.assertEqual(provider.moves, 2)

    def test_controlled_mixed_partial_restores_permissions_and_keeps_success(self):
        provider = SyntheticProvider()
        def mixed(config, request):
            good = provider.upload_file('m0', request.local_paths[0], request.fid)
            events = [{'type': 'list', 'action': 'upload', 'code': 0,
                       'data': {'fileName': good.name, 'fileSize': good.size, 'fileId': good.native_id, 'recordId': good.record_id}},
                      {'type': 'list', 'action': 'upload', 'code': -1,
                       'data': {'fileName': request.local_paths[1].name}},
                      {'type': 'result', 'action': 'upload', 'code': -1, 'data': {'successCount': 1}}]
            return OfficialResult('\n'.join(json.dumps(event) for event in events).encode(), 1)
        with patch('tools.m0_acceptance.validate_runtime'), patch('tools.m0_acceptance.os.access', return_value=False), patch('tools.m0_acceptance.run_official', side_effect=mixed):
            harness = AcceptanceHarness(self.config, provider)
            value = harness.run('mixed-partial')
        self.assertTrue(value['ok'])
        self.assertEqual(value['checks']['mixed_upload_partial'], 'partial_success_retained_and_bytes_verified_no_batch_retry')
        self.assertEqual(provider.upload_count, 1)
        self.assertEqual((harness.root / 'mixed-unreadable.md').stat().st_mode & 0o777, 0o600)

    def preservation_fixture(self, case, *, already_moved=False):
        provider = SyntheticProvider()
        name = 'QuarkVFS-M0-Acceptance-' + case
        target_name = 'QuarkVFS-M0-Acceptance-Trash-' + case
        parent = provider.mkdir('m0', '0', name)
        path = self.root / ('record-' + case + '.md' if case.startswith('9a1') else 'mixed-readable.md')
        path.write_bytes(b'Synthetic short file.\n' if case.startswith('9a1') else b'Synthetic readable mixed item.\n')
        provider.upload_file('m0', path, parent)
        old = self.root / ('m0-acceptance-' + case)
        old.mkdir(mode=0o700)
        records = [{'event': 'intent', 'step': 'create_test_directory', 'name': name, 'parent_fid': '0'},
                   {'event': 'fact', 'step': 'create_test_directory', 'create_response_id': parent}]
        if already_moved:
            target = provider.mkdir('m0', '0', target_name)
            provider.move('m0', parent, target)
            records.append({'event': 'intent', 'step': 'move_to_preservation', 'fid': parent, 'target_fid': target})
        (old / 'journal.jsonl').write_text(''.join(json.dumps(record) + '\n' for record in records))
        return provider

    def test_known_source_preservation_dispatches_move_once(self):
        case = '9a1f345c-af8e-4044-a2f4-5346224663bf'
        provider = self.preservation_fixture(case)
        with patch('tools.m0_acceptance.validate_runtime'):
            result = preserve_known_case(self.config, case, provider=provider)
        self.assertTrue(result['ok'])
        self.assertEqual(provider.moves, 1)
        with patch('tools.m0_acceptance.validate_runtime'):
            repeated = preserve_known_case(self.config, case, provider=provider)
        self.assertTrue(repeated['ok'])
        self.assertTrue(repeated['already_dispatched_read_only'])
        self.assertEqual(provider.moves, 1)

    def test_already_dispatched_preservation_is_read_only(self):
        case = '5b17cbf8-77d5-48eb-94ba-1b593a3fd386'
        provider = self.preservation_fixture(case, already_moved=True)
        with patch('tools.m0_acceptance.validate_runtime'):
            result = preserve_known_case(self.config, case, provider=provider)
        self.assertTrue(result['ok'])
        self.assertEqual(provider.moves, 1)
        self.assertTrue(result['already_dispatched_read_only'])

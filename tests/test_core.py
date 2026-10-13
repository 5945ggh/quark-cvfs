"""Behavior tests for shared semantics using a synthetic in-memory provider."""
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from quark_vfs.core.catalog import Catalog
from quark_vfs.core.jobs import Jobs
from quark_vfs.core.operations import Operations
from quark_vfs.core.transfers import MAX_TRANSFER_BYTES, Transfers, publish_no_replace
from quark_vfs.models import (
    DownloadResult,
    Expected,
    FileRef,
    MutationResult,
    RemoteEntry,
    ScopeBinding,
    UploadResult,
    VfsError,
)
from quark_vfs.storage import Store


class FakeProvider:
    def __init__(self):
        self.rows = {'0': []}
        self.content = {'marker': b'synthetic-private-scope'}
        self.names = {'marker': 'scope-marker.json'}
        self.rows['marker-parent'] = [RemoteEntry('marker', 'marker-parent', 'scope-marker.json', 'file', len(self.content['marker']))]
        self.serial = 0
        self.upload_calls = []
        self.mutations = []
        self.fail_upload = None
        self.fail_download = None
        self.download_calls = []
        self.mutate_source = None
        self.corrupt_download = None
        self.visibility_delay = 0
        self.hidden = {}

    def list_directory(self, account, parent_fid):
        visible = []
        for entry in self.rows[parent_fid]:
            if self.hidden.get(entry.fid, 0):
                self.hidden[entry.fid] -= 1
            else:
                visible.append(entry)
        return tuple(visible)

    def search(self, account, keyword, parent_fid=None):
        return tuple(replace(e, parent_fid=None) for rows in self.rows.values() for e in rows if keyword in e.name)

    def mkdir(self, account, parent_fid, name):
        self.serial += 1
        fid = 'browse-dir|' + str(self.serial)
        self.rows.setdefault(fid, [])
        self.rows[parent_fid].append(RemoteEntry(fid, 'opaque-parent', name, 'directory'))
        self.hidden[fid] = self.visibility_delay
        self.mutations.append(('mkdir', name))
        return 'native-dir-' + str(self.serial)

    def upload_file(self, account, source, parent_fid):
        self.upload_calls.append(source.name)
        if self.fail_upload == source.name:
            raise VfsError('LOCATION_UNKNOWN', 'Synthetic response lost after dispatch.')
        self.serial += 1
        fid = 'browse-file|' + str(self.serial)
        data = source.read_bytes()
        self.content[fid] = data
        self.names[fid] = source.name
        self.rows[parent_fid].append(RemoteEntry(fid, 'opaque-parent', source.name, 'file', len(data)))
        self.hidden[fid] = self.visibility_delay
        if self.mutate_source is not None:
            self.mutate_source.write_bytes(b'changed source')
        return UploadResult('native-' + str(self.serial), 'record-' + str(self.serial), len(data), source.name, 'success')

    def download_file(self, account, fid, staging):
        self.download_calls.append((fid, staging))
        path = staging / self.names[fid]
        if self.fail_download == fid:
            path.write_bytes(b'partial')
            raise VfsError('DOWNLOAD_INTERRUPTED', 'Synthetic foreground interruption.')
        path.write_bytes(self.content[fid])
        if self.corrupt_download == fid:
            path.write_bytes(self.content[fid] + b'synthetic-old-tail')
        return DownloadResult(path, self.names[fid])

    def move(self, account, fid, target_fid):
        for rows in self.rows.values():
            for entry in tuple(rows):
                if entry.fid == fid:
                    rows.remove(entry)
                    self.rows[target_fid].append(replace(entry, parent_fid='opaque-destination'))
                    self.mutations.append(('move', fid))
                    return MutationResult(True)
        return MutationResult(False)

    def rename(self, account, fid, old_name, new_name):
        for rows in self.rows.values():
            for index, entry in enumerate(rows):
                if entry.fid == fid and entry.name == old_name:
                    rows[index] = replace(entry, name=new_name)
                    if fid in self.names:
                        self.names[fid] = new_name
                    return MutationResult(True)
        return MutationResult(False)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.provider = FakeProvider()
        self.store = Store(self.root / 'state.db')
        self.store.bind(ScopeBinding('a', 'synthetic-uuid', 'marker', 'scope-marker.json', hashlib.sha256(self.provider.content['marker']).hexdigest(), 'marker-parent', len(self.provider.content['marker'])))
        self.catalog = Catalog(self.provider, self.store, self.root / 'work')
        self.ops = Operations(self.catalog, self.store)
        self.transfers = Transfers(self.catalog, self.store, self.root / 'work')
        self.remote_root = FileRef('a', fid='0')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def file(self, name, data=b'synthetic'):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_snapshot_more_than_preview_account_scope_and_marker_guard(self):
        self.provider.rows['0'] = [RemoteEntry('fid|' + str(i), 'opaque', str(i), 'file', None) for i in range(105)]
        page = self.catalog.list(self.remote_root, page_size=100)
        self.provider.rows['0'] = []
        second = self.catalog.list(self.remote_root, page_size=100, page_token=page.next_page_token)
        self.assertEqual(len(second.entries), 5)
        self.assertIsNone(second.entries[0].size)
        with self.assertRaises(VfsError):
            self.store.snapshot('other', page.snapshot_id)
        self.provider.content['marker'] = b'other account'
        with self.assertRaises(VfsError) as error:
            self.catalog.list(self.remote_root, page_token=page.next_page_token)
        self.assertEqual(error.exception.code, 'SCOPE_MISMATCH')

    def test_exact_path_ambiguity_and_limited_stat(self):
        directory = self.ops.mkdir(self.remote_root, '资料')
        ref = FileRef('a', fid=directory.fid, parent_fid_hint='0')
        receipt = self.transfers.upload_file(self.file('说明.md'), ref)
        entry = self.catalog.resolve(FileRef('a', path='/资料/说明.md'))
        self.assertEqual(entry.fid, receipt.entry.fid)
        self.provider.rows[directory.fid].append(replace(entry, fid='other|opaque'))
        with self.assertRaises(VfsError) as error:
            self.catalog.resolve(FileRef('a', path='/资料/说明.md'))
        self.assertEqual(error.exception.code, 'AMBIGUOUS_PATH')
        with self.assertRaises(VfsError) as error:
            self.catalog.stat(FileRef('a', fid='not-located'))
        self.assertEqual(error.exception.code, 'LOCATION_UNKNOWN')

    def test_fresh_browse_prefix_changes_return_fresh_full_identity(self):
        old = RemoteEntry('old-prefix|same ', 'opaque-parent', 'sample', 'file', None)
        self.provider.rows['0'] = [old]
        page = self.catalog.list(self.remote_root, page_size=1)
        self.provider.rows['0'] = [replace(old, fid='new-prefix|same ')]
        fresh = self.catalog.resolve(FileRef('a', fid=old.fid))
        self.assertEqual(fresh.fid, 'new-prefix|same ')
        self.assertEqual(self.store.cached_entry('a', old.fid).fid, fresh.fid)
        self.assertIsNone(page.entries[0].size)

    def test_large_transfer_rejected_before_dispatch(self):
        source = self.root / 'large'
        with source.open('wb') as stream:
            stream.truncate(MAX_TRANSFER_BYTES + 1)
        with self.assertRaises(VfsError) as error:
            self.transfers.upload_file(source, self.remote_root)
        self.assertEqual(error.exception.code, 'TRANSFER_SIZE_LIMIT')
        self.assertEqual(self.provider.upload_calls, [])

    def test_transfer_size_boundary_uses_explicit_128_mib_read_limit(self):
        # Sparse files and a sentinel at the secure reader avoid allocating 128 MiB.
        source = self.root / 'boundary.bin'
        for size in (64 * 1024 * 1024 + 1, MAX_TRANSFER_BYTES):
            with self.subTest(size=size), source.open('wb') as stream:
                stream.truncate(size)
            for operation in (lambda: self.transfers.upload_file(source, self.remote_root),
                              lambda: self.transfers.upload([source], self.remote_root)):
                with patch('quark_vfs.core.transfers.secure_bytes', side_effect=RuntimeError('reader reached')) as reader:
                    with self.assertRaisesRegex(RuntimeError, 'reader reached'):
                        operation()
                    self.assertEqual(reader.call_args.kwargs['max_bytes'], MAX_TRANSFER_BYTES)
            entry = RemoteEntry('boundary', '0', source.name, 'file', size)
            staging = self.root / 'boundary-stage'
            staging.mkdir(exist_ok=True)
            with patch.object(self.provider, 'download_file', return_value=DownloadResult(staging / source.name, source.name)) as download:
                with patch('quark_vfs.core.transfers.secure_bytes', side_effect=RuntimeError('reader reached')) as reader:
                    with self.assertRaisesRegex(RuntimeError, 'reader reached'):
                        self.transfers._download_file(FileRef('a', fid=entry.fid, parent_fid_hint='0'), entry, staging)
                    download.assert_called_once()
                    self.assertEqual(reader.call_args.kwargs['max_bytes'], MAX_TRANSFER_BYTES)
        entry = replace(entry, size=MAX_TRANSFER_BYTES + 1)
        with patch.object(self.provider, 'download_file') as download:
            with self.assertRaises(VfsError) as error:
                self.transfers._download_file(FileRef('a', fid=entry.fid, parent_fid_hint='0'), entry, staging)
            self.assertEqual(error.exception.code, 'TRANSFER_SIZE_LIMIT')
            download.assert_not_called()
        with source.open('wb') as stream:
            stream.truncate(MAX_TRANSFER_BYTES + 1)
        with self.assertRaises(VfsError) as error:
            self.transfers.upload([source], self.remote_root)
        self.assertEqual(error.exception.code, 'TRANSFER_SIZE_LIMIT')
        self.assertEqual(self.provider.upload_calls, [])

    def test_upload_id_domain_verified_hash_and_no_replace(self):
        source = self.file('sample.md')
        uploaded = self.transfers.upload_file(source, self.remote_root)
        self.assertIn('trusted_hash_verified', uploaded.verification_levels)
        ref = FileRef('a', fid=uploaded.entry.fid, parent_fid_hint='0')
        target = self.root / 'download.md'
        plain = self.transfers.download(ref, target)
        self.assertIn('local_fingerprint_recorded', plain.verification_levels)
        self.assertNotIn('trusted_hash_verified', plain.verification_levels)
        with self.assertRaises(VfsError) as error:
            self.transfers.download(ref, target)
        self.assertEqual(error.exception.code, 'NAME_CONFLICT')
        receipt = self.transfers.download(ref, target, conflict='keep_both', trusted_sha256=uploaded.sha256)
        self.assertIn('trusted_hash_verified', receipt.verification_levels)
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_manifest_exclusions_symlinks_and_directory_download(self):
        local = self.root / 'tree'
        local.mkdir()
        (local / '.env').write_text('synthetic_secret')
        (local / 'a.md').write_text('synthetic')
        result = self.transfers.upload([local], self.remote_root)
        self.assertEqual(result.state, 'partial')
        self.assertNotIn('.env', self.provider.upload_calls)
        self.assertIn('tree/.env', result.items[-1]['result']['excluded'])
        directory = self.catalog.resolve(FileRef('a', path='/tree'))
        target = self.root / 'published'
        self.transfers.download(FileRef('a', fid=directory.fid, parent_fid_hint='0'), target)
        self.assertEqual((target / 'a.md').read_text(), 'synthetic')
        (local / 'link').symlink_to(local / 'a.md')
        with self.assertRaises(VfsError):
            self.transfers.upload([local], self.remote_root)

    def test_partial_success_persists_and_unknown_never_replays(self):
        self.provider.fail_upload = 'second.md'
        result = self.transfers.upload([self.file('first.md'), self.file('second.md'), self.file('third.md')], self.remote_root)
        self.assertEqual(result.state, 'needs_reconciliation')
        self.assertEqual([i['state'] for i in result.items], ['succeeded', 'needs_reconciliation', 'pending'])
        before = list(self.provider.upload_calls)
        with self.assertRaises(VfsError) as error:
            Jobs(self.store).resume('a', result.job_id)
        self.assertEqual(error.exception.code, 'RECONCILIATION_REQUIRED')
        self.assertEqual(self.provider.upload_calls, before)

    def test_preflight_error_and_postdispatch_same_code_have_different_states(self):
        def fail(_):
            raise VfsError('LOCATION_UNKNOWN', 'synthetic')
        jobs = Jobs(self.store)
        before = jobs.run('a', 'move', [{'fid': 'x'}], fail, preflight=fail)
        after = jobs.run('a', 'move', [{'fid': 'x'}], fail)
        self.assertEqual(before.state, 'failed')
        self.assertEqual(after.state, 'needs_reconciliation')
        done = jobs.resume('a', before.job_id, execute=lambda _: {'ok': True}, preflight=lambda _: None)
        self.assertEqual(done['state'], 'succeeded')

    def test_move_cycle_expectation_and_collision(self):
        parent = self.ops.mkdir(self.remote_root, 'parent')
        child = self.ops.mkdir(FileRef('a', fid=parent.fid, parent_fid_hint='0'), 'child')
        result = self.ops.move([FileRef('a', fid=parent.fid, parent_fid_hint='0')], FileRef('a', fid=child.fid, parent_fid_hint=parent.fid))
        self.assertNotEqual(result.state, 'succeeded')
        self.assertFalse(any(m[0] == 'move' for m in self.provider.mutations))
        with self.assertRaises(VfsError):
            self.ops.rename(FileRef('a', fid=parent.fid, parent_fid_hint='0'), 'new', expected=Expected('changed', '0', 'directory'))

    def test_file_publication_race_and_parent_link_never_overwrite(self):
        source = self.file('stage.txt', b'new')
        target = self.file('target.txt', b'old')
        with self.assertRaises(VfsError):
            publish_no_replace(source, target)
        self.assertEqual(target.read_bytes(), b'old')
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(VfsError):
            publish_no_replace(source, alias / 'new.txt')

    def test_account_lock_and_binding_are_not_silently_changed(self):
        second = Store(self.root / 'state.db')
        try:
            with self.store.account_lock('a'):
                with self.store.account_lock('a'):
                    with self.assertRaises(VfsError):
                        with second.account_lock('a'):
                            pass
                    with self.assertRaises(VfsError):
                        with second.account_lock('other-alias'):
                            pass
            with self.assertRaises(VfsError):
                self.store.bind(self.store.binding('a'))
        finally:
            second.close()

    def test_download_resume_uses_new_staging_without_replaying_success(self):
        uploaded = self.transfers.upload_file(self.file('resume.md'), self.remote_root)
        ref = FileRef('a', fid=uploaded.entry.fid, parent_fid_hint='0')
        target = self.root / 'resume-target.md'
        self.provider.fail_download = uploaded.entry.fid
        with self.assertRaises(VfsError):
            self.transfers.download(ref, target)
        job = self.store.jobs('a')[0]
        first_stage = [p for fid, p in self.provider.download_calls if fid == uploaded.entry.fid][-1]
        self.provider.fail_download = None
        result = self.transfers.resume_download('a', job['job_id'])
        self.assertEqual(result['state'], 'succeeded')
        second_stage = [p for fid, p in self.provider.download_calls if fid == uploaded.entry.fid][-1]
        self.assertNotEqual(first_stage, second_stage)
        calls = len([fid for fid, _ in self.provider.download_calls if fid == uploaded.entry.fid])
        self.transfers.resume_download('a', job['job_id'])
        self.assertEqual(len([fid for fid, _ in self.provider.download_calls if fid == uploaded.entry.fid]), calls)

    def test_private_roots_and_management_recursive_exclusions_are_visible(self):
        local = self.root / 'public'
        private = local / 'private'
        private.mkdir(parents=True)
        (private / 'ordinary-name.json').write_text('synthetic-private')
        (local / 'readme.md').write_text('public synthetic')
        transfers = Transfers(self.catalog, self.store, self.root / 'work', excluded_local_roots=(private,))
        result = transfers.upload([local], self.remote_root, exclude_secrets=False)
        self.assertEqual(result.state, 'partial')
        self.assertEqual(self.provider.upload_calls, ['readme.md'])
        self.assertTrue(any(item['state'] == 'skipped' for item in result.items))
        management = self.ops.mkdir(self.remote_root, 'managed')
        self.store.put_document('a', 'management', 'root', {'fid': management.fid})
        receipt = transfers.download(self.remote_root, self.root / 'root-copy')
        self.assertIn('managed', receipt.excluded)
        self.assertFalse((self.root / 'root-copy' / 'managed').exists())

    def test_huge_scope_marker_is_rejected_before_download(self):
        marker = self.provider.rows['marker-parent'][0]
        self.provider.rows['marker-parent'] = [replace(marker, size=65537)]
        with self.assertRaises(VfsError) as error:
            self.catalog.stat(self.remote_root)
        self.assertEqual(error.exception.code, 'SCOPE_MISMATCH')
        self.assertEqual(self.provider.download_calls, [])

    def test_changed_source_keeps_native_record_and_indexed_immutable_copy(self):
        source = self.file('changing.md')
        self.provider.mutate_source = source
        with self.assertRaises(VfsError) as error:
            self.transfers.upload_file(source, self.remote_root)
        self.assertEqual(error.exception.code, 'SOURCE_CHANGED')
        self.assertIsNotNone(error.exception.job_id)
        job = self.store.job('a', error.exception.job_id)
        evidence = job['items'][0]['result']['evidence']
        self.assertTrue(evidence['native_upload_id'].startswith('native-'))
        self.assertTrue(Path(evidence['immutable_source']).exists())
        self.assertEqual(job['state'], 'needs_reconciliation')

    def test_short_download_with_old_tail_never_publishes(self):
        uploaded = self.transfers.upload_file(self.file('short.md', b'new'), self.remote_root)
        self.provider.corrupt_download = uploaded.entry.fid
        target = self.root / 'short-result.md'
        with self.assertRaises(VfsError) as error:
            self.transfers.download(FileRef('a', fid=uploaded.entry.fid, parent_fid_hint='0'), target)
        self.assertEqual(error.exception.code, 'CONTENT_MISMATCH')
        self.assertFalse(target.exists())
        self.assertIsNotNone(error.exception.job_id)

    def test_source_replaced_with_symlink_during_publication_is_not_published(self):
        source = self.file('to-publish', b'new')
        outside = self.file('outside', b'private synthetic')
        target = self.root / 'published-file'
        import os
        original = os.link
        def replace_then_link(*args, **kwargs):
            source.unlink()
            source.symlink_to(outside)
            return original(*args, **kwargs)
        with patch('quark_vfs.core.transfers.os.link', side_effect=replace_then_link):
            with self.assertRaises(VfsError) as error:
                publish_no_replace(source, target)
        self.assertEqual(error.exception.code, 'PUBLICATION_OUTCOME_UNKNOWN')
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_bytes(), b'private synthetic')

    def test_concurrent_target_replacement_is_never_removed_on_mismatch(self):
        import os
        source = self.file('stage-original', b'verified staging')
        target = self.root / 'concurrent-target'
        original_link = os.link
        def link_then_external_replacement(*args, **kwargs):
            original_link(*args, **kwargs)
            target.unlink()
            target.write_bytes(b'external-new-file')
        with patch('quark_vfs.core.transfers.os.link', side_effect=link_then_external_replacement):
            with self.assertRaises(VfsError) as error:
                publish_no_replace(source, target)
        self.assertEqual(error.exception.code, 'PUBLICATION_OUTCOME_UNKNOWN')
        self.assertEqual(target.read_bytes(), b'external-new-file')
        self.assertEqual(source.read_bytes(), b'verified staging')

    def test_directory_digest_is_rejected_instead_of_silently_ignored(self):
        directory = self.ops.mkdir(self.remote_root, 'directory-hash')
        target = self.root / 'directory-result'
        with self.assertRaises(VfsError) as error:
            self.transfers.download(FileRef('a', fid=directory.fid, parent_fid_hint='0'), target,
                                    trusted_sha256='0' * 64)
        self.assertEqual(error.exception.code, 'TRUSTED_HASH_UNAVAILABLE')
        self.assertFalse(target.exists())

    def test_acknowledged_upload_polls_only_reads_when_visibility_is_delayed(self):
        self.provider.visibility_delay = 2
        with patch('quark_vfs.core.catalog.time.sleep'):
            receipt = self.transfers.upload_file(self.file('lag.md'), self.remote_root)
        self.assertIsNotNone(receipt.entry)
        self.assertEqual(self.provider.upload_calls, ['lag.md'])

    def test_visibility_timeout_retains_known_write_and_never_repeats_it(self):
        self.provider.visibility_delay = 10
        with patch('quark_vfs.core.catalog.time.sleep'):
            with self.assertRaises(VfsError) as error:
                self.transfers.upload_file(self.file('invisible.md'), self.remote_root)
        self.assertEqual(error.exception.code, 'OUTCOME_UNKNOWN')
        with self.assertRaises(VfsError) as rejected:
            self.transfers.resume_upload('a', error.exception.job_id)
        self.assertEqual(rejected.exception.code, 'RECONCILIATION_REQUIRED')
        self.assertEqual(self.provider.upload_calls, ['invisible.md'])

    def test_upload_resume_rechecks_only_preflight_failed_item(self):
        first = self.file('one.md')
        second = self.file('two.md')
        self.provider.mutate_source = second
        result = self.transfers.upload([first, second], self.remote_root)
        self.assertEqual(result.state, 'partial')
        self.assertEqual([i['state'] for i in result.items], ['succeeded', 'failed'])
        second.write_bytes(b'synthetic')
        self.provider.mutate_source = None
        resumed = self.transfers.resume_upload('a', result.job_id)
        self.assertEqual(resumed['state'], 'succeeded')
        self.assertEqual(self.provider.upload_calls, ['one.md', 'two.md'])

    def test_explicit_secret_is_not_silently_successful(self):
        source = self.file('auth.json', b'synthetic secret')
        with self.assertRaises(VfsError) as error:
            self.transfers.upload([source], self.remote_root)
        self.assertEqual(error.exception.code, 'EXCLUDED_SOURCE')
        self.assertEqual(self.provider.upload_calls, [])

    def test_control_objects_cannot_be_moved_or_renamed(self):
        marker = FileRef('a', fid='marker', parent_fid_hint='marker-parent')
        with self.assertRaises(VfsError) as error:
            self.ops.rename(marker, 'renamed-marker')
        self.assertEqual(error.exception.code, 'PROTECTED_SYSTEM_ENTRY')
        management = self.ops.mkdir(self.remote_root, 'managed')
        self.store.put_document('a', 'management', 'root', {'fid': management.fid})
        moved = self.ops.move([FileRef('a', fid=management.fid, parent_fid_hint='0')], self.remote_root)
        self.assertEqual(moved.state, 'failed')
        self.assertEqual(moved.items[0]['result']['error_code'], 'PROTECTED_SYSTEM_ENTRY')
        self.assertFalse(any(action == 'move' for action, _ in self.provider.mutations))

    def test_pending_move_keeps_original_identity_and_expected_metadata(self):
        for replace_identity in (False, True):
            with self.subTest(replace_identity=replace_identity):
                suffix = '-replacement' if replace_identity else '-metadata'
                first = self.transfers.upload_file(self.file('first' + suffix), self.remote_root).entry
                second = self.transfers.upload_file(self.file('second' + suffix), self.remote_root).entry
                destination = self.ops.mkdir(self.remote_root, 'destination' + suffix)
                original_move = self.provider.move
                def mutate_pending(account, fid, target, *, original_move=original_move,
                                   first=first, second=second, replace_identity=replace_identity):
                    response = original_move(account, fid, target)
                    if fid == first.fid:
                        rows = self.provider.rows['0']
                        index = next(i for i, e in enumerate(rows) if e.fid == second.fid)
                        rows[index] = replace(rows[index], fid='replacement|new-object' if replace_identity else second.fid,
                                              modified='new metadata')
                    return response
                with patch.object(self.provider, 'move', side_effect=mutate_pending):
                    result = self.ops.move([FileRef('a', path='/' + first.name), FileRef('a', path='/' + second.name)],
                                           FileRef('a', path='/' + destination.name))
                self.assertEqual(result.state, 'partial')
                self.assertEqual(result.items[1]['state'], 'failed')
                self.assertIsNone(result.items[1]['input']['source']['path'])
                self.assertEqual(result.items[1]['input']['source']['fid'], second.fid)
                self.assertIsNotNone(result.items[1]['input']['expected'])
                count = len(self.provider.mutations)
                with self.assertRaises(VfsError):
                    self.ops.resume('a', result.job_id)
                self.assertEqual(len(self.provider.mutations), count)


if __name__ == '__main__':
    unittest.main()

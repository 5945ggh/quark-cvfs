import copy
import unittest

from quark_vfs.core.trash import TrashError, TrashService, validate_manifest


class Journal:
    def __init__(self):
        self.data = {}

    def put(self, account, operation, value):
        self.data[account, operation] = copy.deepcopy(value)

    def get(self, account, operation):
        return copy.deepcopy(self.data.get((account, operation)))


class Backend:
    def __init__(self):
        self.rows = {'root': [], 'original': [{'fid': 'source', 'name': 'a.md', 'is_dir': False, 'size': 3}]}
        self.content = {'source': b'abc'}
        self.events = []
        self.broken_manifest = False
        self.unknown_move = False
        self.count = 0

    def list_entries(self, parent):
        if parent not in self.rows:
            raise TrashError('LOCATION_UNKNOWN', 'Parent absent')
        return tuple(copy.deepcopy(self.rows[parent]))

    def mkdir(self, parent, name):
        self.count += 1
        fid = 'directory-' + str(self.count)
        self.rows[parent].append({'fid': fid, 'name': name, 'is_dir': True, 'size': None})
        self.rows[fid] = []
        return fid

    def upload_verified_bytes(self, parent, name, content):
        self.count += 1
        fid = 'file-' + str(self.count)
        self.rows[parent].append({'fid': fid, 'name': name, 'is_dir': False, 'size': len(content)})
        self.content[fid] = content
        self.events.append('upload-' + name)
        return fid

    def read_bytes(self, fid, limit):
        if self.broken_manifest:
            return b'broken'
        return self.content[fid][:limit + 1]

    def move(self, fid, source, target, name, conflict, expected=None):
        self.events.append('move')
        if self.unknown_move:
            raise TrashError('OUTCOME_UNKNOWN', 'Response lost')
        if any(row['name'] == name for row in self.rows[target]):
            raise TrashError('CONFLICT', 'Target exists')
        row = next(row for row in self.rows[source] if row['fid'] == fid)
        self.rows[source].remove(row)
        self.rows[target].append(row)
        return {'state': 'completed'}


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.backend, self.journal = Backend(), Journal()
        self.service = TrashService(self.backend, self.journal, account='scope', trash_root_fid='root', scope_uuid='43f584b0-a634-441a-8208-2737bfd7cf36')

    def trash(self):
        return self.service.trash([{'fid': 'source', 'parent_fid': 'original', 'name': 'a.md', 'is_dir': False, 'size': 3}])

    def test_manifest_precedes_move_and_restore_preserves_history(self):
        state = self.trash()
        self.assertEqual(self.backend.events[0], 'upload-manifest.json')
        self.assertEqual(self.backend.events[1], 'move')
        self.assertEqual(state['status'], 'completed')
        self.service.restore(state['operation_id'], state['manifest']['entries'][0]['entry_id'])
        restored = self.journal.get('scope', state['operation_id'])
        self.assertEqual(restored['status'], 'restored')
        self.assertIn('manifest', restored)
        self.assertTrue(any(row['fid'] == 'source' for row in self.backend.rows['original']))

    def test_manifest_mismatch_never_moves_source(self):
        self.backend.broken_manifest = True
        with self.assertRaises(TrashError):
            self.trash()
        self.assertNotIn('move', self.backend.events)
        self.assertEqual(self.backend.rows['original'][0]['fid'], 'source')

    def test_uncertain_move_is_journaled_and_not_retried(self):
        self.backend.unknown_move = True
        with self.assertRaises(TrashError):
            self.trash()
        self.assertEqual(self.backend.events.count('move'), 1)
        self.assertEqual(next(iter(self.journal.data.values()))['status'], 'outcome_unknown')

    def test_rebuild_without_local_database_checks_payload(self):
        state = self.trash()
        self.journal.data.clear()
        rebuilt = self.service.rebuild()
        self.assertEqual(rebuilt[0]['operation_id'], state['operation_id'])
        self.assertEqual(rebuilt[0]['entries'][0]['payload_fid'], 'source')

    def test_manifest_rejects_cross_scope_and_injected_actions(self):
        manifest = self.trash()['manifest']
        with self.assertRaises(TrashError):
            validate_manifest(manifest, 'another')
        manifest['action'] = 'execute'
        with self.assertRaises(TrashError):
            validate_manifest(manifest, 'scope')

    def test_restore_conflict_preserves_payload(self):
        state = self.trash()
        self.backend.rows['original'].append({'fid': 'other', 'name': 'a.md', 'is_dir': False, 'size': 3})
        with self.assertRaises(TrashError):
            self.service.restore(state['operation_id'], state['manifest']['entries'][0]['entry_id'])
        container = state['manifest']['entries'][0]['container_fid']
        self.assertEqual(self.backend.rows[container][0]['fid'], 'source')

    def test_restore_preflight_failure_does_not_lock_later_destination(self):
        from unittest.mock import Mock
        state = self.trash()
        row = state['manifest']['entries'][0]
        before = list(self.backend.events)
        self.backend.preflight_move = Mock(side_effect=TrashError('LOCATION_UNKNOWN','Unknown target ancestry'))
        with self.assertRaises(TrashError):
            self.service.restore(state['operation_id'],row['entry_id'])
        self.assertEqual(before,self.backend.events)
        self.assertNotIn(row['entry_id'],self.journal.get('scope',state['operation_id']).get('restore_attempts',{}))
        self.backend.rows['explicit'] = []
        self.backend.preflight_move = Mock()
        self.service.restore(state['operation_id'],row['entry_id'],destination='explicit',conflict='keep_both')
        self.assertEqual(self.backend.rows['explicit'][0]['fid'],'source')

    def test_posttrash_modified_is_frozen_and_external_change_is_rejected(self):
        original_move=self.backend.move
        def moved(*args,**kwargs):
            result=original_move(*args,**kwargs)
            for row in self.backend.rows[args[2]]:
                if row['fid']==args[0]:
                    row['modified']='post-trash-time'
            return result
        self.backend.move=moved
        state=self.trash()
        row=state['manifest']['entries'][0]
        self.assertIsNone(row['modified'])
        self.assertEqual(state['entries'][0]['payload_expected']['modified'],'post-trash-time')
        self.backend.rows[row['container_fid']][0]['modified']='external-change'
        with self.assertRaises(TrashError):
            self.service.restore(state['operation_id'],row['entry_id'])
        self.backend.rows[row['container_fid']][0]['modified']='post-trash-time'
        self.journal.data.clear()
        rebuilt=self.service.rebuild()[0]
        self.assertEqual(rebuilt['entries'][0]['payload_expected']['modified'],'post-trash-time')
        self.service.restore(state['operation_id'],row['entry_id'])

    def test_legacy_completion_without_payload_observation_needs_reconciliation(self):
        import json
        self.trash()
        completed=next(fid for fid,raw in self.backend.content.items() if b'trash_completion' in raw)
        event=json.loads(self.backend.content[completed])
        event.pop('kind')
        self.backend.content[completed]=json.dumps(event).encode()
        self.journal.data.clear()
        rebuilt=self.service.rebuild()[0]
        self.assertEqual(rebuilt['entries'][0]['status'],'needs_reconciliation')

    def test_directory_trusted_digest_has_no_supported_schema(self):
        manifest = self.trash()['manifest']
        manifest['entries'][0]['is_dir'] = True
        manifest['entries'][0]['trusted_sha256'] = 'a'*64
        with self.assertRaises(TrashError):
            validate_manifest(manifest,'scope')

    def test_rebuild_marks_missing_payload_without_discarding_record(self):
        state = self.trash()
        row = state['manifest']['entries'][0]
        self.backend.rows[row['container_fid']].clear()
        self.journal.data.clear()
        rebuilt = self.service.rebuild()
        self.assertEqual(rebuilt[0]['entries'][0]['status'], 'needs_reconciliation')
        self.assertIn(('scope', state['operation_id']), self.journal.data)

    def test_restore_writes_a_distinct_immutable_remote_event(self):
        state = self.trash()
        self.service.restore(state['operation_id'], state['manifest']['entries'][0]['entry_id'])
        self.assertTrue(any(event.startswith('upload-restored-') for event in self.backend.events))
        self.assertEqual(self.backend.events.count('upload-manifest.json'), 1)

    def test_scope_uuid_mismatch_and_duplicate_sources_fail_before_cloud_write(self):
        source = {'fid': 'source', 'parent_fid': 'original', 'name': 'a.md', 'is_dir': False, 'size': 3}
        with self.assertRaises(TrashError):
            self.service.trash([source.copy(), source.copy()])
        self.assertEqual(self.backend.events, [])
        manifest = self.trash()['manifest']
        with self.assertRaises(TrashError):
            validate_manifest(manifest, 'scope', '34509ad7-6bea-4268-a878-b23c81db4d88')

    def test_shared_core_adapter_roundtrip_and_database_loss(self):
        import hashlib
        import tempfile
        from dataclasses import asdict
        from pathlib import Path

        from test_core import FakeProvider

        from quark_vfs.core.catalog import Catalog
        from quark_vfs.core.operations import Operations
        from quark_vfs.core.transfers import Transfers
        from quark_vfs.core.trash import CoreTrashBackend, StoreTrashJournal
        from quark_vfs.models import FileRef, ScopeBinding
        from quark_vfs.storage import Store

        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory).resolve()
            provider = FakeProvider()
            binding = ScopeBinding('a', '43f584b0-a634-441a-8208-2737bfd7cf36', 'marker',
                                   'scope-marker.json', hashlib.sha256(provider.content['marker']).hexdigest(),
                                   'marker-parent', len(provider.content['marker']))
            store = Store(local / 'original.sqlite3')
            restored_store = None
            try:
                store.bind(binding)
                catalog = Catalog(provider, store, local / 'catalog')
                operations = Operations(catalog, store)
                transfers = Transfers(catalog, store, local / 'transfers')
                root = FileRef('a', fid='0')
                trash_root = operations.mkdir(root, 'managed-trash')
                source = local / 'source.md'
                source.write_bytes(b'whole payload')
                receipt = transfers.upload_file(source, root)
                entry = receipt.entry
                row = asdict(entry)
                row['is_dir'] = False
                row['trusted_sha256'] = receipt.sha256
                backend = CoreTrashBackend(catalog, operations, transfers, 'a', local / 'trash')
                service = TrashService(backend, StoreTrashJournal(store), account='a',
                                       trash_root_fid=trash_root.fid, scope_uuid=binding.scope_uuid)
                state = service.trash([row])
                self.assertEqual(state['status'], 'completed')
                store.close()
                store = None
                restored_store = Store(local / 'rebuilt.sqlite3')
                restored_store.bind(binding)
                rebuilt_catalog = Catalog(provider, restored_store, local / 'rebuilt-catalog')
                rebuilt_ops = Operations(rebuilt_catalog, restored_store)
                rebuilt_transfers = Transfers(rebuilt_catalog, restored_store, local / 'rebuilt-transfers')
                rebuilt_service = TrashService(
                    CoreTrashBackend(rebuilt_catalog, rebuilt_ops, rebuilt_transfers, 'a', local / 'rebuilt-trash'),
                    StoreTrashJournal(restored_store), account='a', trash_root_fid=trash_root.fid,
                    scope_uuid=binding.scope_uuid)
                rebuilt = rebuilt_service.rebuild()
                self.assertEqual(rebuilt[0]['entries'][0]['status'], 'trashed')
                result = rebuilt_service.restore(state['operation_id'], state['manifest']['entries'][0]['entry_id'])
                self.assertEqual(result['state'], 'succeeded')
                self.assertEqual(provider.content[entry.fid], b'whole payload')
                self.assertTrue(any(item.name == 'source.md' for item in provider.rows['0']))
            finally:
                if store is not None:
                    store.close()
                if restored_store is not None:
                    restored_store.close()

    def test_rebuild_retains_verified_restoration_and_remaining_payload(self):
        self.backend.rows['original'].append({'fid': 'second', 'name': 'b.md', 'is_dir': False, 'size': 3})
        self.backend.content['second'] = b'def'
        sources = [{'fid': 'source', 'parent_fid': 'original', 'name': 'a.md', 'is_dir': False, 'size': 3},
                   {'fid': 'second', 'parent_fid': 'original', 'name': 'b.md', 'is_dir': False, 'size': 3}]
        state = self.service.trash(sources)
        self.service.restore(state['operation_id'], state['manifest']['entries'][0]['entry_id'])
        self.journal.data.clear()
        rebuilt = self.service.rebuild()[0]
        self.assertEqual([entry['status'] for entry in rebuilt['entries']], ['restored', 'trashed'])
        self.assertEqual(len(rebuilt['restorations']), 1)

    def test_already_restored_entry_never_moves_a_replacement_payload(self):
        state = self.trash()
        row = state['manifest']['entries'][0]
        self.service.restore(state['operation_id'], row['entry_id'])
        self.backend.rows[row['container_fid']].append({'fid': 'replacement', 'name': 'a.md', 'is_dir': False, 'size': 3})
        before = self.backend.events.count('move')
        with self.assertRaises(TrashError) as error:
            self.service.restore(state['operation_id'], row['entry_id'])
        self.assertEqual(error.exception.code, 'ALREADY_RESTORED')
        self.assertEqual(self.backend.events.count('move'), before)

    def test_unknown_restore_is_not_dispatched_again(self):
        state = self.trash()
        row = state['manifest']['entries'][0]
        self.backend.unknown_move = True
        with self.assertRaises(TrashError):
            self.service.restore(state['operation_id'], row['entry_id'])
        before = self.backend.events.count('move')
        with self.assertRaises(TrashError) as error:
            self.service.restore(state['operation_id'], row['entry_id'])
        self.assertEqual(error.exception.code, 'RECONCILIATION_REQUIRED')
        self.assertEqual(self.backend.events.count('move'), before)

    def test_rebuild_never_revives_a_restored_entry_with_refilled_container(self):
        state = self.trash()
        row = state['manifest']['entries'][0]
        self.service.restore(state['operation_id'], row['entry_id'])
        self.backend.rows[row['container_fid']].append({'fid': 'replacement', 'name': 'a.md', 'is_dir': False, 'size': 3})
        self.journal.data.clear()
        rebuilt = self.service.rebuild()[0]
        self.assertEqual(rebuilt['entries'][0]['status'], 'needs_reconciliation')
        before = self.backend.events.count('move')
        with self.assertRaises(TrashError):
            self.service.restore(state['operation_id'], row['entry_id'])
        self.assertEqual(self.backend.events.count('move'), before)

    def test_rebuild_preserves_unknown_restore_with_or_without_local_database(self):
        state = self.trash()
        row = state['manifest']['entries'][0]
        self.backend.unknown_move = True
        with self.assertRaises(TrashError):
            self.service.restore(state['operation_id'], row['entry_id'])
        for lose_database in (False, True):
            if lose_database:
                self.journal.data.clear()
            rebuilt = self.service.rebuild()[0]
            self.assertEqual(rebuilt['entries'][0]['status'], 'needs_reconciliation')
            before = self.backend.events.count('move')
            with self.assertRaises(TrashError) as error:
                self.service.restore(state['operation_id'], row['entry_id'])
            self.assertEqual(error.exception.code, 'RECONCILIATION_REQUIRED')
            self.assertEqual(self.backend.events.count('move'), before)

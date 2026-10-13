"""Exercise the acceptance workflow through CLI main and the actual shared core."""
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from test_core import FakeProvider

from quark_vfs.config import load_config
from quark_vfs.models import RemoteEntry, ScopeBinding
from quark_vfs.storage import Store
from tools.m1_acceptance import M1Acceptance


class ScopedFakeProvider(FakeProvider):
    def list_directory(self, account, parent_fid):
        return tuple(replace(row, parent_fid=parent_fid, raw_parent_fid=row.parent_fid)
                     for row in super().list_directory(account, parent_fid))


class M1AcceptanceTests(unittest.TestCase):
    def configured(self, root):
        source = Path('examples/config.toml').read_text()
        replacements = {'/absolute/path/to/node': str(root / 'node'),
                        '/absolute/path/to/quarkclouddrive/scripts/quark-drive.cjs': str(root / 'official/scripts/quark-drive.cjs'),
                        '/absolute/private/path/quark-vfs/state': str(root / 'state'),
                        '/absolute/private/path/quark-vfs/cache': str(root / 'cache')}
        for old, new in replacements.items():
            source = source.replace(old, new)
        source = source.replace('name = "default"', 'name = "a"').replace('file_operations_enabled = false', 'file_operations_enabled = true').replace('trash_enabled = false', 'trash_enabled = true')
        path = root / 'config.toml'
        path.write_text(source)
        return path

    def provider_and_binding(self, config):
        provider = ScopedFakeProvider()
        identifier = '43f584b0-a634-441a-8208-2737bfd7cf36'
        name = 'QuarkVFS-' + identifier
        provider.mkdir('a', '0', name)
        management = provider.rows['0'][0]
        marker_name = 'scope-' + identifier + '.json'
        marker = json.dumps({'schema_version': 1, 'kind': 'scope_marker', 'account': 'a',
                             'scope_uuid': identifier}, sort_keys=True).encode()
        provider.content['marker'] = marker
        provider.names['marker'] = marker_name
        provider.rows[management.fid].append(RemoteEntry('marker', management.fid, marker_name, 'file', len(marker)))
        store = Store(config.state_dir / 'quark-vfs.sqlite3')
        try:
            store.bind(ScopeBinding('a', identifier, 'marker', marker_name, hashlib.sha256(marker).hexdigest(), management.fid, len(marker)))
            now = datetime.now(UTC).isoformat()
            store.save_snapshot('a', '0', tuple(provider.list_directory('a', '0')), now, now)
            store.put_document('a', 'management', 'root', {'fid': management.fid, 'name': name, 'parent_fid': '0'})
        finally:
            store.close()
        return provider

    def test_real_cli_core_basic_and_database_loss_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.configured(root)
            config = load_config(path)
            provider = self.provider_and_binding(config)
            with patch('quark_vfs.provider.quark.QuarkProvider', return_value=provider):
                harness = M1Acceptance(path)
                result = harness.run('all')
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['automatic_retries'], 0)
            self.assertEqual(result['checks']['database_loss_rebuild_restored_history_and_directory'], 'passed')
            self.assertFalse(any('delete' in mutation[0] for mutation in provider.mutations))
            self.assertFalse(any('fid' in key for key in result))

    def test_unknown_write_stops_without_reupload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.configured(root)
            provider = self.provider_and_binding(load_config(path))
            with patch('quark_vfs.provider.quark.QuarkProvider', return_value=provider):
                harness = M1Acceptance(path)
                provider.fail_upload = 'source-' + harness.id + '.md'
                result = harness.run('basic')
            self.assertFalse(result['ok'])
            self.assertEqual(provider.upload_calls.count(provider.fail_upload), 1)
            self.assertEqual(result['automatic_retries'], 0)

    def test_strict_search_checkpoint_continues_without_source_reupload(self):
        from quark_vfs.models import VfsError
        with tempfile.TemporaryDirectory() as temporary:
            path = self.configured(Path(temporary).resolve())
            provider = self.provider_and_binding(load_config(path))
            with patch('quark_vfs.provider.quark.QuarkProvider', return_value=provider):
                harness = M1Acceptance(path)
                with patch.object(provider, 'search', side_effect=VfsError('UPSTREAM_FAILED','Synthetic search failure')):
                    self.assertFalse(harness.run('all')['ok'])
                checkpoint = harness.local / 'journal.jsonl'
                resumed = M1Acceptance.from_search_checkpoint(path, checkpoint)
                result = resumed.resume_after_search('all')
                self.assertTrue(result['ok'], result)
                self.assertEqual(provider.upload_calls.count('source-'+harness.id+'.md'),1)
                with self.assertRaises(VfsError):
                    M1Acceptance.from_search_checkpoint(path,checkpoint)

    def test_checkpoint_rejects_changed_local_source(self):
        from quark_vfs.models import VfsError
        with tempfile.TemporaryDirectory() as temporary:
            path = self.configured(Path(temporary).resolve())
            provider = self.provider_and_binding(load_config(path))
            with patch('quark_vfs.provider.quark.QuarkProvider', return_value=provider):
                harness = M1Acceptance(path)
                with patch.object(provider, 'search', side_effect=VfsError('UPSTREAM_FAILED','Synthetic failure')):
                    harness.run('basic')
                (harness.local/('source-'+harness.id+'.md')).write_bytes(b'changed')
                with self.assertRaises(VfsError):
                    M1Acceptance.from_search_checkpoint(path,harness.local/'journal.jsonl')

    def test_predispatch_move_checkpoint_preserves_prior_uploads(self):
        from quark_vfs.models import VfsError
        with tempfile.TemporaryDirectory() as temporary:
            path = self.configured(Path(temporary).resolve())
            provider = self.provider_and_binding(load_config(path))
            with patch('quark_vfs.provider.quark.QuarkProvider',return_value=provider):
                original = M1Acceptance(path)
                with patch.object(provider,'search',side_effect=VfsError('UPSTREAM_FAILED','Synthetic failure')):
                    original.run('all')
                checkpoint=original.local/'journal.jsonl'
                first=M1Acceptance.from_search_checkpoint(path,checkpoint)
                with patch('quark_vfs.core.operations.Operations.move',side_effect=VfsError('UPSTREAM_FAILED','Predispatch read failure')):
                    self.assertFalse(first.resume_after_search('all')['ok'])
                calls=list(provider.upload_calls)
                text=checkpoint.read_text()
                events=[json.loads(line) for line in text.splitlines()]
                events[-2]['result']['error']['job_id']='uncertain-write-job'
                checkpoint.write_text('\n'.join(json.dumps(event) for event in events)+'\n')
                with self.assertRaises(VfsError):
                    M1Acceptance.from_predispatch_move_checkpoint(path,checkpoint)
                checkpoint.write_text(text)
                resumed=M1Acceptance.from_predispatch_move_checkpoint(path,checkpoint)
                self.assertTrue(resumed.resume_predispatch_move('all')['ok'])
                self.assertEqual(provider.upload_calls.count('source-'+original.id+'.md'),1)
                self.assertEqual(provider.upload_calls.count('nested.md'),1)
                self.assertEqual(provider.upload_calls[:len(calls)],calls)
                with self.assertRaises(VfsError):
                    M1Acceptance.from_predispatch_move_checkpoint(path,checkpoint)

    def test_verified_restore_checkpoint_does_not_repeat_trash(self):
        from quark_vfs.core.trash import TrashService
        from quark_vfs.models import VfsError
        with tempfile.TemporaryDirectory() as temporary:
            path=self.configured(Path(temporary).resolve())
            provider=self.provider_and_binding(load_config(path))
            with patch('quark_vfs.provider.quark.QuarkProvider',return_value=provider):
                initial=M1Acceptance(path)
                with patch.object(provider,'search',side_effect=VfsError('UPSTREAM_FAILED','Synthetic search failure')):
                    initial.run('all')
                journal=initial.local/'journal.jsonl'
                search=M1Acceptance.from_search_checkpoint(path,journal)
                with patch('quark_vfs.core.operations.Operations.move',side_effect=VfsError('UPSTREAM_FAILED','Predispatch read')):
                    search.resume_after_search('all')
                move=M1Acceptance.from_predispatch_move_checkpoint(path,journal)
                def legacy_restore(service,operation_id,entry_id,**kwargs):
                    state=service.journal.get(service.account,operation_id)
                    state['entries'][0].pop('payload_expected')
                    service._save(state)
                    raise VfsError('EXPECTED_CHANGED','Legacy post-trash observation absent')
                def legacy_completion(service,state):
                    return json.dumps({'schema_version':1,'account':service.account,'operation_id':state['operation_id'],
                                       'entries':[{k:v for k,v in item.items() if k!='payload_expected'} for item in state['entries']]}).encode()
                with patch.object(TrashService,'restore',legacy_restore), patch.object(TrashService,'_completion_bytes',legacy_completion):
                    self.assertFalse(move.resume_predispatch_move('all')['ok'])
                events=[json.loads(line) for line in journal.read_text().splitlines()]
                state=next(e['result']['data'] for e in events if e.get('event')=='response' and e['step'].endswith('-trash'))
                row=state['manifest']['entries'][0]
                move.record('restore_payload_readonly_verified',operation_id=state['operation_id'],entry_id=row['entry_id'],
                    sha256=hashlib.sha256(b'M1 synthetic document\n').hexdigest(),size=22,
                    bytes_equal_known_source=True,mutation_dispatched=False)
                resumed=M1Acceptance.from_restore_checkpoint(path,journal)
                result=resumed.resume_restore('all')
                self.assertTrue(result['ok'],result)
                self.assertEqual(provider.upload_calls.count('source-'+initial.id+'.md'),1)
                self.assertEqual(provider.upload_calls.count('nested.md'),1)
                with self.assertRaises(VfsError):
                    M1Acceptance.from_restore_checkpoint(path,journal)

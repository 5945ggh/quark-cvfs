"""Bounded tree behavior through the Catalog interface."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from test_core import FakeProvider

from quark_vfs.core.catalog import Catalog
from quark_vfs.models import FileRef, RemoteEntry, ScopeBinding, VfsError
from quark_vfs.storage import Store


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.provider = FakeProvider()
        self.calls = []
        original = self.provider.list_directory
        def listing(account, fid):
            self.calls.append(fid)
            rows = self.provider.rows[fid]
            if isinstance(rows, Exception):
                raise rows
            return original(account, fid)
        self.provider.list_directory = listing
        self.store = Store(root / 'state.db')
        self.addCleanup(self.store.close)
        self.store.bind(ScopeBinding('a', 'uuid', 'marker', 'scope-marker.json',
            hashlib.sha256(self.provider.content['marker']).hexdigest(), 'marker-parent'))
        self.catalog = Catalog(self.provider, self.store, root / 'workspace')
        self.ref = FileRef('a', path='/')

    def directory(self, fid, parent='0', name=None):
        entry = RemoteEntry(fid, parent, name or fid, 'directory')
        self.provider.rows[parent].append(entry)
        self.provider.rows.setdefault(fid, [])

    def test_depth_and_single_scope(self):
        self.directory('a')
        self.directory('b', 'a')
        self.provider.rows['b'].append(RemoteEntry('f', 'b', 'f', 'file', 8))
        for depth, count in [(0, 0), (1, 1), (2, 2)]:
            self.calls.clear()
            result = self.catalog.tree(self.ref, depth=depth)
            self.assertEqual(result['stats']['entries'], count)
            self.assertTrue(result['complete_for_depth'])
            self.assertEqual(self.calls.count('marker-parent'), 1)
            self.assertEqual(result['stats']['directories_attempted'], depth)

    def test_full_snapshot_and_entry_budget(self):
        self.provider.rows['0'] = [RemoteEntry(str(i), '0', f'{i:03}', 'file', i) for i in range(150)]
        self.assertEqual(self.catalog.tree(self.ref)['stats']['files'], 150)
        result = self.catalog.tree(self.ref, max_entries=3)
        self.assertEqual(len(result['root']['children']), 3)
        self.assertEqual(result['root']['omitted_entries'], 147)
        self.assertFalse(result['complete_for_depth'])

    def test_directory_budget_and_breadth_first(self):
        self.directory('a')
        self.directory('b')
        self.directory('c', 'a')
        result = self.catalog.tree(self.ref, depth=3, max_directories=1)
        self.assertEqual(self.calls, ['marker-parent', '0'])
        self.assertEqual(result['root']['listing_status'], 'complete')
        self.assertEqual([n['listing_status'] for n in result['root']['children']], ['directory_limit'] * 2)
        self.calls.clear()
        result = self.catalog.tree(self.ref, depth=3, max_entries=2)
        self.assertEqual([n['fid'] for n in result['root']['children']], ['a', 'b'])
        self.assertEqual(self.calls, ['marker-parent', '0'])

    def test_branch_error_and_auth_abort_are_safe(self):
        for code, status, expected in [('REMOTE_ERROR', 'complete', ['marker-parent', '0', 'a', 'b']),
                                       ('AUTH_REQUIRED', 'aborted', ['marker-parent', '0', 'a'])]:
            self.provider.rows['0'] = []
            self.directory('a')
            self.directory('b')
            error = VfsError(code, 'secret-cookie')
            error.upstream_code = 401
            self.provider.rows['a'] = error
            self.calls.clear()
            result = self.catalog.tree(self.ref)
            first, second = result['root']['children']
            self.assertEqual(first['listing_status'], 'failed')
            self.assertEqual(second['listing_status'], status)
            self.assertNotIn('secret-cookie', str(result))
            self.assertEqual(first['error']['upstream_code'], 401)
            self.assertEqual(self.calls, expected)

    def test_repeat_identity_and_unknown_size(self):
        self.directory('x|1')
        self.directory('y|2')
        self.provider.rows['x|1'] = [RemoteEntry('z|2', 'x|1', 'duplicate', 'directory'),
                                    RemoteEntry('f', 'x|1', 'unknown', 'file')]
        result = self.catalog.tree(self.ref, depth=3)
        duplicate = result['root']['children'][0]['children'][0]
        self.assertEqual(duplicate['fid'], 'z|2')
        self.assertEqual(duplicate['listing_status'], 'repeated')
        self.assertEqual(result['stats']['files_without_size'], 1)
        self.assertFalse(result['complete_for_depth'])

    def test_invalid_limits_precede_provider(self):
        for kwargs in [{'depth': True}, {'depth': -1}, {'depth': 33}, {'max_entries': 0},
                       {'max_entries': 10001}, {'max_directories': 0}, {'max_directories': 1001}]:
            with self.assertRaises(VfsError) as caught:
                self.catalog.tree(self.ref, **kwargs)
            self.assertEqual(caught.exception.code, 'INVALID_TREE_LIMIT')
        self.assertEqual(self.calls, [])

    def test_file_root_rejected(self):
        self.provider.rows['0'] = [RemoteEntry('f', '0', 'file', 'file')]
        with self.assertRaises(VfsError) as caught:
            self.catalog.tree(FileRef('a', path='/file'))
        self.assertEqual(caught.exception.code, 'NOT_DIRECTORY')

    def test_exact_directory_budget_and_cycle(self):
        self.directory('b')
        self.directory('a')
        self.provider.rows['a'] = [RemoteEntry('0', 'a', 'cycle', 'directory')]
        result = self.catalog.tree(self.ref, depth=3, max_directories=2)
        self.assertEqual(self.calls, ['marker-parent', '0', 'a'])
        self.assertEqual(result['stats']['directories_attempted'], 2)
        first, second = result['root']['children']
        self.assertEqual(first['children'][0]['listing_status'], 'repeated')
        self.assertEqual(second['listing_status'], 'directory_limit')

    def test_stable_order_and_known_bytes(self):
        self.provider.rows['0'] = [RemoteEntry('f2', '0', 'a', 'file', 3),
                                    RemoteEntry('f1', '0', 'a', 'file', 7)]
        self.directory('z')
        result = self.catalog.tree(self.ref, depth=1)
        self.assertEqual([n['fid'] for n in result['root']['children']], ['z', 'f1', 'f2'])
        self.assertEqual(result['stats']['known_file_bytes'], 10)
        self.assertEqual(result['stats']['directories_listed'], 1)
        self.assertTrue(result['root']['snapshot_id'])

    def test_complete_listing_at_exact_entry_limit(self):
        self.provider.rows['0'] = [RemoteEntry('f', '0', 'f', 'file')]
        result = self.catalog.tree(self.ref, max_entries=1)
        self.assertTrue(result['complete_for_depth'])
        self.assertEqual(result['root']['listing_status'], 'complete')
        self.assertNotIn('omitted_entries', result['root'])

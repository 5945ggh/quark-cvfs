import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from quark_vfs.cli import main
from quark_vfs.models import VfsError


def tree_result(complete=True):
    return {'root': {'name': '根', 'kind': 'directory', 'depth': 0,
                     'listing_status': 'complete', 'children': [
                         {'name': 'a\n\x1b[31m', 'kind': 'file', 'depth': 1},
                         {'name': '目录', 'kind': 'directory', 'depth': 1,
                          'children': [], 'listing_status': 'depth_limit'}]},
            'requested_depth': 1, 'limits': {'max_directories': 100, 'max_entries': 1000},
            'stats': {'entries': 2, 'files': 1, 'directories': 1,
                      'directories_attempted': 1, 'directories_listed': 1,
                      'known_file_bytes': 0, 'files_without_size': 1},
            'complete_for_depth': complete, 'consistency': 'per_directory'}


class TreeCliTests(unittest.TestCase):
    def invoke(self, extra=(), result=None, error=None):
        output = io.StringIO()
        with patch('quark_vfs.cli._file_dispatch', return_value=result or tree_result(),
                   side_effect=error), contextlib.redirect_stdout(output):
            code = main(['--config', 'examples/config.toml', 'tree', '--ref',
                         '{"fid":"root"}', *extra])
        return code, output.getvalue()

    def test_default_json_and_complete_exit(self):
        code, output = self.invoke()
        value = json.loads(output)
        self.assertEqual(code, 0)
        self.assertTrue(value['ok'])
        self.assertEqual(value['data'], tree_result())
        self.assertIn('operation_id', value)

    def test_partial_keeps_data_and_has_tree_error(self):
        code, output = self.invoke(result=tree_result(False))
        value = json.loads(output)
        self.assertEqual(code, 1)
        self.assertFalse(value['ok'])
        self.assertEqual(value['error']['code'], 'TREE_INCOMPLETE')
        self.assertEqual(value['data'], tree_result(False))

    def test_text_connections_and_safe_names(self):
        code, output = self.invoke(['--format', 'text'])
        self.assertEqual(code, 0)
        self.assertIn('├── a\\n\\u001b[31m', output)
        self.assertIn('└── 目录/', output)
        self.assertNotIn('\x1b', output)
        self.assertIn('depth_limit', output)
        self.assertIn('非全盘', output)
        self.assertIn('非原子', output)
        self.assertIn('max_entries=1000', output)

    def test_unicode_controls_are_escaped_and_nested_connections_preserved(self):
        result = tree_result()
        result['root']['children'][0]['name'] = '中文\u2028\u2029\u202e名字'
        result['root']['children'][1]['children'] = [
            {'name': 'nested', 'kind': 'file', 'depth': 2}]
        _, output = self.invoke(['--format', 'text'], result=result)
        self.assertIn('中文\\u2028\\u2029\\u202e名字', output)
        self.assertIn('    └── nested', output)

    def test_root_slash_is_rendered_once(self):
        result = tree_result()
        result['root']['name'] = '/'
        code, output = self.invoke(['--format', 'text'], result=result)
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines()[0], '/')

    def test_tree_dispatch_owns_scope_and_ignores_unrelated_trash_root(self):
        from quark_vfs.cli import _file_dispatch
        args = SimpleNamespace(command='tree', ref='{"fid":"root"}', depth=2,
                               max_directories=100, max_entries=1000)
        config = SimpleNamespace(file_operations_enabled=True, account='test',
                                 state_dir=Path('/tmp/state'),
                                 cache_dir=Path('/tmp/cache'),
                                 trash_root_fid='unrelated')
        with patch('quark_vfs.storage.Store'), patch('quark_vfs.provider.quark.QuarkProvider'), \
                patch('quark_vfs.core.catalog.Catalog', autospec=True) as catalog:
            catalog.return_value.tree.return_value = tree_result()
            self.assertEqual(_file_dispatch(args, config), tree_result())
            catalog.return_value.assert_scope.assert_not_called()
            catalog.return_value.stat.assert_not_called()
            call = catalog.return_value.tree.call_args
            self.assertEqual(call.args[0].fid, 'root')
            self.assertEqual(call.kwargs, {'depth': 2, 'max_directories': 100, 'max_entries': 1000})

    def test_tree_file_operations_gate_precedes_provider(self):
        output = io.StringIO()
        with patch('quark_vfs.provider.quark.QuarkProvider') as provider, contextlib.redirect_stdout(output):
            code = main(['--config', 'examples/config.toml', 'tree', '--ref', '{"fid":"root"}'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())['error']['code'], 'CAPABILITY_UNVERIFIED')
        provider.assert_not_called()

    def test_invalid_reference_precedes_provider_and_local_state_creation(self):
        from quark_vfs.cli import _file_dispatch
        invalid = ['not-json', '{"path":123}', '{"fid":false}',
                   '{"fid":[]}', '{"fid":"root","parent_fid_hint":{}}',
                   '{"fid":""}', '{"path":"/","parent_fid_hint":""}']
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(file_operations_enabled=True, account='test',
                                     state_dir=Path(directory) / 'state',
                                     cache_dir=Path(directory) / 'cache')
            for ref in invalid:
                with self.subTest(ref=ref), patch('quark_vfs.provider.quark.QuarkProvider') as provider:
                    args = SimpleNamespace(command='tree', ref=ref)
                    with self.assertRaises(VfsError) as error:
                        _file_dispatch(args, config)
                    self.assertEqual(error.exception.code, 'INVALID_REFERENCE')
                    provider.assert_not_called()
                    self.assertFalse(config.state_dir.exists())
                    self.assertFalse(config.cache_dir.exists())

    def test_text_partial_and_global_error(self):
        code, output = self.invoke(['--format', 'text'], result=tree_result(False))
        self.assertEqual(code, 1)
        self.assertIn('TREE_INCOMPLETE', output)
        code, output = self.invoke(['--format', 'text'], error=VfsError('SCOPE_MISMATCH', 'Scope mismatch.'))
        self.assertEqual(code, 1)
        self.assertIn('SCOPE_MISMATCH', output)
        self.assertIn('Scope mismatch.', output)

    def test_invalid_limits_rejected_before_config_or_provider(self):
        for flag, values in {'--depth': ['-1', '33', 'x'], '--max-directories': ['0', '1001'],
                             '--max-entries': ['0', '10001']}.items():
            for value in values:
                with self.subTest(flag=flag, value=value), patch('quark_vfs.cli.load_config') as config:
                    code, output = self.invoke([flag, value])
                    self.assertEqual(code, 1)
                    self.assertEqual(json.loads(output)['error']['code'], 'INVALID_ARGUMENT')
                    config.assert_not_called()

    def test_help_lists_all_options(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_value:
            main(['tree', '--help'])
        self.assertEqual(exit_value.exception.code, 0)
        for flag in ('--ref', '--depth', '--max-directories', '--max-entries', '--format'):
            self.assertIn(flag, output.getvalue())

    def test_existing_commands_remain_json(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['version']), 0)
        self.assertTrue(json.loads(output.getvalue())['ok'])

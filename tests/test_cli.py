import contextlib
import io
import json
import unittest
from unittest.mock import patch

from quark_vfs.cli import main


class CliTests(unittest.TestCase):
    def invoke(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    def test_version(self):
        code, value = self.invoke(['version'])
        self.assertEqual(code, 0)
        self.assertIn('operation_id', value)
        self.assertEqual(value['data']['wrapper_version'], '0.1.0')

    def test_configuration_is_explicit(self):
        code, value = self.invoke(['doctor'])
        self.assertEqual(code, 1)
        self.assertEqual(value['error']['code'], 'CONFIG_REQUIRED')

    def test_default_doctor_does_not_probe(self):
        with patch('quark_vfs.cli.resolve_agent') as probe:
            _, value = self.invoke(['--config', 'examples/config.toml', 'doctor'])
        probe.assert_not_called()
        self.assertEqual(value['data']['authentication'], 'unknown')
        self.assertFalse(any(value['data']['features'].values()))

    def test_arbitrary_arguments_rejected_without_echo(self):
        _, value = self.invoke(['doctor', '--raw-args', 'secret-token'])
        self.assertEqual(value['error']['code'], 'INVALID_ARGUMENT')
        self.assertNotIn('secret-token', str(value))

    def test_doctor_reports_failed_readiness_with_checks(self):
        code, value = self.invoke(['--config', 'examples/config.toml', 'doctor'])
        self.assertEqual(code, 1)
        self.assertEqual(value['error']['code'], 'RUNTIME_UNSAFE')
        self.assertFalse(value['data']['local_runtime_ready'])
        self.assertIn('checks', value['data'])

    def test_status_is_a_snapshot_with_failed_readiness(self):
        code, value = self.invoke(['--config', 'examples/config.toml', 'status'])
        self.assertEqual(code, 0)
        self.assertFalse(value['data']['local_runtime_ready'])

    def test_auth_rejects_token_input(self):
        code, value = self.invoke(['--config', 'examples/config.toml', 'auth', 'login', '--token', 'secret'])
        self.assertEqual(code, 1)
        self.assertEqual(value['error']['code'], 'INVALID_ARGUMENT')
        self.assertNotIn('secret', str(value))

    def test_auth_start_and_result_share_operation_id(self):
        output = io.StringIO()

        def fake_login(config, *, on_started):
            on_started()
            return {'authentication': 'authenticated', 'login_state': 'confirmed'}

        with patch('quark_vfs.cli.login', side_effect=fake_login), contextlib.redirect_stdout(output):
            code = main(['--config', 'examples/config.toml', 'auth', 'login'])
        values = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code, 0)
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0]['operation_id'], values[1]['operation_id'])
        self.assertEqual(values[0]['data']['browser_opened'], 'unknown')

    def test_status_refresh_is_explicit(self):
        with patch('quark_vfs.cli.refresh_status', return_value={'authentication': 'authenticated'}) as refresh:
            _, value = self.invoke(['--config', 'examples/config.toml', 'status', '--refresh'])
        refresh.assert_called_once()
        self.assertEqual(value['data']['authentication'], 'authenticated')

    def test_file_commands_remain_gated_by_default(self):
        code, value = self.invoke(['--config', 'examples/config.toml', 'list', '--ref', '{"fid":"0"}'])
        self.assertEqual(code, 1)
        self.assertEqual(value['error']['code'], 'CAPABILITY_UNVERIFIED')

    def test_core_result_uses_the_shared_envelope(self):
        with patch('quark_vfs.cli._file_dispatch', return_value={'complete': True, 'entries': []}):
            code, value = self.invoke(['--config', 'examples/config.toml', 'list', '--ref', '{"fid":"0"}'])
        self.assertEqual(code, 0)
        self.assertEqual(value['data'], {'complete': True, 'entries': []})
        self.assertIn('operation_id', value)

    def test_manifest_path_cannot_be_used_as_reference(self):
        from quark_vfs.cli import _ref
        from quark_vfs.models import VfsError
        with self.assertRaises(VfsError):
            _ref('{"fid":"safe","action":"execute"}', 'default')

    def test_scope_initialization_verifies_fresh_marker_before_binding(self):
        import tempfile
        from pathlib import Path

        from quark_vfs.cli import _bind_scope, _init_scope
        from quark_vfs.config import Config
        from quark_vfs.models import DownloadResult, RemoteEntry, UploadResult, VfsError
        from quark_vfs.storage import Store

        class Provider:
            def __init__(self):
                self.name = None
                self.payload = None
                self.calls = 0

            def mkdir(self, account, parent, name):
                self.calls += 1
                self.name = name
                return 'native-directory-id'

            def list_directory(self, account, parent):
                if parent == '0':
                    if self.name is None:
                        return ()
                    return (RemoteEntry('observed-directory', '0', self.name, 'directory'),)
                if self.payload is None:
                    return ()
                return (RemoteEntry('observed-marker', parent, self.payload[0], 'file', len(self.payload[1])),)

            def upload_file(self, account, source, parent):
                self.payload = (source.name, source.read_bytes())
                return UploadResult('native-upload-id', 'record', len(self.payload[1]), source.name, 'success')

            def download_file(self, account, fid, directory):
                target = directory / self.payload[0]
                target.write_bytes(self.payload[1])
                return DownloadResult(target, target.name)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = Config(root / 'node', root / 'official/scripts/quark-drive.cjs', root / 'state', root / 'cache',
                            file_operations_enabled=True)
            provider = Provider()
            store = Store(root / 'state/database.sqlite3')
            try:
                result = _init_scope(config, provider, store, '0')
                self.assertTrue(result['namespace_verified'])
                self.assertEqual(store.binding('default').marker_fid, 'observed-marker')
                self.assertEqual(store.cached_entry('default', 'observed-directory').parent_fid, '0')
                with self.assertRaises(VfsError):
                    _init_scope(config, provider, store, '0')
                self.assertEqual(provider.calls, 1)
                from types import SimpleNamespace
                second = Store(root / 'state/rebuilt.sqlite3')
                try:
                    args = SimpleNamespace(scope_uuid=result['scope_uuid'], management_parent_fid='0',
                                           marker_fid=None, management_root_fid=None)
                    bound = _bind_scope(config, provider, second, args)
                    self.assertTrue(bound['namespace_verified'])
                    self.assertEqual(second.binding('default').marker_fid, 'observed-marker')
                    self.assertEqual(provider.calls, 1)
                finally:
                    second.close()
            finally:
                store.close()

    def test_partial_batch_is_not_a_successful_envelope(self):
        with patch('quark_vfs.cli._file_dispatch', return_value={'job_id': 'safe-job', 'state': 'partial', 'items': [{'state': 'succeeded'}]}):
            code, value = self.invoke(['--config', 'examples/config.toml', 'upload', '--parent', '{"fid":"0"}', '--local-path', '/synthetic.md'])
        self.assertEqual(code, 1)
        self.assertFalse(value['ok'])
        self.assertEqual(value['error']['code'], 'PARTIAL_FAILURE')
        self.assertEqual(value['data']['items'][0]['state'], 'succeeded')

    def test_retryable_failure_preserves_safe_job_identifier(self):
        from quark_vfs.models import VfsError
        error = VfsError('ACCOUNT_BUSY', 'Busy', retryable=True)
        error.job_id = 'safe-job'
        with patch('quark_vfs.cli._file_dispatch', side_effect=error):
            code, value = self.invoke(['--config', 'examples/config.toml', 'jobs'])
        self.assertEqual(code, 1)
        self.assertTrue(value['error']['retryable'])
        self.assertEqual(value['data']['job_id'], 'safe-job')

    def test_trash_and_restore_preserve_actionable_business_errors(self):
        from quark_vfs.core.trash import TrashError
        cases = [
            (['trash', '--ref', '{"fid":"source"}'], 'SOURCE_CHANGED',
             'Fresh source reference has changed.'),
            (['restore', '--operation-id', 'operation', '--entry-id', 'entry'], 'NAME_CONFLICT',
             'Recovery target name already exists; no restore intent or movement was dispatched.'),
            (['restore', '--operation-id', 'operation', '--entry-id', 'entry'], 'RECONCILIATION_REQUIRED',
             'A prior recovery attempt must be reconciled before any further movement.'),
        ]
        for arguments, error_code, message in cases:
            with self.subTest(command=arguments[0], error_code=error_code):
                with patch('quark_vfs.cli._file_dispatch', side_effect=TrashError(error_code, message)) as dispatch:
                    code, value = self.invoke(['--config', 'examples/config.toml', *arguments])
                dispatch.assert_called_once()
                self.assertEqual(code, 1)
                self.assertFalse(value['ok'])
                self.assertEqual(value['error'], {'code': error_code, 'message': message, 'retryable': False})

    def test_restore_upstream_error_still_suppresses_raw_details(self):
        from quark_vfs.provider.quark_cli import ProviderError
        with patch('quark_vfs.cli._file_dispatch', side_effect=ProviderError('NAME_CONFLICT', 'raw-upstream-secret')):
            code, value = self.invoke(['--config', 'examples/config.toml', 'restore',
                                       '--operation-id', 'operation', '--entry-id', 'entry'])
        self.assertEqual(code, 1)
        self.assertFalse(value['ok'])
        self.assertEqual(value['error']['code'], 'NAME_CONFLICT')
        self.assertNotIn('raw-upstream-secret', json.dumps(value))

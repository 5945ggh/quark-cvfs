import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_core import FakeProvider

from quark_vfs.cli import main
from quark_vfs.config import load_config
from quark_vfs.core.catalog import Catalog
from quark_vfs.models import ScopeBinding, VfsError
from quark_vfs.provider.quark_cli import ProviderError
from quark_vfs.storage import Store


class ScopeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.provider = FakeProvider()
        self.store = Store(self.root / 'state.db')
        self.addCleanup(self.store.close)
        self.binding = ScopeBinding('a', 'scope', 'marker', 'scope-marker.json',
                                    hashlib.sha256(self.provider.content['marker']).hexdigest(),
                                    'marker-parent', len(self.provider.content['marker']))
        self.store.bind(self.binding)
        self.catalog = Catalog(self.provider, self.store, self.root / 'work')

    def invoke(self, *flags):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(['--config', 'examples/config.toml', 'status', *flags])
        return code, json.loads(output.getvalue())

    def test_directory_failure_preserves_safe_codes_without_claiming_missing(self):
        error = ProviderError('UPSTREAM_FAILED', 'credential-secret')
        error.upstream_code, error.exit_code = 32003, 1
        with patch.object(self.provider, 'list_directory', side_effect=error):
            with self.assertRaises(VfsError) as caught:
                self.catalog.assert_scope('a', fresh=True)
        self.assertEqual(caught.exception.code, 'UPSTREAM_FAILED')
        self.assertEqual(caught.exception.stage, 'scope_directory_read')
        self.assertEqual(caught.exception.upstream_code, 32003)
        self.assertEqual(caught.exception.exit_code, 1)
        self.assertNotIn('credential-secret', str(caught.exception))

    def test_observed_missing_marker_differs_from_identity_change(self):
        self.provider.rows['marker-parent'] = []
        with self.assertRaises(VfsError) as caught:
            self.catalog.assert_scope('a')
        self.assertEqual(caught.exception.code, 'SCOPE_MARKER_MISSING')
        self.provider.rows['marker-parent'] = [replace(FakeProvider().rows['marker-parent'][0], fid='changed')]
        with self.assertRaises(VfsError) as caught:
            self.catalog.assert_scope('a')
        self.assertEqual(caught.exception.code, 'SCOPE_MISMATCH')
        self.assertEqual(caught.exception.stage, 'scope_marker_observation')
        self.assertEqual(self.provider.download_calls, [])

    def test_download_and_content_failure_stages_and_binding_unchanged(self):
        with patch.object(self.provider, 'download_file', side_effect=ProviderError('UPSTREAM_FAILED', 'raw')):
            with self.assertRaises(VfsError) as caught:
                self.catalog.assert_scope('a')
        self.assertEqual(caught.exception.stage, 'scope_marker_download')
        self.provider.content['marker'] = b'wrong content'
        with self.assertRaises(VfsError) as caught:
            self.catalog.assert_scope('a')
        self.assertEqual(caught.exception.stage, 'scope_marker_content')
        self.assertEqual(self.store.binding('a'), self.binding)

    def test_status_default_has_no_network(self):
        with patch('quark_vfs.cli._verify_namespace') as verify, patch('quark_vfs.cli.refresh_status') as refresh:
            code, value = self.invoke()
        self.assertEqual(code, 0)
        verify.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(value['data']['local_namespace']['scope_verification'], 'not_checked')

    def test_combined_flags_preserve_auth_success_and_scope_failure(self):
        error = VfsError('SCOPE_MISMATCH', 'raw-secret')
        error.stage = 'scope_marker_content'
        error.upstream_code = True
        with patch('quark_vfs.cli._verify_namespace', side_effect=error), patch('quark_vfs.cli.refresh_status', return_value={'authentication': 'authenticated'}):
            code, value = self.invoke('--refresh', '--verify-scope')
        self.assertEqual(code, 1)
        self.assertEqual(value['data']['authentication'], 'authenticated')
        namespace = value['data']['local_namespace']
        self.assertEqual(namespace['scope_verification'], 'mismatch')
        self.assertTrue(namespace['live_marker_checked'])
        self.assertNotIn('upstream_code', value['error'])
        self.assertNotIn('raw-secret', json.dumps(value))

    def test_auth_failure_does_not_skip_scope_verification(self):
        with patch('quark_vfs.cli._verify_namespace') as verify, patch('quark_vfs.cli.refresh_status', side_effect=ProviderError('AUTH_REQUIRED', 'raw-secret')):
            code, value = self.invoke('--refresh', '--verify-scope')
        verify.assert_called_once()
        self.assertEqual(code, 1)
        self.assertEqual(value['data']['local_namespace']['scope_verification'], 'passed')
        self.assertEqual(value['data']['authentication_error']['code'], 'AUTH_REQUIRED')
        self.assertNotIn('raw-secret', json.dumps(value))

    def test_local_job_failure_state_is_successful_diagnostic_query(self):
        output = io.StringIO()
        with patch('quark_vfs.cli._file_dispatch', return_value={'state': 'failed', 'local_only': True}), contextlib.redirect_stdout(output):
            code = main(['--config', 'examples/config.toml', 'jobs', '--job-id', 'recorded'])
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(value['ok'])
        self.assertEqual(value['data']['state'], 'failed')

    def test_scope_success_does_not_query_authentication(self):
        with patch('quark_vfs.cli._verify_namespace') as verify, patch('quark_vfs.cli.refresh_status') as refresh:
            code, value = self.invoke('--verify-scope')
        self.assertEqual(code, 0)
        verify.assert_called_once()
        refresh.assert_not_called()
        self.assertEqual(value['data']['authentication'], 'unknown')
        self.assertEqual(value['data']['local_namespace']['scope_verification'], 'passed')

    def test_real_scope_cli_uses_read_only_binding_with_feature_disabled(self):
        config = replace(load_config('examples/config.toml'), account='a', state_dir=self.root / 'state', cache_dir=self.root / 'cache', file_operations_enabled=False)
        config.cache_dir.mkdir()
        store = Store(config.state_dir / 'quark-vfs.sqlite3')
        store.bind(self.binding)
        before = list(store.db.iterdump())
        store.close()
        output = io.StringIO()
        with patch('quark_vfs.cli.load_config', return_value=config), patch('quark_vfs.provider.quark.QuarkProvider', return_value=self.provider), contextlib.redirect_stdout(output):
            code = main(['--config', 'test', 'status', '--verify-scope'])
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(value['data']['local_namespace']['scope_verification'], 'passed')
        read = Store(config.state_dir / 'quark-vfs.sqlite3', read_only=True)
        self.assertEqual(list(read.db.iterdump()), before)
        read.close()
        self.assertEqual(len(self.provider.download_calls), 1)

    def test_real_scope_cli_missing_bad_and_unbound_local_state(self):
        config = replace(load_config('examples/config.toml'), account='a', state_dir=self.root / 'state', cache_dir=self.root / 'cache')
        database = config.state_dir / 'quark-vfs.sqlite3'
        for state, expected in [('missing', 'STATE_NOT_FOUND'), ('bad', 'STATE_UNREADABLE'), ('unbound', 'ACCOUNT_UNBOUND')]:
            with self.subTest(state=state):
                if database.exists():
                    database.unlink()
                if state == 'bad':
                    database.parent.mkdir(exist_ok=True)
                    database.write_bytes(b'not sqlite')
                if state == 'unbound':
                    store = Store(database)
                    store.close()
                output = io.StringIO()
                with patch('quark_vfs.cli.load_config', return_value=config), patch('quark_vfs.provider.quark.QuarkProvider', return_value=self.provider), contextlib.redirect_stdout(output):
                    code = main(['--config', 'test', 'status', '--verify-scope'])
                value = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(value['error']['code'], expected)
                self.assertEqual(value['data']['local_namespace']['scope_verification'], 'unavailable')
                self.assertEqual(self.provider.download_calls, [])

    def test_file_error_numeric_fields_only(self):
        for upstream, exit_code in [(32003, 1), (True, 'secret')]:
            error = ProviderError('UPSTREAM_FAILED', 'raw-secret')
            error.upstream_code, error.exit_code = upstream, exit_code
            output = io.StringIO()
            with patch('quark_vfs.cli._file_dispatch', side_effect=error), contextlib.redirect_stdout(output):
                code = main(['--config', 'examples/config.toml', 'list', '--ref', '{"fid":"0"}'])
            value = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertNotIn('raw-secret', json.dumps(value))
            if type(upstream) is int:
                self.assertEqual(value['error']['upstream_code'], upstream)
                self.assertEqual(value['error']['exit_code'], exit_code)
            else:
                self.assertNotIn('upstream_code', value['error'])
                self.assertNotIn('exit_code', value['error'])

    def test_two_diagnostic_failures_are_both_retained(self):
        error = VfsError('UPSTREAM_FAILED', 'raw-secret', stage='scope_directory_read')
        with patch('quark_vfs.cli._verify_namespace', side_effect=error), patch('quark_vfs.cli.refresh_status', side_effect=ProviderError('AUTH_REQUIRED', 'credential')):
            code, value = self.invoke('--refresh', '--verify-scope')
        self.assertEqual(code, 1)
        self.assertEqual(value['data']['authentication_error']['code'], 'AUTH_REQUIRED')
        namespace = value['data']['local_namespace']
        self.assertEqual(namespace['verification_error']['code'], 'UPSTREAM_FAILED')
        self.assertFalse(namespace['live_marker_checked'])
        self.assertNotIn('credential', json.dumps(value))

    def test_bound_parent_ignores_unused_broken_management_document(self):
        self.store.db.execute("INSERT INTO documents VALUES ('a','management','root','broken-json')")
        self.store.db.commit()
        self.catalog.assert_scope('a')
        self.assertEqual(self.store.binding('a'), self.binding)

    def test_fallback_management_corruption_is_safe_binding_failure(self):
        binding = replace(self.binding, marker_parent_fid=None)
        from dataclasses import asdict
        self.store.db.execute('UPDATE bindings SET data=? WHERE account=?', (json.dumps(asdict(binding)), 'a'))
        for value in ('broken-json-secret', '["secret"]', '{"fid": 123}', '{"fid": ""}'):
            with self.subTest(value=value):
                self.store.db.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,?)', ('a', 'management', 'root', value))
                self.store.db.commit()
                with patch.object(self.provider, 'list_directory') as listing:
                    with self.assertRaises(VfsError) as caught:
                        self.catalog.assert_scope('a')
                listing.assert_not_called()
                self.assertEqual(caught.exception.code, 'STATE_UNREADABLE')
                self.assertEqual(caught.exception.stage, 'scope_binding')
                self.assertNotIn('secret', str(caught.exception))

    def test_persisted_binding_identity_validation_precedes_provider(self):
        from dataclasses import asdict
        for field, value in [('account', 'other'), ('marker_fid', 123), ('marker_name', '../secret'),
                             ('marker_sha256', 'bad'), ('marker_parent_fid', []), ('marker_size', True)]:
            with self.subTest(field=field):
                fields = asdict(self.binding)
                fields[field] = value
                self.store.db.execute('UPDATE bindings SET data=? WHERE account=?', (json.dumps(fields), 'a'))
                self.store.db.commit()
                with patch.object(self.provider, 'list_directory') as listing:
                    with self.assertRaises(VfsError) as caught:
                        self.catalog.assert_scope('a')
                listing.assert_not_called()
                self.assertEqual(caught.exception.code, 'STATE_UNREADABLE')
                self.assertEqual(caught.exception.stage, 'scope_binding')
                read = Store(self.store.path, read_only=True)
                try:
                    with self.assertRaises(VfsError) as read_error:
                        read.binding('a')
                    self.assertEqual(read_error.exception.code, 'STATE_UNREADABLE')
                finally:
                    read.close()

    def test_fallback_database_read_failure_returns_safe_cli_json(self):
        config = replace(load_config('examples/config.toml'), account='a', state_dir=self.root / 'state', cache_dir=self.root / 'cache')
        store = Store(config.state_dir / 'quark-vfs.sqlite3')
        store.bind(replace(self.binding, marker_parent_fid=None))
        store.close()
        output = io.StringIO()
        with patch('quark_vfs.cli.load_config', return_value=config), patch('quark_vfs.provider.quark.QuarkProvider', return_value=self.provider), patch.object(Store, 'get_document', side_effect=sqlite3.DatabaseError('raw-secret')), patch.object(self.provider, 'list_directory') as listing, contextlib.redirect_stdout(output):
            code = main(['--config', 'test', 'status', '--verify-scope'])
        listing.assert_not_called()
        value = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(value['error']['code'], 'STATE_UNREADABLE')
        self.assertEqual(value['error']['stage'], 'scope_binding')
        self.assertNotIn('raw-secret', json.dumps(value))

    def test_configuration_and_argument_errors_preserve_actionable_guidance(self):
        for arguments, expected in [(['status'], '--config'), (['status', '--secret-option'], '--help')]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(arguments)
            value = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertIn(expected, value['error']['message'])
            self.assertNotIn('--secret-option', value['error']['message'])

    def test_ordinary_upstream_failure_is_safe_and_correctly_described(self):
        output = io.StringIO()
        error = ProviderError('UPSTREAM_FAILED', 'raw-credential-secret')
        with patch('quark_vfs.cli._file_dispatch', side_effect=error), contextlib.redirect_stdout(output):
            code = main(['--config', 'examples/config.toml', 'list', '--ref', '{"fid":"0"}'])
        value = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertIn('official operation', value['error']['message'])
        self.assertNotIn('Local diagnostic', value['error']['message'])
        self.assertNotIn('raw-credential-secret', json.dumps(value))

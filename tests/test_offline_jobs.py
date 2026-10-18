import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quark_vfs.cli import main
from quark_vfs.models import VfsError
from quark_vfs.storage import Store, read_local_jobs


class OfflineJobsTests(unittest.TestCase):
    def test_readonly_reports_incomplete_schema_and_malformed_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'state.db'
            with sqlite3.connect(path) as database:
                database.execute('CREATE TABLE bindings(account TEXT, data TEXT)')
            with self.assertRaises(VfsError) as caught:
                Store(path, read_only=True)
            self.assertEqual(caught.exception.code, 'STATE_UNREADABLE')
            store = Store(path)
            try:
                store.db.execute('INSERT INTO bindings VALUES (?, ?)', ('alice', '{}'))
                store.db.commit()
            finally:
                store.close()
            store = Store(path, read_only=True)
            try:
                with self.assertRaises(VfsError) as caught:
                    store.binding('alice')
                self.assertEqual(caught.exception.code, 'STATE_UNREADABLE')
            finally:
                store.close()

    def test_missing_database_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'missing' / 'state.db'
            with self.assertRaises(VfsError) as caught:
                read_local_jobs(path, 'alice')
            self.assertEqual(caught.exception.code, 'STATE_NOT_FOUND')
            self.assertFalse(path.parent.exists())

    def test_corrupt_database_is_reported_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'state.db'
            path.write_bytes(b'not sqlite')
            with self.assertRaises(VfsError) as caught:
                read_local_jobs(path, 'alice')
            self.assertEqual(caught.exception.code, 'STATE_UNREADABLE')
            self.assertEqual(path.read_bytes(), b'not sqlite')

    def test_unbound_account_can_read_failed_jobs_while_writer_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'state.db'
            store = Store(path)
            try:
                job = store.create_job('alice', 'upload', [{'path': 'one'}])
                store.set_job(job, 'failed')
                store.create_job('bob', 'download', [])
                store.put_document('alice', 'trash', 'one', {'id': 'one'})
                store.put_document('alice', 'bootstrap', 'two', {'id': 'two'})
                original = path.read_bytes()
                with store.account_lock('alice'):
                    result = read_local_jobs(path, 'alice')
                    detail = read_local_jobs(path, 'alice', job)
                self.assertEqual([item['job_id'] for item in result['jobs']], [job])
                self.assertEqual(result['trash_operations'], ({'id': 'one'},))
                self.assertEqual(result['scope_initializations'], ({'id': 'two'},))
                self.assertEqual(detail['state'], 'failed')
                self.assertTrue(detail['local_only'])
                self.assertEqual(path.read_bytes(), original)
                with self.assertRaises(VfsError) as caught:
                    read_local_jobs(path, 'bob', job)
                self.assertEqual(caught.exception.code, 'JOB_UNKNOWN')
            finally:
                store.close()

    def test_readonly_rejects_symlink_and_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'state.db'
            Store(path).close()
            alias = Path(directory).resolve() / 'alias.db'
            alias.symlink_to(path)
            with self.assertRaises(VfsError) as caught:
                Store(alias, read_only=True)
            self.assertEqual(caught.exception.code, 'UNSAFE_STATE_PATH')
            store = Store(path, read_only=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    store.create_job('alice', 'upload', [])
            finally:
                store.close()

    def test_cli_jobs_is_offline_and_resume_stays_gated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            config = root / 'config.toml'
            source = Path('examples/config.toml').read_text()
            source = source.replace('/absolute/private/path/quark-vfs/state', str(root / 'state'))
            source = source.replace('/absolute/private/path/quark-vfs/cache', str(root / 'cache'))
            config.write_text(source)
            store = Store(root / 'state' / 'quark-vfs.sqlite3')
            try:
                job = store.create_job('default', 'upload', [])
                store.set_job(job, 'unknown')
                store.db.execute("INSERT INTO bindings VALUES (?, ?)", ('default', 'broken-marker'))
                store.db.commit()
            finally:
                store.close()
            def invoke(extra):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(['--config', str(config), 'jobs', *extra])
                return code, json.loads(output.getvalue())
            with patch('quark_vfs.cli.resolve_agent', side_effect=AssertionError('official runner prohibited')):
                code, result = invoke(['--job-id', job])
                self.assertEqual(code, 0)
                self.assertTrue(result['ok'])
                self.assertEqual(result['data']['state'], 'unknown')
                code, result = invoke(['--job-id', job, '--resume'])
                self.assertEqual(code, 1)
                self.assertEqual(result['error']['code'], 'CAPABILITY_UNVERIFIED')

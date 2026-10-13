import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from quark_vfs.config import Config
from quark_vfs.provider.quark_cli import (
    AcceptanceCall,
    ProviderError,
    build_argv,
    resolve_agent,
    run_official,
)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'scripts').mkdir()
        (root / 'codex').mkdir()
        (root / 'state').mkdir()
        (root / 'cache').mkdir()
        self.entry = root / 'scripts' / 'quark-drive.cjs'
        self.entry.write_text('print("QK_AGENT_ID=codex")')
        self.config = Config(Path(sys.executable), self.entry, root / 'state', root / 'cache', timeout_seconds=.2, max_output_bytes=1024)

    def tearDown(self):
        self.temp.cleanup()

    def test_only_whitelisted_argv(self):
        self.assertEqual(build_argv(self.config, 'resolve-agent')[-1], 'resolve-agent')
        with self.assertRaises(ProviderError):
            build_argv(self.config, 'auth')

    def test_real_environment_inherited(self):
        self.entry.write_text('import os\nprint("QK_AGENT_ID=" + os.environ["QUARK_VFS_TEST_ID"])')
        old = os.environ.get('QUARK_VFS_TEST_ID')
        os.environ['QUARK_VFS_TEST_ID'] = 'codex'
        try:
            self.assertEqual(resolve_agent(self.config), 'codex')
        finally:
            if old is None:
                del os.environ['QUARK_VFS_TEST_ID']
            else:
                os.environ['QUARK_VFS_TEST_ID'] = old

    def test_repository_cwd_rejected(self):
        (self.config.state_dir / '.git').mkdir()
        with self.assertRaises(ProviderError) as error:
            resolve_agent(self.config)
        self.assertEqual(error.exception.code, 'RUNTIME_UNSAFE')

    def test_missing_official_state_rejected(self):
        (self.config.official_root / 'codex').rmdir()
        with self.assertRaises(ProviderError):
            resolve_agent(self.config)

    def test_output_is_bounded_and_secret_suppressed(self):
        self.entry.write_text('print("secret-token" * 10000)')
        with self.assertRaises(ProviderError) as error:
            resolve_agent(self.config)
        self.assertEqual(error.exception.code, 'OUTPUT_LIMIT')
        self.assertNotIn('secret-token', str(error.exception))

    def test_timeout(self):
        self.entry.write_text('import time\ntime.sleep(10)')
        with self.assertRaises(ProviderError) as error:
            resolve_agent(self.config)
        self.assertEqual(error.exception.code, 'PROBE_TIMEOUT')

    def test_failure_does_not_reveal_stderr(self):
        self.entry.write_text('import sys\nprint("secret-token", file=sys.stderr)\nsys.exit(1)')
        with self.assertRaises(ProviderError) as error:
            resolve_agent(self.config)
        self.assertEqual(error.exception.code, 'PROBE_FAILED')
        self.assertNotIn('secret-token', str(error.exception))

    def test_unexpected_identity_output_rejected(self):
        self.entry.write_text('print("unexpected diagnostic\\nQK_AGENT_ID=codex")')
        with self.assertRaises(ProviderError) as error:
            resolve_agent(self.config)
        self.assertEqual(error.exception.code, 'AGENT_UNVERIFIED')

    def test_entry_alias_checks_actual_installation(self):
        other = self.config.state_dir / 'other' / 'scripts'
        other.mkdir(parents=True)
        target = other / 'quark-drive.cjs'
        target.write_text('print("QK_AGENT_ID=codex")')
        self.entry.unlink()
        self.entry.symlink_to(target)
        self.assertEqual(build_argv(self.config, 'resolve-agent')[1], str(target.resolve()))
        with self.assertRaises(ProviderError) as error:
            resolve_agent(self.config)
        self.assertEqual(error.exception.code, 'RUNTIME_UNSAFE')

    def test_login_uses_separate_timeout_and_started_callback(self):
        self.entry.write_text('import time\ntime.sleep(.3)\nprint("synthetic result")')
        started = []
        result = run_official(replace(self.config, login_timeout_seconds=1), 'login',
                              on_started=lambda: started.append(True))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(started, [True])

    def test_business_output_survives_nonzero_exit(self):
        self.entry.write_text('import sys\nprint("synthetic failure")\nsys.exit(1)')
        result = run_official(self.config, 'get-user-info')
        self.assertEqual(result.exit_code, 1)
        self.assertIn(b'synthetic failure', result.stdout)

    def test_acceptance_interrupts_only_its_observed_partial_regular_file(self):
        staging = self.config.state_dir.resolve() / 'm0-acceptance-synthetic' / 'partial-staging'
        staging.mkdir(parents=True)
        self.entry.write_text('import sys,time\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index("--output-dir")+1])\n(p/"synthetic.bin").write_bytes(b"x"*2048)\ntime.sleep(10)')
        request = AcceptanceCall('download-interrupt', fid='synthetic-fid', local_path=staging,
                                 expected_name='synthetic.bin', expected_size=4096)
        result = run_official(self.config, request)
        self.assertEqual(result.interrupted_size, 2048)
        self.assertNotEqual(result.exit_code, 0)

    def test_interruption_probe_refuses_preexisting_staging_content(self):
        staging = self.config.state_dir.resolve() / 'm0-acceptance-synthetic' / 'partial-staging'
        staging.mkdir(parents=True)
        (staging / 'existing.bin').write_bytes(b'preexisting')
        request = AcceptanceCall('download-interrupt', fid='synthetic-fid', local_path=staging,
                                 expected_name='synthetic.bin', expected_size=4096)
        with self.assertRaises(ProviderError) as error:
            run_official(self.config, request)
        self.assertEqual(error.exception.code, 'UNSAFE_LOCAL_PATH')

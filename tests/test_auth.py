import json
import unittest
from unittest.mock import patch

from quark_vfs.config import load_config
from quark_vfs.provider.auth import login, refresh_status
from quark_vfs.provider.quark_cli import OfficialResult, ProviderError


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config('examples/config.toml')

    def output(self, action, data, code=0):
        return OfficialResult(json.dumps({'type': 'result', 'action': action, 'code': code, 'data': data}).encode(), 0)

    def test_login_confirmation_drops_secrets(self):
        output = self.output('login', {'status': 'confirmed', 'userId': 'private', 'token': 'secret'})
        with patch('quark_vfs.provider.auth.run_official', return_value=output) as run:
            value = login(self.config)
        self.assertEqual(value['authentication'], 'authenticated')
        self.assertNotIn('private', str(value))
        self.assertNotIn('secret', str(value))
        self.assertEqual(run.call_args.args[1], 'login')

    def test_login_unknown_does_not_succeed(self):
        for status in ('pending', 'future', True, None):
            with patch('quark_vfs.provider.auth.run_official', return_value=self.output('login', {'status': status})):
                with self.assertRaises(ProviderError):
                    login(self.config)

    def test_refresh_drops_account_fields(self):
        output = self.output('get-user-info', {'userInfo': {'nickname': 'private'}, 'vipInfo': {'capacity': 123}})
        with patch('quark_vfs.provider.auth.run_official', return_value=output):
            value = refresh_status(self.config)
        self.assertEqual(value['authentication'], 'authenticated')
        self.assertNotIn('private', str(value))
        self.assertEqual(value['authorization_scope'], 'unknown')

    def test_unknown_schema_and_raw_output_are_suppressed(self):
        for output in (OfficialResult(b'secret-token', 0), self.output('future', {}), self.output('get-user-info', {}),
                       self.output('get-user-info', {'userInfo': {'nickname': []}, 'vipInfo': None}),
                       self.output('get-user-info', {'userInfo': None, 'vipInfo': None}, -107)):
            with patch('quark_vfs.provider.auth.run_official', return_value=output):
                with self.assertRaises(ProviderError) as error:
                    refresh_status(self.config)
            self.assertNotIn('secret-token', str(error.exception))

    def test_known_business_failures_survive_nonzero_exit(self):
        for action, code, expected in (('login', -118, 'AUTH_ALREADY_VALID'),
                                       ('get-user-info', -1408, 'AUTH_REQUIRED')):
            output = self.output(action, {}, code)
            output = OfficialResult(output.stdout, 1)
            with patch('quark_vfs.provider.auth.run_official', return_value=output):
                with self.assertRaises(ProviderError) as error:
                    (login if action == 'login' else refresh_status)(self.config)
            self.assertEqual(error.exception.code, expected)

    def test_success_business_with_failed_process_does_not_succeed(self):
        output = self.output('login', {'status': 'confirmed'})
        with patch('quark_vfs.provider.auth.run_official', return_value=OfficialResult(output.stdout, 1)):
            with self.assertRaises(ProviderError) as error:
                login(self.config)
        self.assertEqual(error.exception.code, 'RUNTIME_FAILED')

    def test_notice_retained_without_links_or_token(self):
        output = OfficialResult(json.dumps({'type': 'result', 'action': 'login', 'code': 0,
                                           'data': {'status': 'confirmed'},
                                           'notice': 'Membership applies token=secret https://example.com/signed'}).encode(), 0)
        with patch('quark_vfs.provider.auth.run_official', return_value=output):
            value = login(self.config)
        self.assertIn('withheld', value['upstream_notices'][0])
        self.assertNotIn('secret', str(value))
        self.assertNotIn('https://', str(value))

    def test_static_not_authenticated_action_is_classified(self):
        output = self.output('not_authenticated', {}, -1408)
        with patch('quark_vfs.provider.auth.run_official', return_value=output):
            with self.assertRaises(ProviderError) as error:
                refresh_status(self.config)
        self.assertEqual(error.exception.code, 'AUTH_REQUIRED')

    def test_structured_notice_text_is_preserved(self):
        output = OfficialResult(json.dumps({'type': 'result', 'action': 'login', 'code': 0,
                                           'data': {'status': 'confirmed'},
                                           'notice': {'kind': 'membership', 'text': 'Membership applies'}}).encode(), 0)
        with patch('quark_vfs.provider.auth.run_official', return_value=output):
            value = login(self.config)
        self.assertEqual(value['upstream_notices'], ['Membership applies'])

    def test_bearer_and_json_credentials_are_withheld(self):
        for notice in ('Authorization: Bearer synthetic-secret', '{"token":"synthetic-secret"}'):
            output = OfficialResult(json.dumps({'type': 'result', 'action': 'login', 'code': 0,
                                               'data': {'status': 'confirmed'}, 'notice': notice}).encode(), 0)
            with patch('quark_vfs.provider.auth.run_official', return_value=output):
                self.assertNotIn('synthetic-secret', str(login(self.config)))

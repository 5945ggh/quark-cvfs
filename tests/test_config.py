import tempfile
import unittest
from pathlib import Path

from quark_vfs.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text(text)
            return load_config(path)

    def test_relative_paths_rejected(self):
        with self.assertRaises(ConfigError):
            self.load('[runtime]\nnode_executable="node"\n')

    def test_feature_gate_cannot_be_enabled(self):
        text = Path('examples/config.toml').read_text().replace('mcp_verified = false', 'mcp_verified = true')
        with self.assertRaises(ConfigError):
            self.load(text)

    def test_valid_example(self):
        config = load_config('examples/config.toml')
        self.assertEqual(config.max_output_bytes, 65536)

    def test_features_must_be_table(self):
        with self.assertRaises(ConfigError):
            self.load('features=[]\n' + Path('examples/config.toml').read_text().replace('[features]', '[unused]'))

    def test_explicit_m1_gates_do_not_open_unverified_features(self):
        text = Path('examples/config.toml').read_text().replace('file_operations_enabled = false', 'file_operations_enabled = true')
        config = self.load(text)
        self.assertTrue(config.file_operations_enabled)
        self.assertFalse(config.trash_enabled)

    def test_account_scope_must_be_nonempty(self):
        with self.assertRaises(ConfigError):
            self.load(Path('examples/config.toml').read_text().replace('name = "default"', 'name = ""'))

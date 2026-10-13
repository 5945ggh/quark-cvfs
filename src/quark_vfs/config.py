"""Explicit configuration without discovering or reading credentials."""
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    code = "CONFIG_INVALID"


@dataclass(frozen=True)
class Config:
    node_executable: Path
    official_entry: Path
    state_dir: Path
    cache_dir: Path
    expected_version: str | None = None
    timeout_seconds: float = 10
    max_output_bytes: int = 65536
    login_timeout_seconds: float = 180
    account: str = "default"
    trash_root_fid: str | None = None
    file_operations_enabled: bool = False
    trash_enabled: bool = False
    trash_root_parent_fid: str | None = None

    @property
    def official_root(self) -> Path:
        return self.official_entry.resolve().parent.parent


def load_config(path: str | Path) -> Config:
    try:
        with Path(path).open("rb") as stream:
            data = tomllib.load(stream)
        runtime, local = data["runtime"], data["local"]
        paths = []
        for section, key in ((runtime, "node_executable"), (runtime, "official_entry"),
                             (local, "state_dir"), (local, "cache_dir")):
            value = section[key]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ConfigError("Runtime and local paths must be absolute.")
            paths.append(Path(value))
        if paths[1].name != "quark-drive.cjs" or paths[1].parent.name != "scripts":
            raise ConfigError("Official entry must be scripts/quark-drive.cjs.")
        features = data.get("features", {})
        if not isinstance(features, dict):
            raise ConfigError("features must be a TOML table.")
        if any(type(value) is not bool for value in features.values()):
            raise ConfigError("Feature gates must be booleans.")
        if any(value is not False for key, value in features.items()
               if key not in ("file_operations_enabled", "trash_enabled")):
            raise ConfigError("Unverified feature gates must remain false.")
        account_table = data.get("account", {})
        if not isinstance(account_table, dict):
            raise ConfigError("account must be a TOML table.")
        trash_parent = account_table.get("trash_root_parent_fid")
        if trash_parent is not None and (not isinstance(trash_parent, str) or not trash_parent):
            raise ConfigError("trash_root_parent_fid must be nonempty text.")
        account = account_table.get("name", "default")
        trash_root = account_table.get("trash_root_fid")
        if not isinstance(account, str) or not account or len(account) > 128:
            raise ConfigError("An explicit nonempty account alias is required.")
        if trash_root is not None and (not isinstance(trash_root, str) or not trash_root):
            raise ConfigError("trash_root_fid must be nonempty text.")
        timeout = runtime.get("timeout_seconds", 10)
        limit = runtime.get("max_output_bytes", 65536)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ConfigError("timeout_seconds must be greater than zero and at most 60.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1024 <= limit <= 1048576:
            raise ConfigError("max_output_bytes must be between 1024 and 1048576.")
        version = runtime.get("expected_version")
        login_timeout = runtime.get("login_timeout_seconds", 180)
        if isinstance(login_timeout, bool) or not isinstance(login_timeout, (int, float)) or not 60 <= login_timeout <= 600:
            raise ConfigError("login_timeout_seconds must be between 60 and 600.")
        if version is not None and not isinstance(version, str):
            raise ConfigError("expected_version must be text.")
        return Config(paths[0], paths[1], paths[2], paths[3], version, timeout, limit, login_timeout,
                      account, trash_root, features.get("file_operations_enabled", False),
                      features.get("trash_enabled", False), trash_parent)
    except ConfigError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError("Cannot read a valid configuration; check the explicit TOML file.") from exc

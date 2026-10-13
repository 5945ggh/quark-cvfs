"""Bounded official runtime calls for diagnostics, authentication and synthetic M0 verification."""
import hashlib
import os
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..models import VfsError
from .protocol import ProtocolError, parse_agent_identity


class ProviderError(VfsError):
    def __init__(self, code: str, message: str, *, notices: tuple[str, ...] = ()):
        super().__init__(code, message)
        self.notices = notices
        self.upstream_code: int | None = None
        self.exit_code: int | None = None


@dataclass(frozen=True)
class OfficialResult:
    stdout: bytes
    exit_code: int
    interrupted_size: int | None = None


@dataclass(frozen=True)
class VerificationCall:
    """Internal finite request shape for the synthetic M0 workflow only."""
    action: str
    parent_fid: str | None = None
    fid: str | None = None
    target_fid: str | None = None
    name: str | None = None
    local_path: Path | None = None


@dataclass(frozen=True)
class ProviderCall:
    """Structured public-provider requests, with no raw argument escape hatch."""
    action: str
    parent_fid: str | None = None
    fids: tuple[str, ...] = ()
    target_fid: str | None = None
    name: str | None = None
    keyword: str | None = None
    local_path: Path | None = None
    batch_id: str | None = None


@dataclass(frozen=True)
class AcceptanceCall:
    """Additional finite commands exclusive to explicit M0 acceptance probes."""
    action: str
    fid: str | None = None
    record_id: str | None = None
    local_path: Path | None = None
    expected_name: str | None = None
    expected_size: int | None = None
    local_paths: tuple[Path, ...] = ()


def _acceptance_argv(call: AcceptanceCall) -> list[str]:
    if call.action == 'download-list':
        return ['download', 'list']
    if call.action == 'upload-list':
        return ['upload', 'list']
    if call.action == 'upload-mixed':
        if (not call.fid or call.fid.startswith('-') or '\x00' in call.fid or len(call.local_paths) != 2
                or any(not path.is_absolute() or not any(part.startswith('m0-acceptance-') for part in path.parts) for path in call.local_paths)
                or call.local_paths[0].parent != call.local_paths[1].parent):
            raise ProviderError('INVALID_ARGUMENT', 'Mixed probe requires exactly two run-owned absolute paths and an explicit parent.')
        return ['upload', *(str(path) for path in call.local_paths), '--parent-fid', call.fid]
    if call.local_path is None or not call.local_path.is_absolute():
        raise ProviderError('INVALID_ARGUMENT', 'Acceptance staging must be absolute.')
    if not any(part.startswith('m0-acceptance-') for part in call.local_path.parts):
        raise ProviderError('INVALID_ARGUMENT', 'Acceptance staging must belong to an M0 acceptance run.')
    if call.action == 'download-resume' and call.record_id and not call.record_id.startswith('-') and '\x00' not in call.record_id:
        return ['download', 'resume', '--record-id', call.record_id, '--output-dir', str(call.local_path)]
    if call.action == 'download-overwrite' and call.fid and not call.fid.startswith('-') and '\x00' not in call.fid and call.local_path.name == 'short-tail':
        return ['download', '--fid', call.fid, '--output-dir', str(call.local_path), '--overwrite']
    if (call.action == 'download-interrupt' and call.fid and not call.fid.startswith('-') and '\x00' not in call.fid
            and isinstance(call.expected_name, str) and call.expected_name
            and not any(c in call.expected_name for c in ('/', '\\', '\x00'))
            and type(call.expected_size) is int and 1024 <= call.expected_size <= 16 * 1024 * 1024):
        return ['download', '--fid', call.fid, '--output-dir', str(call.local_path)]
    raise ProviderError('COMMAND_NOT_ALLOWED', 'Unsupported acceptance request.')


def _provider_argv(call: ProviderCall) -> list[str]:
    identities = (*call.fids, call.parent_fid, call.target_fid)
    if any(fid is not None and (not isinstance(fid, str) or not fid or fid.startswith('-') or '\x00' in fid) for fid in identities):
        raise ProviderError('INVALID_ARGUMENT', 'File identities must be nonempty complete strings.')
    if call.local_path is not None and not call.local_path.is_absolute():
        raise ProviderError('INVALID_ARGUMENT', 'Local provider paths must be absolute.')
    if call.action == 'browse' and call.parent_fid is not None and call.local_path is not None:
        return ['browse', '--parent-fid', call.parent_fid, '--all', '--task-dir', str(call.local_path)]
    if call.action == 'create-folder' and call.parent_fid is not None and call.name is not None:
        if not call.name or call.name.startswith('-') or call.name in ('.', '..') or any(c in call.name for c in ('/', '\\', '\x00')):
            raise ProviderError('INVALID_ARGUMENT', 'Folder name must be one safe component.')
        return ['create-folder', '--dir-path', call.name, '--parent-fid', call.parent_fid]
    if call.action == 'upload' and call.parent_fid is not None and call.local_path is not None:
        return ['upload', str(call.local_path), '--parent-fid', call.parent_fid]
    if call.action == 'download' and len(call.fids) == 1 and call.local_path is not None:
        return ['download', '--fid', call.fids[0], '--output-dir', str(call.local_path)]
    if call.action == 'move' and 1 <= len(call.fids) <= 100 and call.target_fid is not None:
        return ['move', *call.fids, '--target-fid', call.target_fid]
    if call.action == 'search' and isinstance(call.keyword, str) and call.keyword and '\x00' not in call.keyword:
        args = ['search', '--keyword=' + call.keyword, '--stdout-only']
        if call.parent_fid is not None:
            args.extend(['--parent-fid', call.parent_fid])
        return args
    if call.action == 'rename' and call.local_path is not None and call.batch_id is not None:
        if len(call.batch_id) != 16 or any(c not in '0123456789abcdef' for c in call.batch_id):
            raise ProviderError('INVALID_ARGUMENT', 'Rename batch ID must contain exactly 16 lowercase hex characters.')
        return ['rename', '--batch-id', call.batch_id, '--items-file', str(call.local_path)]
    raise ProviderError('COMMAND_NOT_ALLOWED', 'Unsupported structured provider request.')


def _verification_argv(call: VerificationCall) -> list[str]:
    for fid in (call.parent_fid, call.fid, call.target_fid):
        if fid is not None and (not fid or fid.startswith("-") or "\x00" in fid):
            raise ProviderError("WORKFLOW_ARGUMENT_INVALID", "Invalid internal file identity.")
    if call.local_path is not None and not call.local_path.is_absolute():
        raise ProviderError("WORKFLOW_ARGUMENT_INVALID", "Internal local paths must be absolute.")
    if call.action == "browse" and call.parent_fid is not None and call.local_path is not None:
        return ["browse", "--parent-fid", call.parent_fid, "--all", "--task-dir", str(call.local_path)]
    if call.action == "create-folder" and call.parent_fid is not None and call.name is not None:
        if not call.name.startswith("QuarkVFS-M0-") or any(char in call.name for char in ("/", "\\", "\x00")):
            raise ProviderError("WORKFLOW_ARGUMENT_INVALID", "Only synthetic test directory names are allowed.")
        return ["create-folder", "--dir-path", call.name, "--parent-fid", call.parent_fid]
    if call.action == "upload" and call.parent_fid is not None and call.local_path is not None:
        return ["upload", str(call.local_path), "--parent-fid", call.parent_fid]
    if call.action == "download" and call.fid is not None and call.local_path is not None:
        return ["download", "--fid", call.fid, "--output-dir", str(call.local_path)]
    if call.action == "move" and call.fid is not None and call.target_fid is not None:
        return ["move", call.fid, "--target-fid", call.target_fid]
    raise ProviderError("COMMAND_NOT_ALLOWED", "Unsupported internal verification request.")


def in_repository(path: Path) -> bool:
    resolved = path.resolve()
    return any((parent / ".git").exists() for parent in (resolved, *resolved.parents))


def local_checks(config: Config) -> dict:
    root = config.official_root
    paths = {"runtime_root": root, "official_state": root / "codex",
             "working_directory": config.state_dir, "cache_directory": config.cache_dir}
    checks = {name: {"exists": path.is_dir(), "outside_repository": not in_repository(path),
                     "writable": path.is_dir() and os.access(path, os.W_OK | os.X_OK)}
              for name, path in paths.items()}
    checks["node"] = {"exists": config.node_executable.is_file(),
                      "executable": os.access(config.node_executable, os.X_OK)}
    checks["entry"] = {"exists": config.official_entry.is_file(),
                       "outside_repository": not in_repository(config.official_entry)}
    return checks


def validate_runtime(config: Config) -> dict:
    checks = local_checks(config)
    if not all(all(check.values()) for check in checks.values()):
        raise ProviderError("RUNTIME_UNSAFE", "Required runtime and private directories must exist, be writable where needed, and stay outside repositories.")
    return checks


def entry_fingerprint(config: Config) -> str | None:
    if not config.official_entry.is_file():
        return None
    digest = hashlib.sha256()
    with config.official_entry.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def build_argv(config: Config, command: str | VerificationCall | ProviderCall | AcceptanceCall) -> list[str]:
    if isinstance(command, AcceptanceCall):
        return [str(config.node_executable.resolve()), str(config.official_entry.resolve()), *_acceptance_argv(command)]
    if isinstance(command, ProviderCall):
        return [str(config.node_executable.resolve()), str(config.official_entry.resolve()), *_provider_argv(command)]
    if isinstance(command, VerificationCall):
        return [str(config.node_executable.resolve()), str(config.official_entry.resolve()), *_verification_argv(command)]
    if command not in ("resolve-agent", "login", "get-user-info"):
        raise ProviderError("COMMAND_NOT_ALLOWED", "Command is not available in the controlled adapter.")
    return [str(config.node_executable.resolve()), str(config.official_entry.resolve()), command]


def run_official(config: Config, command: str | VerificationCall | ProviderCall | AcceptanceCall, *, on_started: Callable[[], None] | None = None) -> OfficialResult:
    validate_runtime(config)
    argv = build_argv(config, command)
    watch = None
    if isinstance(command, AcceptanceCall) and command.action == 'download-interrupt':
        if command.local_path is None:
            raise ProviderError('INVALID_ARGUMENT', 'Missing acceptance staging.')
        handles = [os.open(command.local_path.anchor, os.O_RDONLY | os.O_DIRECTORY)]
        try:
            for part in command.local_path.parts[1:]:
                handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            if os.listdir(handles[-1]):
                raise ProviderError('UNSAFE_LOCAL_PATH', 'Interruption probe requires fresh empty staging.')
            watch = os.dup(handles[-1])
        except OSError:
            raise ProviderError('UNSAFE_LOCAL_PATH', 'Interruption staging must be a canonical private directory without links.') from None
        finally:
            for descriptor in reversed(handles):
                os.close(descriptor)
    try:
        process = subprocess.Popen(argv, cwd=config.state_dir,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, shell=False,
                               start_new_session=(os.name == "posix"))
    except BaseException:
        if watch is not None:
            os.close(watch)
        raise
    # Drain both pipes continuously, retaining a single bounded output budget.
    buffers = [bytearray(), bytearray()]
    exceeded = threading.Event()
    lock = threading.Lock()

    def stop() -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def drain(stream, index):
        while block := stream.read1(4096):
            with lock:
                available = config.max_output_bytes - sum(map(len, buffers))
                buffers[index].extend(block[:max(available, 0)])
                if len(block) > available:
                    exceeded.set()
                    stop()
                    break
        stream.close()

    threads = [threading.Thread(target=drain, args=(stream, i), daemon=True)
               for i, stream in enumerate((process.stdout, process.stderr))]
    for thread in threads:
        thread.start()
    interrupted_size = None
    try:
        if on_started is not None:
            on_started()
        timeout = config.login_timeout_seconds if command == "login" else (max(config.timeout_seconds, 120) if isinstance(command, (VerificationCall, ProviderCall, AcceptanceCall)) else config.timeout_seconds)
        if isinstance(command, AcceptanceCall) and command.action == 'download-interrupt':
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if command.local_path is None or command.expected_name is None or command.expected_size is None:
                    raise ProviderError('INVALID_ARGUMENT', 'Incomplete synthetic interruption request.')
                try:
                    metadata = os.stat(command.expected_name, dir_fd=watch, follow_symlinks=False)
                    if stat.S_ISREG(metadata.st_mode) and 0 < metadata.st_size < command.expected_size:
                        interrupted_size = metadata.st_size
                        stop()
                        process.wait()
                        break
                except FileNotFoundError:
                    pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                try:
                    process.wait(timeout=min(.01, remaining))
                except subprocess.TimeoutExpired:
                    pass
        else:
            process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop()
        process.wait()
        code = "PROBE_TIMEOUT" if command == "resolve-agent" else "RUNTIME_TIMEOUT"
        raise ProviderError(code, "Official runtime exceeded its timeout; authorization outcome is unknown.") from None
    finally:
        # Also terminate descendants holding a pipe after their parent exits.
        stop()
        for thread in threads:
            thread.join(timeout=1)
        if watch is not None:
            os.close(watch)
    if exceeded.is_set():
        raise ProviderError("OUTPUT_LIMIT", "Official runtime exceeded the diagnostic output limit.")
    return OfficialResult(bytes(buffers[0]), process.returncode, interrupted_size)


def resolve_agent(config: Config) -> str:
    output = run_official(config, "resolve-agent")
    if output.exit_code:
        raise ProviderError("PROBE_FAILED", "Official runtime identity probe failed; raw output is suppressed.")
    try:
        return parse_agent_identity(output.stdout.decode("utf-8", errors="strict"))
    except (ProtocolError, UnicodeError):
        raise ProviderError("AGENT_UNVERIFIED", "Probe did not report exactly one supported Codex identity.") from None

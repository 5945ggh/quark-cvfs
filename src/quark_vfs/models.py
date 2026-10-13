"""Shared file interface, including honest provider identity and outcome domains."""
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class VfsError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, job_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.job_id = job_id


def same_browse_identity(left: str, right: str) -> bool:
    """Only call for full identities from browse in one verified account scope."""
    left_tail, right_tail = left.rsplit('|', 1)[-1], right.rsplit('|', 1)[-1]
    if '|' in left and '|' in right and left_tail and right_tail:
        return left_tail == right_tail
    return left == right


@dataclass(frozen=True)
class ScopeBinding:
    account: str
    scope_uuid: str
    marker_fid: str
    marker_name: str
    marker_sha256: str
    marker_parent_fid: str | None = None
    marker_size: int | None = None


@dataclass(frozen=True)
class FileRef:
    account: str
    fid: str | None = None
    parent_fid_hint: str | None = None
    path: str | None = None

    def __post_init__(self):
        if not isinstance(self.account, str) or not self.account or (self.fid is None) == (self.path is None):
            raise VfsError("INVALID_REFERENCE", "Specify exactly one complete FID or absolute path and an account scope.")
        if self.fid is not None and (not isinstance(self.fid, str) or not self.fid or "\x00" in self.fid):
            raise VfsError("INVALID_REFERENCE", "FID must be nonempty.")
        if self.path is not None and (not self.path.startswith("/") or self.parent_fid_hint is not None):
            raise VfsError("INVALID_REFERENCE", "Paths must be absolute and cannot carry a parent hint.")


@dataclass(frozen=True)
class RemoteEntry:
    fid: str
    parent_fid: str | None
    name: str
    kind: str  # file or directory
    size: int | None = None
    modified: str | None = None
    content_hash: str | None = None
    observed_at: str | None = None
    raw_parent_fid: str | None = None

    def __post_init__(self):
        if (not self.fid or self.parent_fid == '' or not self.name or self.kind not in ("file", "directory")
                or (self.size is not None and (type(self.size) is not int or self.size < 0))):
            raise VfsError("UNKNOWN_SCHEMA", "Invalid remote entry metadata.")


@dataclass(frozen=True)
class Expected:
    name: str
    parent_fid: str | None
    kind: str
    size: int | None = None
    modified: str | None = None
    content_hash: str | None = None

    @classmethod
    def from_entry(cls, entry: RemoteEntry):
        return cls(entry.name, entry.parent_fid, entry.kind, entry.size, entry.modified, entry.content_hash)


@dataclass(frozen=True)
class UploadResult:
    native_id: str | None
    record_id: str | None
    size: int | None
    name: str
    outcome: str = "unknown"
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    name: str
    record_id: str | None = None
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class MutationResult:
    acknowledged: bool
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class Page:
    snapshot_id: str
    entries: tuple[RemoteEntry, ...]
    next_page_token: str | None
    complete: bool = True
    started_at: str | None = None
    finished_at: str | None = None
    account: str | None = None
    parent_fid: str | None = None


@dataclass(frozen=True)
class TransferReceipt:
    entry: RemoteEntry | None = None
    local_path: Path | None = None
    verification_levels: tuple[str, ...] = ()
    sha256: str | None = None
    record_id: str | None = None
    notices: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    job_id: str | None = None


@dataclass(frozen=True)
class BatchResult:
    job_id: str
    state: str
    items: tuple[dict, ...] = field(default_factory=tuple)


class Provider(Protocol):
    """Adapter seam. Scope verification must use a marker, never a nickname.

    list_directory is a full direct enumeration; parent_fid is query scope,
    with upstream opaque parent metadata preserved in raw_parent_fid.
    Mutations acknowledge business and exit status, not final observed identity.
    """
    def list_directory(self, account: str, parent_fid: str) -> Sequence[RemoteEntry]: ...
    def search(self, account: str, keyword: str, parent_fid: str | None = None) -> Sequence[RemoteEntry]: ...
    def mkdir(self, account: str, parent_fid: str, name: str) -> str: ...
    def upload_file(self, account: str, source: Path, parent_fid: str) -> UploadResult: ...
    def download_file(self, account: str, fid: str, staging: Path) -> DownloadResult: ...
    def move(self, account: str, fid: str, target_fid: str) -> MutationResult: ...
    def rename(self, account: str, fid: str, old_name: str, new_name: str) -> MutationResult: ...

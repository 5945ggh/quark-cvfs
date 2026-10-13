"""Conservative framing primitives; no unverified upstream success schema.

Decoders supplied to assess_outcome must implement a separately verified profile.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


def _positive_limit(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ProtocolError("INVALID_LIMIT", f"{name} must be a positive integer")


def _utf8_size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeError as error:
        raise ProtocolError("INVALID_JSON", "Protocol text is not valid UTF-8") from error


def parse_agent_identity(text: str) -> str:
    if text.strip() != "QK_AGENT_ID=codex":
        raise ProtocolError("AGENT_IDENTITY_INVALID", "Expected the Codex identity line")
    return "codex"


def _object(line: str, number: int) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(line, object_pairs_hook=pairs,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, RecursionError) as error:
        raise ProtocolError("INVALID_JSON", f"Invalid JSON at line {number}") from error
    if not isinstance(value, dict):
        raise ProtocolError("UNKNOWN_SCHEMA", f"Expected an object at line {number}")
    return value


@dataclass(frozen=True)
class ProtocolTranscript:
    events: tuple[dict, ...]

    def _of_type(self, name: str) -> tuple[dict, ...]:
        return tuple(event for event in self.events if event["type"] == name)

    @property
    def progress(self):
        return self._of_type("progress")

    @property
    def lists(self):
        return self._of_type("list")

    @property
    def results(self):
        return self._of_type("result")

    @property
    def artifacts(self):
        return self._of_type("artifact")

    @property
    def notices(self):
        return tuple(event["notice"] for event in self.events if event.get("notice"))


def parse_ndjson(text: str, *, max_bytes: int = 8 * 1024 * 1024,
                 max_line_bytes: int = 1024 * 1024) -> ProtocolTranscript:
    _positive_limit(max_bytes, "max_bytes")
    _positive_limit(max_line_bytes, "max_line_bytes")
    if _utf8_size(text) > max_bytes:
        raise ProtocolError("OUTPUT_LIMIT", "Protocol output exceeds byte limit")
    events: list[dict] = []
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        if _utf8_size(line) > max_line_bytes:
            raise ProtocolError("OUTPUT_LIMIT", "Protocol line exceeds byte limit")
        event = _object(line, number)
        if event.get("type") not in ("progress", "list", "result", "artifact"):
            raise ProtocolError("UNKNOWN_SCHEMA", f"Unknown event at line {number}")
        if event["type"] == "result" and any(e["type"] == "result" for e in events):
            raise ProtocolError("DUPLICATE_RESULT", "Multiple terminal results")
        events.append(event)
    return ProtocolTranscript(tuple(events))


ResultDecoder = Callable[[dict], bool | None]
ItemDecoder = Callable[[dict], Iterable[bool | None]]


def decode_static_code_status(event: dict) -> bool | None:
    """1.0.24 static observation only; not a real-account compatibility gate.

    Callers must additionally check command-specific completeness and counts.
    """
    if (type(event.get("code")) is not int or not isinstance(event.get("msg"), str)
            or event.get("action") not in ("browse", "upload")
            or not isinstance(event.get("data"), dict)):
        return None
    return event["code"] == 0


def assess_outcome(transcript: ProtocolTranscript, exit_code: int, *,
                   result_decoder: ResultDecoder | None = None,
                   item_decoder: ItemDecoder | None = None) -> Outcome:
    """Combine process, business and item evidence without guessing field meaning."""
    if type(exit_code) is not int:
        raise ProtocolError("INVALID_EXIT_CODE", "Exit code must be an integer")
    business = None
    if len(transcript.results) == 1 and result_decoder is not None:
        business = result_decoder(transcript.results[0])
    items: list[bool | None] = []
    for event in transcript.lists:
        items.extend(item_decoder(event) if item_decoder is not None else [None])
    if any(value is not None and type(value) is not bool for value in [business, *items]):
        raise ProtocolError("UNKNOWN_SCHEMA", "Status decoder must return boolean or unknown")
    succeeded = any(value is True for value in items)
    failed = exit_code != 0 or business is False or any(value is False for value in items)
    unknown = business is None or any(value is None for value in items)
    if failed:
        return Outcome.PARTIAL if succeeded else Outcome.FAILED
    if unknown:
        return Outcome.UNKNOWN
    return Outcome.SUCCESS


def read_artifact_jsonl(file_path: str | Path, *, allowed_output_dir: str | Path,
                        expected_count: int, required_fields: Iterable[str],
                        max_bytes: int = 64 * 1024 * 1024,
                        max_line_bytes: int = 1024 * 1024,
                        max_rows: int = 100_000) -> tuple[dict, ...]:
    """Read the full artifact through no-follow descriptors rooted in this call's directory.

    expected_count must come from a verified completeness schema, never preview size.
    Unsupported platforms fail closed rather than weaken symlink checks.
    """
    for name, value in (("max_bytes", max_bytes), ("max_line_bytes", max_line_bytes),
                        ("max_rows", max_rows)):
        _positive_limit(value, name)
    if isinstance(required_fields, (str, bytes)):
        raise ProtocolError("INVALID_FIELDS", "Required fields must be a collection of field names")
    try:
        fields = tuple(required_fields)
    except TypeError as error:
        raise ProtocolError("INVALID_FIELDS", "Required fields must be iterable") from error
    if any(type(field) is not str or not field for field in fields):
        raise ProtocolError("INVALID_FIELDS", "Required field names must be nonempty strings")
    if type(expected_count) is not int or not 0 <= expected_count <= max_rows:
        raise ProtocolError("INVALID_COUNT", "Expected count outside permitted range")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ProtocolError("PATH_CHECK_UNAVAILABLE", "Secure artifact opening unavailable")
    root = Path(os.path.abspath(allowed_output_dir))
    path = Path(file_path)
    path = path if path.is_absolute() else root / path
    if ".." in path.parts:
        raise ProtocolError("UNSAFE_ARTIFACT_PATH", "Parent traversal is forbidden")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ProtocolError("UNSAFE_ARTIFACT_PATH", "Artifact outside allowed output directory") from error
    if not relative.parts:
        raise ProtocolError("UNSAFE_ARTIFACT_PATH", "Artifact must be a file")
    descriptors = []
    try:
        # Walk from filesystem root: even a symlink in the permitted root is rejected.
        current = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(current)
        for part in (*root.parts[1:], *relative.parts[:-1]):
            current = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            descriptors.append(current)
        fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        descriptors.append(fd)
        import stat
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProtocolError("UNSAFE_ARTIFACT_PATH", "Artifact is not a regular file")
        rows: list[dict] = []
        total = 0
        with os.fdopen(os.dup(fd), "rb") as stream:
            while line := stream.readline(max_line_bytes + 1):
                total += len(line)
                if len(line) > max_line_bytes or total > max_bytes or len(rows) >= max_rows:
                    raise ProtocolError("OUTPUT_LIMIT", "Artifact exceeds read limits")
                try:
                    text = line.decode("utf-8")
                except UnicodeError as error:
                    raise ProtocolError("INVALID_JSON", "Artifact is not UTF-8") from error
                row = _object(text, len(rows) + 1)
                if any(field not in row or row[field] is None for field in fields):
                    raise ProtocolError("UNKNOWN_SCHEMA", "Artifact row lacks required fields")
                rows.append(row)
        if len(rows) != expected_count:
            raise ProtocolError("COUNT_MISMATCH", "Artifact count does not match expected total")
        return tuple(rows)
    except OSError as error:
        raise ProtocolError("ARTIFACT_READ_FAILED", "Cannot securely read artifact") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

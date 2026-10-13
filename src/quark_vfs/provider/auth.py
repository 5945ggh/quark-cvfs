"""Narrow authentication result decoding; never expose credential or account data."""
import re
from collections.abc import Callable

from ..config import Config
from .protocol import ProtocolError, parse_ndjson
from .quark_cli import OfficialResult, ProviderError, run_official


def sanitize_notices(values) -> tuple[str, ...]:
    notices = []
    for notice in values:
        if isinstance(notice, dict) and isinstance(notice.get("kind"), str) and isinstance(notice.get("text"), str):
            notice = notice["text"]
        if isinstance(notice, str):
            if re.search(r"(?i)bearer|token|authorization|secret|(?:[\"']?code[\"']?\s*[:=])", notice):
                notices.append("Upstream notice received; possible credential content withheld.")
                continue
            text = re.sub(r"https?://\S+", "[upstream link omitted]", notice)
            text = re.sub(r"(?i)(token|authorization|code|secret)\s*[:=]\s*\S+", r"\1=[redacted]", text)
            notices.append(text[:2048])
        else:
            notices.append("Upstream notice received in an unrecognized structured format; content withheld.")
    return tuple(notices)


def _result(output: OfficialResult, action: str) -> tuple[dict, tuple[str, ...]]:
    try:
        transcript = parse_ndjson(output.stdout.decode("utf-8", errors="strict"))
    except (ProtocolError, UnicodeError):
        raise ProviderError("AUTH_SCHEMA_UNKNOWN", "Official authentication response schema is unrecognized; outcome is unknown.") from None
    if len(transcript.results) != 1:
        raise ProviderError("AUTH_SCHEMA_UNKNOWN", "Official authentication result is missing; outcome is unknown.")
    result = transcript.results[0]
    safe_notices = sanitize_notices(transcript.notices)
    if type(result.get("code")) is not int:
        raise ProviderError("AUTH_SCHEMA_UNKNOWN", "Official authentication result schema is unrecognized; outcome is unknown.")
    if result["code"] == -1408 and result.get("action") in (action, "not_authenticated"):
        raise ProviderError("AUTH_REQUIRED", "Official runtime reports that browser authorization is required.", notices=safe_notices)
    if result.get("action") != action:
        raise ProviderError("AUTH_SCHEMA_UNKNOWN", "Official authentication result action is unrecognized; outcome is unknown.", notices=safe_notices)
    if action == "login" and result["code"] == -118:
        raise ProviderError("AUTH_ALREADY_VALID", "Official runtime reports an existing valid authorization; use status --refresh instead of repeating login.", notices=safe_notices)
    if result["code"] != 0:
        raise ProviderError("AUTH_NOT_CONFIRMED", "Official authentication request was not successful; authentication state remains unknown.", notices=safe_notices)
    if output.exit_code != 0:
        raise ProviderError("RUNTIME_FAILED", "Official process failed after its business result; outcome is unknown.", notices=safe_notices)
    if not isinstance(result.get("data"), dict):
        raise ProviderError("AUTH_SCHEMA_UNKNOWN", "Official authentication data schema is unrecognized; outcome is unknown.", notices=safe_notices)
    return result["data"], safe_notices


def login(config: Config, *, on_started: Callable[[], None] | None = None) -> dict:
    data, notices = _result(run_official(config, "login", on_started=on_started), "login")
    if data.get("status") not in ("confirmed", "second_confirmed", "install_confirmed"):
        raise ProviderError("AUTH_NOT_CONFIRMED", "Browser authorization was not confirmed; refresh status to check the current state.", notices=notices)
    return {"authentication": "authenticated", "authorization_scope": "unknown",
            "login_state": "confirmed", "account_info_refreshed": False, "upstream_notices": list(notices)}


def refresh_status(config: Config) -> dict:
    data, notices = _result(run_official(config, "get-user-info"), "get-user-info")
    user = data.get("userInfo")
    vip = data.get("vipInfo")
    if ("userInfo" not in data or "vipInfo" not in data
            or (user is not None and (not isinstance(user, dict) or not isinstance(user.get("nickname"), str)))
            or (vip is not None and not isinstance(vip, dict))):
        raise ProviderError("AUTH_SCHEMA_UNKNOWN", "Account response schema is unrecognized; authentication state remains unknown.", notices=notices)
    return {"authentication": "authenticated", "authorization_scope": "unknown",
            "account_info_refreshed": True, "upstream_notices": list(notices)}

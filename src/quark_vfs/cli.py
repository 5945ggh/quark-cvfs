"""JSON command interface with explicit tree text output."""
import argparse
import json
import uuid
from typing import Any, TypedDict

from . import __version__
from .config import ConfigError, load_config
from .core.trash import TrashError
from .models import VfsError
from .provider.auth import login, refresh_status
from .provider.quark_cli import (
    ProviderError,
    entry_fingerprint,
    local_checks,
    resolve_agent,
)
from .verification import VerificationError, verify_workflow


class Envelope(TypedDict, total=False):
    ok: bool
    operation_id: str
    data: dict[str, Any]
    warnings: list[str]
    error: dict[str, Any]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProviderError("INVALID_ARGUMENT", "Invalid command arguments; use --help for supported options.")


def main(argv=None) -> int:
    result: Envelope = {"ok": True, "operation_id": str(uuid.uuid4()), "data": {}, "warnings": []}
    output_format = 'json'
    try:
        parser = Parser(description="M0 local diagnostics for the official Quark runtime")
        parser.add_argument("--config", help="Explicit TOML configuration path")
        commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
        commands.add_parser("version")
        doctor = commands.add_parser("doctor")
        doctor.add_argument("--probe", action="store_true", help="Execute official resolve-agent with the real inherited environment; upstream initialization and network telemetry may occur")
        status = commands.add_parser("status")
        status.add_argument("--refresh", action="store_true", help="Query official account status over the network; account details are suppressed")
        status.add_argument("--verify-scope", action="store_true", help="Read the bound cloud marker without changing the binding")
        auth = commands.add_parser("auth")
        auth.add_subparsers(dest="auth_command", required=True, parser_class=Parser).add_parser("login", help="Start official browser authorization and wait for its confirmation")
        verification = commands.add_parser("verify-workflow", help="Run a synthetic document round trip in new UUID cloud directories; preserve evidence and stop on uncertain outcomes")
        verification.add_argument("--resume", help="Resume only a recorded successful-source-upload M0 checkpoint by canonical UUID; never repeats its upload")
        _add_file_commands(commands)
        args = parser.parse_args(argv)
        if args.command == 'tree':
            output_format = args.format
        if args.command == "version":
            result["data"] = {"wrapper_version": __version__, "protocol_version": 1}
        else:
            if not args.config:
                raise ProviderError("CONFIG_REQUIRED", "Provide --config /absolute/path/config.toml; no runtime is discovered automatically.")
            config = load_config(args.config)
            if args.command in _FILE_COMMANDS:
                result["data"] = _jsonable(_file_dispatch(args, config))
                data = result["data"]
                if args.command == 'tree' and not data['complete_for_depth']:
                    result['ok'] = False
                    result['error'] = {'code': 'TREE_INCOMPLETE', 'message': 'Requested depth is incomplete because a budget was reached or a directory branch could not be listed.', 'retryable': False}
                elif not (args.command == "jobs" and not args.resume) and isinstance(data, dict) and data.get("state") in ("partial", "failed", "needs_reconciliation"):
                    result["ok"] = False
                    error_code = "OUTCOME_UNKNOWN" if data["state"] == "needs_reconciliation" else "PARTIAL_FAILURE"
                    result["error"] = {"code": error_code, "message": "Operation did not fully succeed; preserve successful items and reconcile recorded outcomes.", "retryable": False}
                _print_result(result, output_format)
                return 0 if result["ok"] else 1
            result["data"] = {
                "wrapper_version": __version__, "protocol_version": 1,
                "checks": local_checks(config),
                "expected_official_version": config.expected_version,
                "observed_official_version": None,
                "entry_sha256": entry_fingerprint(config),
                "authentication": "unknown", "authorization_scope": "unknown",
                "agent_identity": "unverified", "tasks": "unknown",
                "account": config.account,
                "local_namespace": _local_namespace_status(config),
                "features": {"file_operations_enabled": config.file_operations_enabled,
                             "trash_enabled": config.trash_enabled, "mcp_verified": False, "archive_replace_verified": False,
                             "download_resume_verified": False},
            }
            result["warnings"].append("Independent integration terms, authorization scope and unopened capability gates remain unverified.")
            ready = all(all(check.values()) for check in result["data"]["checks"].values())
            result["data"]["local_runtime_ready"] = ready
            if args.command == "doctor" and not ready:
                raise ProviderError("RUNTIME_UNSAFE", "Local runtime checks failed; inspect data.checks and configure safe existing directories.")
            if args.command == "doctor" and args.probe:
                result["warnings"].append("Official runtime initialization may access its own credentials and send upstream network telemetry.")
                result["data"]["agent_identity"] = resolve_agent(config)
                result["data"]["identity_probe"] = "passed"
            if args.command == "status" and args.refresh:
                result["warnings"].append("Official account status refresh contacts upstream services; raw account details are suppressed.")
                try:
                    result['data'].update(refresh_status(config))
                except (VfsError, OSError) as error:
                    result['ok'] = False
                    result['error'] = _safe_error(error, stage='authentication_refresh')
                    result['data']['authentication_error'] = result['error']
            if args.command == 'status' and args.verify_scope:
                namespace = result['data']['local_namespace']
                try:
                    _verify_namespace(config)
                    namespace.update(live_marker_checked=True, scope_verification='passed')
                except (VfsError, OSError) as error:
                    mismatch = getattr(error, 'code', None) in ('SCOPE_MISMATCH', 'SCOPE_MARKER_MISSING')
                    namespace.update(live_marker_checked=mismatch, scope_verification='mismatch' if mismatch else 'unavailable', verification_error=_safe_error(error, stage='scope_binding'))
                    result['ok'] = False
                    result.setdefault('error', namespace['verification_error'])
            if args.command == "auth":
                result["warnings"].append("Official runtime attempts to open its authorization page; complete authorization in your browser. Credentials remain managed by the official runtime.")

                def started() -> None:
                    event: Envelope = {"ok": True, "operation_id": result["operation_id"],
                                       "data": {"event": "login_started", "login_state": "waiting_for_browser",
                                                "browser_authorization_required": True, "browser_opened": "unknown"},
                                       "warnings": ["Login process started; browser opening and authorization are not yet confirmed."]}
                    print(json.dumps(event, ensure_ascii=False), flush=True)

                result["data"].update(login(config, on_started=started))
            if args.command == "verify-workflow":
                result["data"].update(verify_workflow(config, resume=args.resume))
    except (ConfigError, ProviderError, VfsError, TrashError) as exc:
        result["ok"] = False
        result["error"] = _safe_error(exc)
        for field in ("job_id", "operation_id", "scope_uuid"):
            value = getattr(exc, field, None)
            if isinstance(value, str):
                result["data"][field] = value
        if isinstance(exc, ProviderError) and exc.notices:
            result["data"]["upstream_notices"] = list(exc.notices)
        if isinstance(exc, VerificationError):
            result["data"].update(exc.summary)
    except OSError:
        result["ok"] = False
        result["error"] = {"code": "LOCAL_IO_ERROR", "message": "A local runtime or filesystem check failed; raw system details are suppressed.", "retryable": False}
    _print_result(result, output_format)
    return 0 if result["ok"] else 1


def _terminal_safe(value: str) -> str:
    import unicodedata
    escaped = {'\n': r'\n', '\r': r'\r', '\t': r'\t', '\\': r'\\'}
    return ''.join(escaped.get(char, f'\\u{ord(char):04x}' if unicodedata.category(char).startswith('C') or char in '\u2028\u2029' else char)
                   for char in value)


def _print_result(result: Envelope, output_format: str) -> None:
    if output_format == 'json':
        print(json.dumps(result, ensure_ascii=False))
        return
    data = result.get('data', {})
    if 'root' in data:
        def render(node, prefix='', connector=''):
            label = _terminal_safe(node['name'])
            if node['kind'] == 'directory' and not label.endswith('/'):
                label += '/'
            status = node.get('listing_status')
            if status and status != 'complete':
                label += f' [{status}]'
            if node.get('error'):
                error = node['error']
                label += ' ' + _terminal_safe(f"{error['code']}: {error['message']}")
            if 'omitted_entries' in node:
                label += f" [omitted_entries={node['omitted_entries']}]"
            print(prefix + connector + label)
            children = node.get('children', [])
            child_prefix = prefix + ('    ' if connector == '└── ' else '│   ' if connector else '')
            for index, child in enumerate(children):
                render(child, child_prefix, '└── ' if index == len(children) - 1 else '├── ')
        render(data['root'])
        limits, stats = data['limits'], data['stats']
        print(f"depth={data['requested_depth']} max_directories={limits['max_directories']} max_entries={limits['max_entries']}")
        print('统计（仅显示范围，非全盘）：' + ' '.join(f'{key}={value}' for key, value in stats.items()))
        print('一致性：每个目录独立快照；多目录结果非原子快照。')
    if result.get('error'):
        error = result['error']
        print(_terminal_safe(f"Error {error['code']}: {error['message']}"))


def _bounded_int(minimum: int, maximum: int):
    def parse(value: str) -> int:
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f'Expected an integer from {minimum} to {maximum}.')
        return number
    return parse


def _jsonable(value):
    from dataclasses import asdict, is_dataclass
    from pathlib import Path
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _ref(value: str, account: str):
    from .models import FileRef, VfsError
    try:
        fields = json.loads(value)
        if not isinstance(fields, dict) or set(fields) - {'fid', 'path', 'parent_fid_hint'}:
            raise ValueError
        if any(item is not None and (not isinstance(item, str) or not item)
               for item in fields.values()):
            raise ValueError
        return FileRef(account=account, **fields)
    except (ValueError, TypeError):
        raise VfsError('INVALID_REFERENCE', 'Use a JSON reference with fid or absolute path and optional parent_fid_hint.') from None


def _add_file_commands(commands):
    init = commands.add_parser('init-scope', help='Create and verify a project namespace marker before binding a local account alias')
    init.add_argument('--parent-fid', required=True)
    binding = commands.add_parser('bind-scope', help='Verify an existing project marker after local database loss')
    binding.add_argument('--scope-uuid', required=True)
    binding.add_argument('--marker-fid')
    binding.add_argument('--management-root-fid')
    binding.add_argument('--management-parent-fid', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--ref', required=True)
    listing.add_argument('--page-size', type=int, default=100)
    listing.add_argument('--page-token')
    tree = commands.add_parser('tree', help='Read a bounded directory tree; directories at the requested depth remain unexpanded')
    tree.add_argument('--ref', required=True, help='JSON file reference with fid or absolute path')
    tree.add_argument('--depth', type=_bounded_int(0, 32), default=2, help='Requested depth from 0 to 32 (default: 2)')
    tree.add_argument('--max-directories', type=_bounded_int(1, 1000), default=100, help='Directory listing budget from 1 to 1000 (default: 100)')
    tree.add_argument('--max-entries', type=_bounded_int(1, 10000), default=1000, help='Displayed entry budget from 1 to 10000 (default: 1000)')
    tree.add_argument('--format', choices=('json', 'text'), default='json', help='Output format (default: json)')
    stat = commands.add_parser('stat')
    stat.add_argument('--ref', required=True)
    stat.add_argument('--cached', action='store_true')
    search = commands.add_parser('search')
    search.add_argument('--keyword', required=True)
    search.add_argument('--parent-fid')
    mkdir = commands.add_parser('mkdir')
    mkdir.add_argument('--parent', required=True)
    mkdir.add_argument('--name', required=True)
    upload = commands.add_parser('upload')
    upload.add_argument('--parent', required=True)
    upload.add_argument('--local-path', action='append', required=True)
    upload.add_argument('--include-secrets', action='store_true', help='Explicitly include secret-name patterns; configured private state/cache/runtime roots remain excluded')
    download = commands.add_parser('download')
    download.add_argument('--ref', required=True)
    download.add_argument('--local-target', required=True)
    move = commands.add_parser('move')
    move.add_argument('--ref', action='append', required=True)
    move.add_argument('--target', required=True)
    move.add_argument('--expected', help='JSON Expected array matching the input references')
    rename = commands.add_parser('rename')
    rename.add_argument('--ref', required=True)
    rename.add_argument('--name', required=True)
    rename.add_argument('--expected', help='JSON observed Expected metadata')
    trash = commands.add_parser('trash')
    trash.add_argument('--ref', action='append', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--operation-id', required=True)
    restore.add_argument('--entry-id', required=True)
    destinations = restore.add_mutually_exclusive_group()
    destinations.add_argument('--destination-fid')
    destinations.add_argument('--destination', help='Explicit JSON directory reference, with reliable parent hint when needed')
    commands.add_parser('rebuild-trash')
    jobs = commands.add_parser('jobs')
    jobs.add_argument('--job-id')
    jobs.add_argument('--resume', action='store_true')
    for command in (mkdir, upload, download, move, rename, restore):
        command.add_argument('--conflict', choices=('fail', 'keep_both'), default='fail')


_FILE_COMMANDS = {'init-scope', 'bind-scope', 'list', 'tree', 'stat', 'search', 'mkdir', 'upload', 'download',
                  'move', 'rename', 'trash', 'restore', 'jobs', 'rebuild-trash'}


def _init_scope(config, provider, store, parent_fid):
    """Bootstrap only a new namespace; existing bindings are never silently replaced."""
    import hashlib
    from pathlib import Path

    from .core.catalog import secure_bytes
    from .models import ScopeBinding, VfsError
    account = config.account
    try:
        store.binding(account)
    except VfsError as error:
        if error.code != 'ACCOUNT_UNBOUND':
            raise
    else:
        raise VfsError('SCOPE_ALREADY_BOUND', 'Use the existing marker or configure a new alias; automatic rebinding is forbidden.')
    if any(row.get('state') != 'bound' for row in store.list_documents(account, 'bootstrap')):
        raise VfsError('RECONCILIATION_REQUIRED', 'A previous namespace initialization is unfinished; reconcile its recorded objects before starting another.')
    scope_uuid = str(uuid.uuid4())
    name = 'QuarkVFS-' + scope_uuid
    prior = provider.list_directory(account, parent_fid)
    if any(row.name == name for row in prior):
        raise VfsError('NAME_CONFLICT', 'Random namespace name already exists.')
    bootstrap = {'scope_uuid': scope_uuid, 'parent_fid': parent_fid, 'name': name, 'state': 'mkdir_pending'}
    store.put_document(account, 'bootstrap', scope_uuid, bootstrap)
    response_id = provider.mkdir(account, parent_fid, name)
    bootstrap.update(state='mkdir_response_observed', response_id=response_id)
    store.put_document(account, 'bootstrap', scope_uuid, bootstrap)
    root_rows = _bootstrap_entries(provider, account, parent_fid,
                                   lambda rows: len([row for row in rows if row.name == name and row.kind == 'directory']) == 1)
    matches = [row for row in root_rows
               if row.name == name and row.kind == 'directory']
    if len(matches) != 1 or provider.list_directory(account, matches[0].fid):
        raise VfsError('OUTCOME_UNKNOWN', 'New namespace is not uniquely empty; do not retry initialization blindly.')
    root = matches[0].fid
    directory = config.cache_dir / 'init' / scope_uuid
    directory.mkdir(parents=True, mode=0o700)
    marker = directory / ('scope-' + scope_uuid + '.json')
    data = json.dumps({'schema_version': 1, 'kind': 'scope_marker', 'account': account,
                       'scope_uuid': scope_uuid}, sort_keys=True).encode()
    marker.write_bytes(data)
    bootstrap.update(state='marker_upload_pending', management_root_fid=root)
    store.put_document(account, 'bootstrap', scope_uuid, bootstrap)
    uploaded = provider.upload_file(account, marker, root)
    bootstrap.update(state='marker_upload_response_observed', upload_outcome=uploaded.outcome,
                     native_id=uploaded.native_id, record_id=uploaded.record_id)
    store.put_document(account, 'bootstrap', scope_uuid, bootstrap)
    if uploaded.outcome != 'success':
        raise VfsError('OUTCOME_UNKNOWN', 'Scope marker upload is uncertain; no local binding was created.')
    rows = _bootstrap_entries(provider, account, root,
                              lambda entries: len([row for row in entries if row.name == marker.name
                                                   and row.kind == 'file' and row.size == len(data)]) == 1)
    candidates = [row for row in rows if row.name == marker.name and row.kind == 'file' and row.size == len(data)]
    if len(candidates) != 1:
        raise VfsError('OUTCOME_UNKNOWN', 'Scope marker cannot be uniquely observed.')
    output = directory / 'readback'
    output.mkdir(mode=0o700)
    result = provider.download_file(account, candidates[0].fid, output)
    observed, _ = secure_bytes(Path(result.path), output, max_bytes=65536)
    if observed != data:
        raise VfsError('SCOPE_MISMATCH', 'Scope marker exact readback failed.')
    binding = ScopeBinding(account, scope_uuid, candidates[0].fid, marker.name, hashlib.sha256(data).hexdigest(), root, len(data))
    store.bind(binding)
    from datetime import UTC, datetime
    observed_at = datetime.now(UTC).isoformat()
    store.save_snapshot(account, parent_fid, root_rows, observed_at, observed_at)
    store.save_snapshot(account, root, tuple(rows), observed_at, observed_at)
    store.put_document(account, 'management', 'root', {'fid': root, 'name': name, 'parent_fid': parent_fid})
    bootstrap.update(state='bound', marker_fid=candidates[0].fid)
    store.put_document(account, 'bootstrap', scope_uuid, bootstrap)
    return {'account': account, 'scope_uuid': scope_uuid, 'management_root_fid': root,
            'namespace_verified': True, 'upstream_account_id': 'unknown'}


def _file_dispatch(args, config):
    from pathlib import Path

    from .core.catalog import Catalog
    from .models import VfsError
    from .provider.quark import QuarkProvider
    from .storage import Store
    if args.command == 'jobs':
        if args.resume and not args.job_id:
            raise VfsError('INVALID_ARGUMENT', 'jobs --resume requires --job-id.')
        if not args.resume:
            from .storage import read_local_jobs
            return read_local_jobs(config.state_dir / 'quark-vfs.sqlite3', config.account, args.job_id)
    if not config.file_operations_enabled:
        raise VfsError('CAPABILITY_UNVERIFIED', 'Enable file_operations_enabled explicitly after the required verification.')
    tree_ref = _ref(args.ref, config.account) if args.command == 'tree' else None
    store = Store(config.state_dir / 'quark-vfs.sqlite3')
    provider = QuarkProvider(config)
    try:
        with store.account_lock(config.account):
            if args.command == 'init-scope':
                # Explicit parent argument is passed independently of immutable configuration.
                return _init_scope(config, provider, store, args.parent_fid)
            if args.command == 'bind-scope':
                return _bind_scope(config, provider, store, args)
            catalog = Catalog(provider, store, config.cache_dir / 'core')
            if args.command == 'tree':
                return catalog.tree(tree_ref, depth=args.depth,
                                    max_directories=args.max_directories, max_entries=args.max_entries)
            catalog.assert_scope(config.account)
            if config.trash_root_fid:
                from .models import FileRef
                trash_root = catalog.stat(FileRef(config.account, fid=config.trash_root_fid,
                                                  parent_fid_hint=config.trash_root_parent_fid), fresh=True)
                if trash_root.kind != 'directory':
                    raise VfsError('NOT_DIRECTORY', 'Configured trash root must be a fresh directory.')
                import hashlib
                key = 'trash_root:' + hashlib.sha256(trash_root.fid.encode()).hexdigest()
                store.put_document(config.account, 'management', key, {'fid': trash_root.fid})
            if args.command == 'list':
                return catalog.list(_ref(args.ref, config.account), page_size=args.page_size, page_token=args.page_token)
            if args.command == 'stat':
                reference = _ref(args.ref, config.account)
                cached = store.cached_entry(config.account, reference.fid) if args.cached and reference.fid else None
                entry = catalog.stat(reference, fresh=not args.cached)
                return {'entry': entry, 'fresh_requested': not args.cached,
                        'metadata_freshness': 'cached' if cached is not None else 'fresh'}
            if args.command == 'search':
                return catalog.search(config.account, args.keyword, args.parent_fid)
            from .core.operations import Operations
            from .core.transfers import Transfers
            operations = Operations(catalog, store)
            transfers = Transfers(catalog, store, config.cache_dir / 'transfers',
                                  excluded_local_roots=(config.state_dir, config.cache_dir, config.official_root))
            if args.command == 'jobs':
                if not args.job_id:
                    raise VfsError('INVALID_ARGUMENT', 'jobs --resume requires --job-id.')
                job = store.job(config.account, args.job_id)
                if job['action'] == 'download':
                    return transfers.resume_download(config.account, args.job_id)
                if job['action'] == 'upload':
                    return transfers.resume_upload(config.account, args.job_id)
                if job['action'] in ('mkdir', 'rename', 'move'):
                    return operations.resume(config.account, args.job_id)
                raise VfsError('RESUME_UNAVAILABLE', 'No constrained resume dispatcher exists for this recorded action.')
            if args.command == 'mkdir':
                return operations.mkdir(_ref(args.parent, config.account), args.name, conflict=args.conflict)
            if args.command == 'upload':
                return transfers.upload([Path(path) for path in args.local_path], _ref(args.parent, config.account), conflict=args.conflict,
                                        exclude_secrets=not args.include_secrets)
            if args.command == 'download':
                return transfers.download(_ref(args.ref, config.account), Path(args.local_target), conflict=args.conflict)
            if args.command == 'move':
                references = [_ref(ref, config.account) for ref in args.ref]
                expected = None
                if args.expected is not None:
                    try:
                        planned = json.loads(args.expected)
                        if not isinstance(planned, list) or len(planned) != len(references):
                            raise ValueError
                        expected = {}
                        for reference, metadata in zip(references, planned, strict=True):
                            fid = reference.fid or catalog.resolve(reference).fid
                            expected[fid] = _expected(metadata)
                    except (ValueError, TypeError):
                        raise VfsError('INVALID_ARGUMENT', 'Expected must be an array matching the move references.') from None
                return operations.move(references, _ref(args.target, config.account), conflict=args.conflict, expected=expected)
            if args.command == 'rename':
                try:
                    metadata = _expected(json.loads(args.expected)) if args.expected is not None else None
                except (ValueError, TypeError):
                    raise VfsError('INVALID_ARGUMENT', 'Expected metadata must be valid JSON.') from None
                return operations.rename(_ref(args.ref, config.account), args.name, conflict=args.conflict, expected=metadata)
            if not config.trash_enabled:
                raise VfsError('CAPABILITY_UNVERIFIED', 'Trash recovery must be explicitly enabled after verification.')
            from .core.trash import CoreTrashBackend, StoreTrashJournal, TrashService
            root = config.trash_root_fid
            if not root:
                management = store.get_document(config.account, 'management', 'root')
                if not management:
                    raise VfsError('TRASH_NOT_CONFIGURED', 'Specify a verified trash root or initialize a namespace.')
                root = management['fid']
            service = TrashService(CoreTrashBackend(catalog, operations, transfers, config.account,
                                                   config.cache_dir / 'trash'),
                                   StoreTrashJournal(store), account=config.account, trash_root_fid=root,
                                   scope_uuid=store.binding(config.account).scope_uuid)
            if args.command == 'trash':
                from dataclasses import asdict
                sources = []
                for ref in args.ref:
                    entry = catalog.stat(_ref(ref, config.account), fresh=True)
                    row = asdict(entry)
                    row['is_dir'] = entry.kind == 'directory'
                    trusted = store.get_document(config.account, 'trusted_hash', entry.fid)
                    if trusted and all(trusted.get('entry', {}).get(key) == row.get(key)
                                       for key in ('name', 'kind', 'size', 'parent_fid', 'modified', 'content_hash')):
                        row['trusted_sha256'] = trusted.get('sha256')
                    sources.append(row)
                return service.trash(sources)
            if args.command == 'restore':
                destination = args.destination_fid
                if args.destination is not None:
                    target = catalog.resolve(_ref(args.destination, config.account))
                    if target.kind != 'directory':
                        raise VfsError('NOT_DIRECTORY', 'Recovery destination must be a fresh directory.')
                    destination = target.fid
                return service.restore(args.operation_id, args.entry_id,
                                       destination=destination, conflict=args.conflict)
            return {'operations': service.rebuild()}
    except (VfsError, TrashError) as error:
        if 'service' in locals() and getattr(service, 'active_operation_id', None):
            error.operation_id = service.active_operation_id
        bootstrap = store.list_documents(config.account, 'bootstrap')
        pending = [row for row in bootstrap if row.get('state') != 'bound']
        if len(pending) == 1:
            error.scope_uuid = pending[0]['scope_uuid']
        raise
    finally:
        store.close()


def _bind_scope(config, provider, store, args):
    import hashlib

    from .core.catalog import secure_bytes
    from .models import ScopeBinding, VfsError
    try:
        identifier = str(uuid.UUID(args.scope_uuid))
    except ValueError:
        raise VfsError('INVALID_ARGUMENT', 'Scope UUID must be canonical.') from None
    if identifier != args.scope_uuid:
        raise VfsError('INVALID_ARGUMENT', 'Scope UUID must be canonical.')
    try:
        store.binding(config.account)
    except VfsError as error:
        if error.code != 'ACCOUNT_UNBOUND':
            raise
    else:
        raise VfsError('SCOPE_ALREADY_BOUND', 'Existing aliases cannot be rebound.')
    name = 'scope-' + identifier + '.json'
    expected = json.dumps({'schema_version': 1, 'kind': 'scope_marker', 'account': config.account,
                           'scope_uuid': identifier}, sort_keys=True).encode()
    parent_rows = tuple(provider.list_directory(config.account, args.management_parent_fid))
    from .core.catalog import same_browse_identity
    roots = [row for row in parent_rows if row.kind == 'directory'
             and row.name == 'QuarkVFS-' + identifier]
    if len(roots) != 1 or (args.management_root_fid is not None
                          and not same_browse_identity(roots[0].fid, args.management_root_fid)):
        raise VfsError('SCOPE_MISMATCH', 'Namespace directory is not unique in the explicit parent.')
    root_fid = roots[0].fid
    rows = tuple(provider.list_directory(config.account, root_fid))
    candidates = [row for row in rows
                  if row.name == name and row.kind == 'file' and row.size == len(expected)]
    if len(candidates) != 1 or (args.marker_fid is not None
                                and not same_browse_identity(candidates[0].fid, args.marker_fid)):
        raise VfsError('SCOPE_MISMATCH', 'Explicit marker does not match the fresh namespace observation.')
    directory = config.cache_dir / 'bind' / str(uuid.uuid4())
    directory.mkdir(parents=True, mode=0o700)
    result = provider.download_file(config.account, candidates[0].fid, directory)
    observed, _ = secure_bytes(result.path, directory, max_bytes=65536)
    if observed != expected:
        raise VfsError('SCOPE_MISMATCH', 'Namespace marker content differs.')
    store.bind(ScopeBinding(config.account, identifier, candidates[0].fid, name,
                            hashlib.sha256(expected).hexdigest(), root_fid, len(expected)))
    from datetime import UTC, datetime
    observed_at = datetime.now(UTC).isoformat()
    store.save_snapshot(config.account, args.management_parent_fid, parent_rows, observed_at, observed_at)
    store.save_snapshot(config.account, root_fid, rows, observed_at, observed_at)
    store.put_document(config.account, 'management', 'root', {'fid': root_fid,
                                                            'parent_fid': args.management_parent_fid})
    bootstrap = store.get_document(config.account, 'bootstrap', identifier)
    if bootstrap is not None:
        bootstrap.update(state='bound', marker_fid=candidates[0].fid)
        store.put_document(config.account, 'bootstrap', identifier, bootstrap)
    return {'account': config.account, 'scope_uuid': identifier, 'namespace_verified': True}


def _expected(value):
    from .models import Expected, VfsError
    required = {'name', 'parent_fid', 'kind'}
    optional = {'size', 'modified', 'content_hash'}
    if (not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional
            or any(not isinstance(value[key], str) or not value[key] for key in required)
            or value['kind'] not in ('file', 'directory')
            or (value.get('size') is not None and (type(value['size']) is not int or value['size'] < 0))
            or any(value.get(key) is not None and not isinstance(value[key], str) for key in ('modified', 'content_hash'))):
        raise VfsError('INVALID_ARGUMENT', 'Expected metadata has unknown fields or invalid types.')
    return Expected(**value)


def _local_namespace_status(config):
    import sqlite3
    from urllib.parse import quote
    path = config.state_dir / 'quark-vfs.sqlite3'
    if not path.exists():
        return {'binding': 'unbound', 'upstream_account_id': 'unknown', 'live_marker_checked': False, 'scope_verification': 'not_checked'}
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        return {'binding': 'unknown', 'upstream_account_id': 'unknown', 'live_marker_checked': False, 'scope_verification': 'not_checked'}
    try:
        database = sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True)
        try:
            row = database.execute('SELECT data FROM bindings WHERE account=?', (config.account,)).fetchone()
        finally:
            database.close()
        if row is None:
            return {'binding': 'unbound', 'upstream_account_id': 'unknown', 'live_marker_checked': False, 'scope_verification': 'not_checked'}
        binding = json.loads(row[0])
        identifier = str(uuid.UUID(binding['scope_uuid']))
        if binding['account'] != config.account or identifier != binding['scope_uuid']:
            raise ValueError
        return {'binding': 'bound', 'scope_uuid': identifier, 'live_marker_checked': False, 'scope_verification': 'not_checked',
                'marker_fid': binding.get('marker_fid'), 'marker_name': binding.get('marker_name'),
                'management_root_fid': binding.get('marker_parent_fid'),
                'upstream_account_id': 'unknown'}
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        return {'binding': 'unknown', 'upstream_account_id': 'unknown', 'live_marker_checked': False, 'scope_verification': 'not_checked'}


def _bootstrap_entries(provider, account, parent_fid, predicate):
    import time
    for delay in (0, 0.1, 0.2, 0.4):
        if delay:
            time.sleep(delay)
        rows = tuple(provider.list_directory(account, parent_fid))
        if predicate(rows):
            return rows
    raise VfsError('OUTCOME_UNKNOWN', 'Acknowledged namespace changes were not uniquely visible after bounded read-only checks; mutations were not repeated.')


def _safe_error(error, *, stage=None):
    code = getattr(error, 'code', 'LOCAL_IO_ERROR')
    if isinstance(error, (ConfigError, TrashError)) or isinstance(error, VfsError) and not isinstance(error, ProviderError):
        message = str(error)
    elif isinstance(error, ProviderError):
        message = _diagnostic_message(code, None)
    else:
        message = 'Local diagnostic check failed; raw details are suppressed.'
    result = {'code': code, 'message': message,
              'retryable': bool(getattr(error, 'retryable', False))}
    failure_stage = getattr(error, 'stage', None) or stage
    if failure_stage:
        result.update(stage=failure_stage, message=_diagnostic_message(result['code'], failure_stage))
    for field in ('upstream_code', 'exit_code'):
        value = getattr(error, field, None)
        if type(value) is int:
            result[field] = value
    return result


def _verify_namespace(config):
    from .core.catalog import Catalog
    from .provider.quark import QuarkProvider
    from .storage import Store
    store = Store(config.state_dir / 'quark-vfs.sqlite3', read_only=True)
    try:
        Catalog(QuarkProvider(config), store, config.cache_dir / 'scope-check').assert_scope(config.account, fresh=True)
    finally:
        store.close()


def _diagnostic_message(code, stage):
    if code == 'CONFIG_REQUIRED':
        return 'Provide --config /absolute/path/config.toml; no runtime is discovered automatically.'
    if code == 'INVALID_ARGUMENT':
        return 'Invalid command arguments; use --help for supported options.'
    if code == 'RUNTIME_UNSAFE':
        return 'Local runtime checks failed; inspect data.checks and configure safe existing directories.'
    if code == 'SCOPE_MARKER_MISSING':
        return 'The bound marker was absent from a successful directory listing. Check the original management directory in the Quark client.'
    if code == 'SCOPE_MISMATCH':
        if stage == 'scope_marker_content':
            return 'The downloaded marker name, path or content does not match the local binding.'
        return 'The observed marker identity, name, type or bounded size does not match the local binding.'
    if code == 'ACCOUNT_UNBOUND':
        return 'No local scope binding exists for this account alias.'
    if code == 'AUTH_REQUIRED':
        return 'Official browser authorization is required; run auth login to complete authorization.'
    if code == 'STATE_NOT_FOUND':
        return 'The local state database does not exist; scope verification was not started.'
    if code == 'STATE_UNREADABLE':
        return 'The local state database could not be read; scope verification was not started.'
    if stage == 'scope_directory_read':
        return 'The management directory listing could not be completed. This does not establish that the directory was deleted.'
    if stage == 'scope_marker_download':
        return 'The marker download could not be completed; the scope remains unverified.'
    if stage == 'scope_marker_content':
        return 'The local marker read could not be completed safely; the scope remains unverified.'
    if stage == 'authentication_refresh':
        return 'The account status refresh could not be completed; raw upstream details are suppressed.'
    if stage is None:
        return 'The official operation was not confirmed; inspect the safe error codes before retrying.'
    return 'The diagnostic check could not be completed; inspect the safe stage and codes before recovery.'

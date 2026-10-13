"""Recoverable trash using inert, account-bound manifests and injected file operations."""
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from ..models import RemoteEntry


class TrashError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class TrashBackend(Protocol):
    def list_entries(self, parent_fid: str) -> tuple[dict, ...]: ...
    def mkdir(self, parent_fid: str, name: str) -> str: ...
    def upload_verified_bytes(self, parent_fid: str, name: str, content: bytes) -> str: ...
    def read_bytes(self, fid: str, max_bytes: int) -> bytes: ...
    def move(self, fid: str, source_parent: str, target_parent: str,
             name: str, conflict: str, expected: dict | None = None) -> dict: ...


class TrashJournal(Protocol):
    def put(self, account: str, operation_id: str, value: dict) -> None: ...
    def get(self, account: str, operation_id: str) -> dict | None: ...


def _text(value) -> bool:
    return isinstance(value, str) and bool(value)


def _identity(fid: str) -> str:
    tail = fid.rsplit("|", 1)[-1] if "|" in fid else ""
    return tail or fid


def _uuid(value) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def validate_manifest(value: object, account: str, scope_uuid: str | None = None) -> dict:
    """Only known inert fields are accepted; paths and executable instructions are rejected."""
    if not isinstance(value, dict) or set(value) != {
            'schema_version', 'account', 'scope_uuid', 'operation_id', 'created_at', 'entries'}:
        raise TrashError('INVALID_MANIFEST', 'Unknown manifest fields or structure.')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise TrashError('INVALID_MANIFEST', 'Unsupported manifest version.')
    if value['account'] != account:
        raise TrashError('ACCOUNT_MISMATCH', 'Manifest belongs to another configured account scope.')
    if not _uuid(value['scope_uuid']) or (scope_uuid is not None and value['scope_uuid'] != scope_uuid):
        raise TrashError('ACCOUNT_MISMATCH', 'Manifest namespace binding differs.')
    if not _uuid(value['operation_id']) or not _text(value['created_at']):
        raise TrashError('INVALID_MANIFEST', 'Invalid manifest identity.')
    entries = value['entries']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
        raise TrashError('INVALID_MANIFEST', 'Manifest entries outside permitted range.')
    seen = set()
    for row in entries:
        fields = {'entry_id', 'original_fid', 'original_parent_fid', 'original_name',
                  'is_dir', 'size', 'modified', 'content_hash', 'trusted_sha256', 'container_name', 'container_fid'}
        if not isinstance(row, dict) or set(row) != fields:
            raise TrashError('INVALID_MANIFEST', 'Invalid entry fields.')
        if not _uuid(row['entry_id']) or row['entry_id'] in seen:
            raise TrashError('INVALID_MANIFEST', 'Invalid or repeated entry identity.')
        seen.add(row['entry_id'])
        if not all(_text(row[k]) for k in ('original_fid', 'original_parent_fid',
                                           'original_name', 'container_fid')):
            raise TrashError('INVALID_MANIFEST', 'Required reference absent.')
        if row['container_name'] != row['entry_id'] or type(row['is_dir']) is not bool:
            raise TrashError('INVALID_MANIFEST', 'Invalid payload container or type.')
        if any(row[key] is not None and not isinstance(row[key], str) for key in ('modified', 'content_hash')):
            raise TrashError('INVALID_MANIFEST', 'Invalid optional change metadata.')
        digest = row['trusted_sha256']
        if row['is_dir'] and digest is not None:
            raise TrashError('INVALID_MANIFEST', 'Directory content fingerprints have no supported verification schema.')
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64
                                   or any(char not in '0123456789abcdef' for char in digest)):
            raise TrashError('INVALID_MANIFEST', 'Invalid trusted content fingerprint.')
        if row['size'] is not None and (type(row['size']) is not int or row['size'] < 0):
            raise TrashError('INVALID_MANIFEST', 'Invalid payload size.')
    return value


class TrashService:
    def __init__(self, backend: TrashBackend, journal: TrashJournal, *,
                 account: str, trash_root_fid: str, scope_uuid: str):
        if not _text(account) or not _text(trash_root_fid):
            raise TrashError('TRASH_NOT_CONFIGURED', 'Explicit account and trash root are required.')
        self.backend, self.journal = backend, journal
        if not _uuid(scope_uuid):
            raise TrashError("ACCOUNT_UNBOUND", "A verified namespace UUID is required.")
        self.account, self.root, self.scope_uuid = account, trash_root_fid, scope_uuid

    def _save(self, value: dict) -> None:
        self.active_operation_id = value['operation_id']
        self.journal.put(self.account, value['operation_id'], value)

    def _payload(self, parent: str, row: dict) -> dict:
        matches = [entry for entry in self.backend.list_entries(parent)
                   if entry.get('name') == row['original_name']
                   and entry.get('is_dir') is row['is_dir']
                   and (row['is_dir'] or entry.get('size') == row['size'])]
        if len(matches) != 1 or not _text(matches[0].get('fid')):
            raise TrashError('OUTCOME_UNKNOWN', 'Payload cannot be uniquely located in its dedicated container.')
        candidate = matches[0]
        if row.get('trusted_sha256') is not None:
            verify = getattr(self.backend, 'verify_payload', None)
            if verify is None:
                raise TrashError('CONTENT_UNVERIFIED', 'Backend cannot verify the recorded trusted fingerprint.')
            verify(parent, candidate['fid'], row['trusted_sha256'])
        return candidate

    def trash(self, sources: list[dict]) -> dict:
        if not sources or len(sources) > 1000:
            raise TrashError('INVALID_ARGUMENT', 'Provide between one and 1000 fresh sources.')
        # Check all inputs before making any remote change.
        seen_sources = set()
        for source in sources:
            if not all(_text(source.get(k)) for k in ('fid', 'parent_fid', 'name')):
                raise TrashError('LOCATION_UNKNOWN', 'Fresh source and explicit parent are required.')
            key = _identity(source['fid'])
            if key in seen_sources:
                raise TrashError('INVALID_ARGUMENT', 'Duplicate source references are forbidden.')
            seen_sources.add(key)
            guard = getattr(self.backend, 'guard_source', None)
            if guard is not None:
                guard(source['fid'])
            if type(source.get('is_dir')) is not bool:
                raise TrashError('INVALID_ARGUMENT', 'Source type is required.')
            observed = self._payload(source['parent_fid'], {
                'original_name': source['name'], 'is_dir': source['is_dir'], 'size': source.get('size')})
            if _identity(observed['fid']) != _identity(source['fid']):
                raise TrashError('SOURCE_CHANGED', 'Fresh source reference has changed.')
            for key in ('modified', 'content_hash'):
                if source.get(key) is not None and observed.get(key) != source[key]:
                    raise TrashError('SOURCE_CHANGED', 'Source change metadata differs.')
            source['fid'] = observed['fid']
            source.setdefault('modified', observed.get('modified'))
            source.setdefault('content_hash', observed.get('content_hash'))
        op = str(uuid.uuid4())
        state: dict[str, Any] = {'operation_id': op, 'status': 'intent', 'entries': [], 'account': self.account}
        self._save(state)
        container = self.backend.mkdir(self.root, op)
        state['operation_container_fid'] = container
        self._save(state)
        entries = []
        for source in sources:
            entry_id = str(uuid.uuid4())
            fid = self.backend.mkdir(container, entry_id)
            entries.append({'entry_id': entry_id, 'original_fid': source['fid'],
                            'original_parent_fid': source['parent_fid'], 'original_name': source['name'],
                            'is_dir': source['is_dir'], 'size': source.get('size'),
                            'modified': source.get('modified'), 'content_hash': source.get('content_hash'),
                            'trusted_sha256': source.get('trusted_sha256'),
                            'container_name': entry_id, 'container_fid': fid})
        manifest = validate_manifest({'schema_version': 1, 'account': self.account, 'scope_uuid': self.scope_uuid,
                                     'operation_id': op, 'created_at': datetime.now(UTC).isoformat(),
                                     'entries': entries}, self.account, self.scope_uuid)
        content = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode('utf-8')
        state['manifest'] = manifest
        state['status'] = 'manifest_pending'
        self._save(state)
        # Backend promises upload plus exact readback before returning.
        manifest_fid = self.backend.upload_verified_bytes(container, 'manifest.json', content)
        if self.backend.read_bytes(manifest_fid, len(content) + 1) != content:
            raise TrashError('MANIFEST_UNVERIFIED', 'Manifest readback differs; no payload was moved.')
        state['manifest_fid'], state['status'] = manifest_fid, 'manifest_verified'
        self._save(state)
        for row in entries:
            state['status'], state['active_entry'] = 'move_pending', row['entry_id']
            self._save(state)
            try:
                self.backend.move(row['original_fid'], row['original_parent_fid'],
                                  row['container_fid'], row['original_name'], 'fail', expected=row)
            except Exception:
                state['status'] = 'outcome_unknown'
                self._save(state)
                raise
            payload = self._payload(row['container_fid'], row)
            state['entries'].append({'entry_id': row['entry_id'], 'payload_fid': payload['fid'],
                                     'status': 'trashed', 'payload_expected': self._freeze_payload(payload)})
            self._save(state)
        event = self._completion_bytes(state)
        state['status'] = 'completion_pending'
        self._save(state)
        self.backend.upload_verified_bytes(container, 'completed-' + str(uuid.uuid4()) + '.json', event)
        state['status'] = 'completed'
        self._save(state)
        return state

    @staticmethod
    def _freeze_payload(payload: dict) -> dict:
        return {'original_name': payload['name'], 'is_dir': payload['is_dir'],
                'size': payload.get('size'), 'modified': payload.get('modified'),
                'content_hash': payload.get('content_hash')}

    @classmethod
    def _matches_payload(cls, payload: dict, expected: dict) -> bool:
        return cls._freeze_payload(payload) == expected

    def _completion_bytes(self, state: dict) -> bytes:
        return json.dumps({'schema_version': 1, 'kind': 'trash_completion',
                           'account': self.account, 'scope_uuid': self.scope_uuid,
                           'operation_id': state['operation_id'], 'entries': state['entries']},
                          sort_keys=True).encode()

    def _observe_checkpoint_payload(self, operation_id: str, entry_id: str, trusted_sha256: str) -> None:
        """Explicit operator reconciliation with an independently known source digest."""
        state = self.journal.get(self.account, operation_id)
        if not state:
            raise TrashError('LOCATION_UNKNOWN', 'Checkpoint operation is absent.')
        manifest = validate_manifest(state['manifest'], self.account, self.scope_uuid)
        rows = [row for row in manifest['entries'] if row['entry_id'] == entry_id]
        records = [row for row in state['entries'] if row['entry_id'] == entry_id]
        if (len(rows) != 1 or len(records) != 1 or rows[0]['is_dir']
                or records[0]['status'] != 'trashed' or state.get('restore_attempts')
                or records[0].get('payload_expected') is not None
                or not isinstance(trusted_sha256, str) or len(trusted_sha256) != 64
                or any(c not in '0123456789abcdef' for c in trusted_sha256)):
            raise TrashError('RECONCILIATION_REQUIRED', 'Only an unattempted file checkpoint with an explicit known digest can be observed.')
        contents = self.backend.list_entries(state['operation_container_fid'])
        if self._restore_intents(contents, operation_id) or self._restore_events(contents, operation_id):
            raise TrashError('RECONCILIATION_REQUIRED', 'Remote recovery history prohibits checkpoint observation.')
        row = rows[0]
        payload = self._payload(row['container_fid'], row)
        verify = getattr(self.backend, 'verify_payload', None)
        if verify is None:
            raise TrashError('CONTENT_UNVERIFIED', 'Checkpoint requires actual trusted digest verification.')
        verify(row['container_fid'], payload['fid'], trusted_sha256)
        # Freeze only after actual content verification; preserve the original immutable manifest.
        records[0]['payload_expected'] = self._freeze_payload(payload)
        records[0]['checkpoint_sha256'] = trusted_sha256
        self.backend.upload_verified_bytes(state['operation_container_fid'],
            'completed-' + str(uuid.uuid4()) + '.json', self._completion_bytes(state))
        self._save(state)

    def restore(self, operation_id: str, entry_id: str, *, destination: str | None = None,
                conflict: str = 'fail') -> dict:
        if conflict not in ('fail', 'keep_both'):
            raise TrashError('INVALID_ARGUMENT', 'Only fail and keep_both are supported.')
        state = self.journal.get(self.account, operation_id)
        if not state or state.get('account') != self.account:
            raise TrashError('LOCATION_UNKNOWN', 'No account-bound recovery record exists.')
        manifest = validate_manifest(state.get('manifest'), self.account, self.scope_uuid)
        matches = [row for row in manifest['entries'] if row['entry_id'] == entry_id]
        if len(matches) != 1:
            raise TrashError('LOCATION_UNKNOWN', 'Recovery entry absent.')
        row = matches[0]
        recorded = [item for item in state.get('entries', []) if item.get('entry_id') == entry_id]
        if len(recorded) != 1:
            raise TrashError('RECONCILIATION_REQUIRED', 'Recovery entry has not been observed in its container.')
        if recorded[0].get('status') == 'restored':
            raise TrashError('ALREADY_RESTORED', 'This recovery entry has already been restored; no object will be moved.')
        attempts = state.setdefault('restore_attempts', {})
        if (recorded[0].get('status') != 'trashed' or entry_id in attempts):
            raise TrashError('RECONCILIATION_REQUIRED', 'A prior recovery attempt must be reconciled before any further movement.')
        target = destination if destination is not None else row['original_parent_fid']
        if not _text(target):
            raise TrashError('LOCATION_UNKNOWN', 'Explicit recovery destination required.')
        # Fresh listing validates parent availability; never infer it from an old path.
        target_entries = self.backend.list_entries(target)
        validate_target = getattr(self.backend, 'validate_destination', None)
        if validate_target is not None:
            validate_target(target)
        if conflict == 'fail' and any(entry.get('name') == row['original_name'] for entry in target_entries):
            raise TrashError('NAME_CONFLICT', 'Recovery target name already exists; no restore intent or movement was dispatched.')
        payload = self._payload(row['container_fid'], row)
        baseline = recorded[0].get('payload_expected')
        if (baseline is None or _identity(payload['fid']) != _identity(recorded[0].get('payload_fid', ''))
                or not self._matches_payload(payload, baseline)):
            raise TrashError('EXPECTED_CHANGED', 'Payload differs from the verified post-trash observation or lacks it.')
        if recorded[0].get('checkpoint_sha256') is not None:
            verify = getattr(self.backend, 'verify_payload', None)
            if verify is None:
                raise TrashError('CONTENT_UNVERIFIED', 'Checkpoint digest verification is unavailable.')
            verify(row['container_fid'], payload['fid'], recorded[0]['checkpoint_sha256'])
        planned = {**row, **baseline}
        preflight = getattr(self.backend, 'preflight_move', None)
        if preflight is not None:
            preflight(payload['fid'], row['container_fid'], target, conflict, planned)
        state['status'] = 'restore_pending'
        attempt_id = str(uuid.uuid4())
        attempts[entry_id] = {'destination': target, 'conflict': conflict, 'state': 'intent_pending', 'attempt_id': attempt_id}
        self._save(state)
        intent = json.dumps({'schema_version': 1, 'kind': 'restore_intent', 'account': self.account,
                             'scope_uuid': self.scope_uuid, 'operation_id': operation_id, 'entry_id': entry_id,
                             'attempt_id': attempt_id, 'destination': target, 'original_name': row['original_name'],
                             'conflict': conflict, 'state': 'planned'}, sort_keys=True).encode()
        self.backend.upload_verified_bytes(state['operation_container_fid'], 'restore-intent-' + attempt_id + '.json', intent)
        attempts[entry_id]['state'] = 'dispatched'
        self._save(state)
        try:
            result = self.backend.move(payload['fid'], row['container_fid'], target,
                                       row['original_name'], conflict, expected=planned)
        except Exception:
            state['status'] = 'outcome_unknown'
            attempts[entry_id]['state'] = 'outcome_unknown'
            self._save(state)
            raise
        state['status'] = 'restore_event_pending'
        attempts[entry_id]['state'] = 'event_pending'
        state.setdefault('restorations', []).append({'entry_id': entry_id, 'destination': target,
                                                     'result': result})
        self._save(state)
        observed_results = [item.get('result', {}).get('entry') for item in result.get('items', [])
                            if isinstance(item.get('result'), dict)]
        names = [entry.get('name') for entry in observed_results if isinstance(entry, dict)]
        target_name = names[0] if len(names) == 1 and _text(names[0]) else row['original_name']
        event = json.dumps({'schema_version': 1, 'kind': 'restore_event', 'account': self.account, 'scope_uuid': self.scope_uuid,
                            'operation_id': operation_id, 'entry_id': entry_id, 'attempt_id': attempt_id,
                            'destination': target, 'target_name': target_name, 'state': 'restored'}, sort_keys=True).encode()
        self.backend.upload_verified_bytes(state['operation_container_fid'],
                                           'restored-' + str(uuid.uuid4()) + '.json', event)
        state['status'] = 'restored'
        attempts[entry_id]['state'] = 'restored'
        recorded[0]['status'] = 'restored'
        self._save(state)
        return result

    def rebuild(self) -> tuple[dict, ...]:
        recovered = []
        for operation in self.backend.list_entries(self.root):
            if not operation.get('is_dir') or not _uuid(operation.get('name')):
                continue
            contents = self.backend.list_entries(operation['fid'])
            files = [row for row in contents if row.get('name') == 'manifest.json' and not row.get('is_dir')]
            if len(files) != 1:
                continue
            raw = self.backend.read_bytes(files[0]['fid'], 1024 * 1024)
            if len(raw) > 1024 * 1024:
                raise TrashError('INVALID_MANIFEST', 'Manifest too large.')
            try:
                manifest = validate_manifest(json.loads(raw), self.account, self.scope_uuid)
            except (ValueError, UnicodeError) as exc:
                raise TrashError('INVALID_MANIFEST', 'Manifest is not valid JSON.') from exc
            if manifest['operation_id'] != operation['name']:
                raise TrashError('INVALID_MANIFEST', 'Manifest and operation directory disagree.')
            restore_events = self._restore_events(contents, manifest['operation_id'])
            cloud_intents = self._restore_intents(contents, manifest['operation_id'])
            previous = self.journal.get(self.account, manifest['operation_id']) or {}
            previous_attempts = previous.get('restore_attempts', {})
            completion = self._completion_entries(contents, manifest)
            entries = []
            for row in manifest['entries']:
                identity = row['entry_id']
                containers = [candidate for candidate in contents
                              if candidate.get('name') == row['container_name'] and candidate.get('is_dir')]
                if len(containers) != 1:
                    entries.append({'entry_id': identity, 'payload_fid': None, 'status': 'needs_reconciliation'})
                    continue
                row['container_fid'] = containers[0]['fid']
                restored = restore_events.get(identity)
                intents = cloud_intents.get(identity, [])
                if restored is not None:
                    # Restored history takes precedence. A refilled container never revives the entry.
                    status = 'needs_reconciliation'
                    if (len(intents) == 1 and intents[0]['attempt_id'] == restored['attempt_id']
                            and not self.backend.list_entries(row['container_fid'])):
                        try:
                            self._payload(restored['destination'], {**row, 'original_name': restored['target_name']})
                        except Exception:
                            pass
                        else:
                            status = 'restored'
                    entries.append({'entry_id': identity, 'payload_fid': None, 'status': status})
                    continue
                if identity in previous_attempts or intents:
                    entries.append({'entry_id': identity, 'payload_fid': None, 'status': 'needs_reconciliation'})
                    continue
                try:
                    payload = self._payload(row['container_fid'], row)
                except TrashError:
                    entries.append({'entry_id': identity, 'payload_fid': None, 'status': 'needs_reconciliation'})
                else:
                    observed = completion.get(identity)
                    valid = (observed is not None and _identity(payload['fid']) == _identity(observed['payload_fid'])
                             and self._matches_payload(payload, observed['payload_expected']))
                    if observed is not None and valid and observed.get('checkpoint_sha256'):
                        verify = getattr(self.backend, 'verify_payload', None)
                        if verify is None:
                            valid = False
                        else:
                            verify(row['container_fid'], payload['fid'], observed['checkpoint_sha256'])
                    entries.append({'entry_id': identity, 'payload_fid': payload['fid'],
                                    'status': 'trashed' if valid else 'needs_reconciliation',
                                    **({'payload_expected': observed['payload_expected'],
                                        **({'checkpoint_sha256': observed['checkpoint_sha256']} if observed.get('checkpoint_sha256') else {})}
                                       if valid and observed is not None else {})})
            state = {'operation_id': manifest['operation_id'], 'account': self.account,
                     'operation_container_fid': operation['fid'], 'manifest': manifest,
                     'entries': entries, 'restore_attempts': previous_attempts,
                     'restorations': list(restore_events.values()), 'status': 'rebuilt'}
            self._save(state)
            recovered.append(state)
        return tuple(recovered)

    def _completion_entries(self, contents: tuple[dict, ...], manifest: dict) -> dict:
        found: dict[str, dict] = {}
        allowed = {'entry_id', 'payload_fid', 'status', 'payload_expected', 'checkpoint_sha256'}
        expected_fields = {'original_name', 'is_dir', 'size', 'modified', 'content_hash'}
        manifest_ids = {row['entry_id'] for row in manifest['entries']}
        for entry in contents:
            name = entry.get('name', '')
            if entry.get('is_dir') or not isinstance(name, str) or not name.startswith('completed-') or not name.endswith('.json'):
                continue
            if not _uuid(name[len('completed-'):-len('.json')]):
                continue
            raw = self.backend.read_bytes(entry['fid'], 1048576)
            if len(raw) > 1048576:
                raise TrashError('INVALID_MANIFEST', 'Completion observation exceeds its byte limit.')
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if (not isinstance(event, dict) or set(event) != {'schema_version','kind','account','scope_uuid','operation_id','entries'}
                    or type(event['schema_version']) is not int or event['schema_version'] != 1
                    or event['kind'] != 'trash_completion' or event['account'] != self.account
                    or event['scope_uuid'] != self.scope_uuid or event['operation_id'] != manifest['operation_id']
                    or not isinstance(event['entries'], list)):
                continue
            candidates: dict[str, dict] = {}
            for item in event['entries']:
                if (not isinstance(item, dict) or not set(item) <= allowed
                        or not {'entry_id','payload_fid','status','payload_expected'} <= set(item)
                        or item['entry_id'] not in manifest_ids or item['entry_id'] in candidates
                        or item['status'] != 'trashed' or not _text(item['payload_fid'])
                        or not isinstance(item['payload_expected'], dict) or set(item['payload_expected']) != expected_fields):
                    candidates = {}
                    break
                expected = item['payload_expected']
                original = next(row for row in manifest['entries'] if row['entry_id'] == item['entry_id'])
                if (expected['original_name'] != original['original_name'] or expected['is_dir'] is not original['is_dir']
                        or type(expected['size']) is not type(original['size'])
                        or expected['size'] != original['size']
                        or any(expected[k] is not None and not isinstance(expected[k], str) for k in ('modified','content_hash'))):
                    candidates = {}
                    break
                digest = item.get('checkpoint_sha256')
                if digest is not None and (original['is_dir'] or not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
                    candidates = {}
                    break
                candidates[item['entry_id']] = item
            if set(candidates) != manifest_ids:
                continue
            for key,item in candidates.items():
                if key in found and found[key] != item:
                    raise TrashError('RECONCILIATION_REQUIRED', 'Conflicting post-trash observations require reconciliation.')
                found[key] = item
        return found

    def _restore_events(self, contents: tuple[dict, ...], operation_id: str) -> dict:
        events: dict[str, dict | None] = {}
        for entry in contents:
            name = entry.get('name', '')
            if entry.get('is_dir') or not isinstance(name, str) or not name.startswith('restored-') or not name.endswith('.json'):
                continue
            if not _uuid(name[len('restored-'):-len('.json')]):
                continue
            raw = self.backend.read_bytes(entry['fid'], 65536)
            if len(raw) > 65536:
                raise TrashError('INVALID_MANIFEST', 'Recovery event exceeds the byte limit.')
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            fields = {'schema_version', 'kind', 'account', 'scope_uuid', 'operation_id',
                      'entry_id', 'attempt_id', 'destination', 'target_name', 'state'}
            if (not isinstance(event, dict) or set(event) != fields or type(event['schema_version']) is not int
                    or event['schema_version'] != 1 or event['kind'] != 'restore_event'
                    or event['account'] != self.account or event['scope_uuid'] != self.scope_uuid
                    or event['operation_id'] != operation_id or not _uuid(event['entry_id']) or not _uuid(event['attempt_id'])
                    or not _text(event['destination']) or not _text(event['target_name'])
                    or event['state'] != 'restored'):
                continue
            if event['entry_id'] in events:
                # Multiple recovery events are not ordered by directory iteration.
                events[event['entry_id']] = None
            else:
                events[event['entry_id']] = event
        return {key: value for key, value in events.items() if value is not None}

    def _restore_intents(self, contents: tuple[dict, ...], operation_id: str) -> dict:
        intents: dict[str, list[dict]] = {}
        for entry in contents:
            name = entry.get('name', '')
            if (entry.get('is_dir') or not isinstance(name, str) or not name.startswith('restore-intent-')
                    or not name.endswith('.json') or not _uuid(name[len('restore-intent-'):-5])):
                continue
            raw = self.backend.read_bytes(entry['fid'], 65536)
            if len(raw) > 65536:
                raise TrashError('INVALID_MANIFEST', 'Recovery intent exceeds the byte limit.')
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            fields = {'schema_version', 'kind', 'account', 'scope_uuid', 'operation_id', 'entry_id',
                      'attempt_id', 'destination', 'original_name', 'conflict', 'state'}
            if (not isinstance(event, dict) or set(event) != fields or type(event['schema_version']) is not int
                    or event['schema_version'] != 1 or event['kind'] != 'restore_intent'
                    or event['account'] != self.account or event['scope_uuid'] != self.scope_uuid
                    or event['operation_id'] != operation_id or not _uuid(event['entry_id'])
                    or not _uuid(event['attempt_id']) or not _text(event['destination'])
                    or not _text(event['original_name']) or event['conflict'] not in ('fail', 'keep_both')
                    or event['state'] != 'planned'):
                continue
            intents.setdefault(event['entry_id'], []).append(event)
        return intents


class StoreTrashJournal:
    def __init__(self, store):
        self.store = store

    def put(self, account: str, operation_id: str, value: dict) -> None:
        self.store.put_document(account, 'trash', operation_id, value)

    def get(self, account: str, operation_id: str) -> dict | None:
        return self.store.get_document(account, 'trash', operation_id)


class CoreTrashBackend:
    """Use guarded common core; local paths are generated here, never taken from manifests."""
    def __init__(self, catalog, operations, transfers, account: str, workspace):
        from pathlib import Path
        self.catalog, self.operations, self.transfers = catalog, operations, transfers
        self.account, self.workspace = account, Path(workspace)
        self.observed: dict[str, RemoteEntry] = {}

    def list_entries(self, parent_fid: str) -> tuple[dict, ...]:
        self.catalog.assert_scope(self.account)
        entries = self.catalog.entries(self.account, parent_fid)
        self.observed.update({row.fid: row for row in entries})
        return tuple({'fid': row.fid, 'parent_fid': row.parent_fid, 'name': row.name,
                      'is_dir': row.kind == 'directory', 'size': row.size,
                      'modified': row.modified, 'content_hash': row.content_hash}
                     for row in entries)

    def mkdir(self, parent_fid: str, name: str) -> str:
        from ..models import FileRef
        return self.operations.mkdir(FileRef(self.account, fid=parent_fid), name).fid

    def upload_verified_bytes(self, parent_fid: str, name: str, content: bytes) -> str:
        from ..models import FileRef
        directory = self.workspace / str(uuid.uuid4())
        directory.mkdir(parents=True, mode=0o700)
        source = directory / name
        source.write_bytes(content)
        receipt = self.transfers.upload_file(source, FileRef(self.account, fid=parent_fid))
        if receipt.entry is None:
            raise TrashError('OUTCOME_UNKNOWN', 'Manifest upload lacks an observed object.')
        downloaded = directory / 'verified.bin'
        self.transfers.download(FileRef(self.account, fid=receipt.entry.fid, parent_fid_hint=parent_fid),
                                downloaded, trusted_sha256=receipt.sha256)
        if downloaded.read_bytes() != content:
            raise TrashError('MANIFEST_UNVERIFIED', 'Immutable record content differs.')
        self.observed[receipt.entry.fid] = receipt.entry
        return receipt.entry.fid

    def read_bytes(self, fid: str, max_bytes: int) -> bytes:
        from ..models import FileRef
        entry = self.observed.get(fid)
        if entry is None or entry.kind != 'file' or entry.size is None or entry.size > max_bytes:
            raise TrashError('INVALID_MANIFEST', 'A fresh bounded manifest size is required before download.')
        entry = self.catalog.stat(FileRef(self.account, fid=entry.fid, parent_fid_hint=entry.parent_fid), fresh=True)
        if entry.kind != 'file' or entry.size is None or entry.size > max_bytes:
            raise TrashError('INVALID_MANIFEST', 'Fresh manifest exceeds the permitted download size.')
        directory = self.workspace / str(uuid.uuid4())
        directory.mkdir(parents=True, mode=0o700)
        target = directory / 'record.bin'
        self.transfers.download(FileRef(self.account, fid=entry.fid, parent_fid_hint=entry.parent_fid), target)
        with target.open('rb') as stream:
            return stream.read(max_bytes + 1)

    def move(self, fid: str, source_parent: str, target_parent: str,
             name: str, conflict: str, expected: dict | None = None) -> dict:
        from dataclasses import asdict

        from ..models import Expected, FileRef
        from .operations import check_expected
        planned = None
        if expected is not None:
            fresh = self.catalog.resolve(FileRef(self.account, fid=fid, parent_fid_hint=source_parent))
            expectation = Expected(expected["original_name"], source_parent,
                                     "directory" if expected["is_dir"] else "file",
                                     expected["size"], expected["modified"], expected["content_hash"])
            check_expected(fresh, expectation)
            fid = fresh.fid
            planned = {fid: expectation}
        result = self.operations.move([FileRef(self.account, fid=fid, parent_fid_hint=source_parent)],
                                      FileRef(self.account, fid=target_parent), conflict=conflict, expected=planned)
        if result.state != 'succeeded':
            raise TrashError('OUTCOME_UNKNOWN', 'Move requires reconciliation; do not repeat it.')
        return asdict(result)

    def verify_payload(self, parent: str, fid: str, digest: str) -> None:
        from ..models import FileRef
        directory = self.workspace / str(uuid.uuid4())
        directory.mkdir(parents=True, mode=0o700)
        self.transfers.download(FileRef(self.account, fid=fid, parent_fid_hint=parent),
                                directory / 'payload.bin', trusted_sha256=digest)

    def preflight_move(self, fid: str, source_parent: str, target_parent: str,
                       conflict: str, expected: dict) -> None:
        from ..models import Expected, FileRef
        from .operations import check_expected, choose_name
        source = self.catalog.resolve(FileRef(self.account, fid=fid, parent_fid_hint=source_parent))
        target = self.catalog.resolve(FileRef(self.account, fid=target_parent))
        if target.kind != 'directory':
            raise TrashError('NOT_DIRECTORY', 'Recovery destination is not a directory.')
        self.operations.assert_mutable(self.account, source.fid)
        check_expected(source, Expected(expected['original_name'], source_parent,
                       'directory' if expected['is_dir'] else 'file', expected['size'],
                       expected['modified'], expected['content_hash']))
        self.operations._check_ancestry(self.account, source, target)
        choose_name(source.name, self.catalog.entries(self.account, target.fid), conflict)

    def validate_destination(self, fid: str) -> None:
        from ..models import FileRef
        entry = self.catalog.resolve(FileRef(self.account, fid=fid))
        if entry.kind != 'directory':
            raise TrashError('NOT_DIRECTORY', 'Recovery destination must be a fresh directory.')

    def guard_source(self, fid: str) -> None:
        from ..models import FileRef
        self.operations.assert_mutable(self.account, fid)
        entry = self.catalog.resolve(FileRef(self.account, fid=fid))
        if entry.kind != 'directory':
            return
        pending = [entry.fid]
        seen: set[str] = set()
        while pending:
            parent = pending.pop()
            key = _identity(parent)
            if key in seen or len(seen) >= 1000:
                raise TrashError('ANCESTRY_UNKNOWN', 'Protected namespace ancestry cannot be safely confirmed.')
            seen.add(key)
            for child in self.catalog.entries(self.account, parent):
                self.operations.assert_mutable(self.account, child.fid)
                if child.kind == 'directory':
                    pending.append(child.fid)

"""Durable synchronous tasks; unknown writes are never automatically replayed."""
from collections.abc import Callable

from ..models import BatchResult, VfsError
from ..storage import Store


class Jobs:
    def __init__(self, store: Store):
        self.store = store
        self._evidence: tuple[str, int, dict] | None = None

    def record(self, facts: dict) -> None:
        if self._evidence is None:
            raise VfsError('JOB_CONTEXT_UNKNOWN', 'No active item to attach durable facts.')
        key, ordinal, evidence = self._evidence
        evidence.update(facts)
        self.store.set_item(key, ordinal, 'running', {'phase': 'dispatched', 'evidence': evidence})

    def run(self, account: str, action: str, items: list[dict], execute: Callable[[dict], dict],
            *, preflight: Callable[[dict], None] | None = None) -> BatchResult:
        key = self.store.create_job(account, action, items)
        self.store.set_job(key, 'running')
        uncertain = False
        failed = False
        for index, item in enumerate(items):
            if uncertain:
                break
            if preflight is not None:
                try:
                    preflight(item)
                except Exception as error:
                    self.store.set_item(key, index, 'failed', {'error_code': getattr(error, 'code', 'PREFLIGHT_FAILED'), 'phase': 'preflight'})
                    failed = True
                    continue
            self.store.set_item(key, index, 'running', {'phase': 'dispatched'})
            previous = self._evidence
            self._evidence = (key, index, {})
            try:
                result = execute(item)
            except Exception as error:
                code = getattr(error, 'code', 'OUTCOME_UNKNOWN')
                self.store.set_item(key, index, 'needs_reconciliation', {'error_code': code, 'phase': 'dispatched', 'evidence': self._evidence[2]})
                failed = True
                uncertain = True
            else:
                result['evidence'] = self._evidence[2]
                self.store.set_item(key, index, 'skipped' if result.get('outcome') == 'excluded' else 'succeeded', result)
            finally:
                self._evidence = previous
        items_out = self.store.job(account, key)['items']
        success = any(i['state'] == 'succeeded' for i in items_out)
        skipped = any(i['state'] == 'skipped' for i in items_out)
        state = 'needs_reconciliation' if uncertain else ('partial' if (failed or skipped) and success else 'failed' if failed else 'excluded' if skipped else 'succeeded')
        self.store.set_job(key, state)
        return BatchResult(key, state, items_out)

    def list(self, account: str) -> tuple[dict, ...]:
        return self.store.jobs(account)

    def get(self, account: str, key: str) -> dict:
        return self.store.job(account, key)

    def resume(self, account: str, key: str, *, execute: Callable[[dict], dict] | None = None,
               preflight: Callable[[dict], None] | None = None) -> dict:
        """Resume only never-dispatched items; a download dispatcher may safely restart reads.

        The caller supplies the same constrained action implementation and checks
        persisted expectations again. Unknown mutations require reconciliation.
        """
        with self.store.account_lock(account):
            job = self.store.job(account, key)
            uncertain = any(i['state'] in ('running', 'needs_reconciliation') for i in job['items'])
            if uncertain and job['action'] != 'download':
                for item in job['items']:
                    if item['state'] == 'running':
                        self.store.set_item(key, item['ordinal'], 'needs_reconciliation', {'error_code': 'HOST_INTERRUPTED'})
                self.store.set_job(key, 'needs_reconciliation')
                raise VfsError('RECONCILIATION_REQUIRED', 'Unknown mutations cannot be replayed; reconcile observed effects first.', job_id=key)
            if all(i['state'] in ('succeeded', 'skipped') for i in job['items']):
                return self.store.job(account, key)
            if execute is None or preflight is None:
                raise VfsError('RESUME_UNAVAILABLE', 'A constrained action dispatcher and fresh preflight are required to resume.', job_id=key)
            for item in job['items']:
                if item['state'] in ('succeeded', 'skipped'):
                    continue
                try:
                    preflight(item['input'])
                except Exception as error:
                    self.store.set_item(key, item['ordinal'], 'failed', {'error_code': getattr(error, 'code', 'PREFLIGHT_FAILED'),
                        'phase': 'preflight', 'previous_attempt': item['result']})
                    self.store.set_job(key, 'partial' if any(i['state'] == 'succeeded' for i in job['items']) else 'failed')
                    if isinstance(error, VfsError):
                        error.job_id = key
                    raise
                self.store.set_item(key, item['ordinal'], 'running', {'phase': 'dispatched'})
                previous = self._evidence
                self._evidence = (key, item['ordinal'], {})
                try:
                    result = execute(item['input'])
                except Exception as error:
                    self.store.set_item(key, item['ordinal'], 'needs_reconciliation', {'error_code': getattr(error, 'code', 'OUTCOME_UNKNOWN'),
                        'evidence': self._evidence[2], 'previous_attempt': item['result']})
                    self.store.set_job(key, 'needs_reconciliation')
                    if isinstance(error, VfsError):
                        error.job_id = key
                    raise
                finally:
                    evidence = self._evidence[2]
                    self._evidence = previous
                result['evidence'] = evidence
                result['previous_attempt'] = item['result']
                self.store.set_item(key, item['ordinal'], 'succeeded', result)
            self.store.set_job(key, 'succeeded')
            result = self.store.job(account, key)
            result['resume_mode'] = 'safe_restart' if job['action'] == 'download' else 'undispatched_items_only'
            return result

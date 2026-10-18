"""Synthetic M1 acceptance through the real CLI and shared core; no retries or deletion."""
import argparse
import contextlib
import io
import json
import os
import tempfile
import uuid
from pathlib import Path

from quark_vfs.cli import main as cli_main
from quark_vfs.config import load_config
from quark_vfs.models import VfsError
from quark_vfs.storage import Store


class M1Acceptance:
    def __init__(self, config_path: Path, *, parent_fid: str | None = None, progress=None):
        self.config_path = Path(config_path).resolve()
        self.config = load_config(self.config_path)
        if not self.config.file_operations_enabled or not self.config.trash_enabled:
            raise VfsError('CAPABILITY_UNVERIFIED', 'Acceptance requires explicit file and trash gates; it does not change them.')
        self.parent = parent_fid
        self.progress = progress
        self.id = str(uuid.uuid4())
        self.local = Path(tempfile.mkdtemp(prefix='quark-vfs-m1-' + self.id + '-')).resolve()
        os.chmod(self.local, 0o700)
        self.summary = {'acceptance_id': self.id, 'checks': {}, 'automatic_retries': 0,
                        'permanent_deletions': 0, 'gates_changed': False, 'phase': None,
                        'local_journal_directory': str(self.local)}
        self.step = 'initialization'
        self.sequence = 0

    @classmethod
    def from_search_checkpoint(cls, config_path, journal_path, *, progress=None, _events=None):
        from quark_vfs.core.catalog import secure_bytes
        local = Path(journal_path).parent
        raw, _ = secure_bytes(Path(journal_path), local, max_bytes=4194304)
        events = _events if _events is not None else [json.loads(line) for line in raw.splitlines()]
        intents = [x for x in events if x.get('event') == 'intent']
        responses = [x for x in events if x.get('event') == 'response']
        commands = [x['argv'][2] for x in intents]
        prefix = ['mkdir', 'list', 'mkdir', 'list', 'upload', 'list', 'download', 'stat', 'search']
        if (commands not in (prefix, ['init-scope', *prefix]) or len(responses) != len(intents)
                or len(events) != 2 * len(intents) + 1 or events[-1].get('event') != 'stopped'
                or responses[-1]['result'].get('error', {}).get('code') != 'UPSTREAM_FAILED'
                or any(x['result'].get('ok') is not True for x in responses[:-1])):
            raise VfsError('INVALID_CHECKPOINT', 'Only the successful source prefix followed by failed search is resumable.')
        for i, (intent, response) in enumerate(zip(intents, responses, strict=True), 1):
            if (events[2*i-2] != intent or events[2*i-1] != response
                    or intent['step'] != str(i)+'-'+intent['argv'][2] or response['step'] != intent['step']):
                raise VfsError('INVALID_CHECKPOINT', 'Checkpoint sequence differs.')
        offset = len(commands)-len(prefix)
        samples, trash = responses[offset+2]['result']['data'], responses[offset]['result']['data']
        identifier = samples['name'].removeprefix('M1-samples-')
        if (str(uuid.UUID(identifier)) != identifier or trash['name'] != 'M1-trash-'+identifier
                or not local.name.startswith('quark-vfs-m1-'+identifier+'-')
                or samples['kind'] != 'directory' or trash['kind'] != 'directory'):
            raise VfsError('INVALID_CHECKPOINT', 'Exclusive synthetic namespace differs.')
        secure_bytes(local/'validation.toml', local, max_bytes=65536)
        config, authorized = load_config(local/'validation.toml'), load_config(config_path)
        if any(getattr(config,f) != getattr(authorized,f) for f in ('account','state_dir','cache_dir','node_executable','official_entry')):
            raise VfsError('INVALID_CHECKPOINT', 'Authorized runtime or local account differs.')
        from quark_vfs.models import same_browse_identity
        if (not config.file_operations_enabled or not config.trash_enabled
                or not same_browse_identity(config.trash_root_fid or '', trash['fid'])
                or not same_browse_identity(config.trash_root_parent_fid or '', samples['parent_fid'])):
            raise VfsError('INVALID_CHECKPOINT', 'Dedicated trash or gates differ.')
        source = local/('source-'+identifier+'.md')
        payload, _ = secure_bytes(source,local,max_bytes=1024)
        if (payload != b'M1 synthetic document\n'
                or intents[offset+4]['argv'][3:] != ['--parent',json.dumps({'fid':samples['fid']}),'--local-path',str(source)]
                or responses[offset+1]['result']['data']['entries'] or responses[offset+3]['result']['data']['entries']
                or responses[offset+4]['result']['data'].get('state') != 'succeeded'):
            raise VfsError('INVALID_CHECKPOINT', 'Verified source prefix changed.')
        if intents[-1]['argv'][3:] not in (['--keyword',identifier],['--keyword',identifier,'--parent-fid',samples['fid']]):
            raise VfsError('INVALID_CHECKPOINT', 'Failed search differs.')
        obj = object.__new__(cls)
        obj.config, obj.config_path, obj.local, obj.id = config,local/'validation.toml',local,identifier
        obj.parent, obj.progress, obj.sequence, obj.step = None,progress,len(intents),intents[-1]['step']
        obj.summary = {'acceptance_id':identifier,'checks':{},'automatic_retries':0,'permanent_deletions':0,
                       'gates_changed':False,'local_journal_directory':str(local),'resume_mode':'strict_search_checkpoint'}
        obj._checkpoint_source = source
        return obj

    def resume_after_search(self, phase):
        self.summary['phase'] = phase
        self.record('resume_after_search_checkpoint',reason='parent_filter_21001_global_unique_uuid_search')
        try:
            # No initialization is allowed on this continuation.
            store = Store(self.config.state_dir/'quark-vfs.sqlite3')
            try:
                binding = store.binding(self.config.account)
                management = store.get_document(self.config.account,'management','root')
            finally:
                store.close()
            if not management:
                raise VfsError('INVALID_CHECKPOINT','Existing management binding is required.')
            samples = self.unique(management['fid'],'M1-samples-'+self.id)
            trash = self.unique(management['fid'],'M1-trash-'+self.id)
            from quark_vfs.models import same_browse_identity
            if not same_browse_identity(trash['fid'], self.config.trash_root_fid or ''):
                raise VfsError('INVALID_CHECKPOINT','Dedicated trash identity changed.')
            rows = self.children(samples['fid'])
            source, payload = self._checkpoint_source,b'M1 synthetic document\n'
            if len(rows)!=1 or rows[0]['name']!=source.name or rows[0]['kind']!='file' or rows[0]['size']!=len(payload):
                raise VfsError('INVALID_CHECKPOINT','Source-only checkpoint changed.')
            self.download_equal(rows[0],payload)
            self.call('search','--keyword',self.id)
            samples,tree,entry,payload = self.finish_basic(samples,rows[0],payload,source)
            if phase=='all':
                self.recovery(binding,management,samples,tree,entry,payload)
            self.record('completed')
            self.summary['ok']=True
        except (VfsError,OSError,ValueError) as error:
            self.record('stopped',error_code=getattr(error,'code','LOCAL_IO_ERROR'))
            self.summary.update(ok=False,stopped_at_step=self.step,error_code=getattr(error,'code','LOCAL_IO_ERROR'))
        return self.summary

    @classmethod
    def from_predispatch_move_checkpoint(cls, config_path, journal_path, *, progress=None, _events=None):
        from quark_vfs.core.catalog import secure_bytes
        raw, _ = secure_bytes(Path(journal_path), Path(journal_path).parent, max_bytes=4194304)
        events = _events if _events is not None else [json.loads(line) for line in raw.splitlines()]
        markers = [i for i,e in enumerate(events) if e.get('event')=='resume_after_search_checkpoint']
        if len(markers)!=1:
            raise VfsError('INVALID_CHECKPOINT','Expected one consumed search checkpoint.')
        obj = cls.from_search_checkpoint(config_path,journal_path,progress=progress,_events=events[:markers[0]])
        tail = events[markers[0]+1:]
        commands = ['list','list','list','download','search','upload','list','download','list','list','mkdir','move']
        if len(tail)!=25 or tail[-1].get('event')!='stopped':
            raise VfsError('INVALID_CHECKPOINT','Only the exact predispatch move stop is accepted.')
        for i, command in enumerate(commands):
            intent,response = tail[2*i:2*i+2]
            step = str(obj.sequence+i+1)+'-'+command
            if intent.get('event')!='intent' or response.get('event')!='response' or intent['step']!=step or response['step']!=step or intent['argv'][2]!=command:
                raise VfsError('INVALID_CHECKPOINT','Continuation sequence differs.')
            result = response['result']
            if i<11 and result.get('ok') is not True:
                raise VfsError('INVALID_CHECKPOINT','Successful predispatch prefix differs.')
            if i==11 and (result.get('ok') is not False or result.get('error',{}).get('code')!='UPSTREAM_FAILED' or result.get('data')!={} or result.get('error',{}).get('job_id')):
                raise VfsError('INVALID_CHECKPOINT','Unknown mutation outcomes are not resumable.')
        obj.sequence +=12
        obj.step = tail[-1]['step']
        obj._checkpoint_destination = tail[21]['result']['data']
        if obj._checkpoint_destination['name']!='destination' or obj._checkpoint_destination['kind']!='directory':
            raise VfsError('INVALID_CHECKPOINT','Dedicated destination differs.')
        obj.summary['resume_mode']='strict_zero_dispatch_move_checkpoint'
        return obj

    def resume_predispatch_move(self, phase):
        from quark_vfs.models import same_browse_identity
        self.summary['phase']=phase
        self.record('resume_predispatch_move_checkpoint',reason='confirmed_pre_job_read_failure_zero_mutation')
        try:
            store = Store(self.config.state_dir/'quark-vfs.sqlite3')
            try:
                binding = store.binding(self.config.account)
                management = store.get_document(self.config.account,'management','root')
                jobs = store.jobs(self.config.account)
            finally:
                store.close()
            if not management:
                raise VfsError('INVALID_CHECKPOINT','Existing namespace binding is required.')
            samples = self.unique(management['fid'],'M1-samples-'+self.id)
            rows = self.children(samples['fid'])
            if {r['name'] for r in rows}!={self._checkpoint_source.name,'tree-'+self.id,'destination'} or len(rows)!=3:
                raise VfsError('INVALID_CHECKPOINT','Source/tree/destination checkpoint changed.')
            source = next(r for r in rows if r['name']==self._checkpoint_source.name)
            tree = next(r for r in rows if r['name']=='tree-'+self.id)
            destination = next(r for r in rows if r['name']=='destination')
            if (source['kind']!='file' or source['size']!=len(b'M1 synthetic document\n')
                    or tree['kind']!='directory' or destination['kind']!='directory'
                    or not same_browse_identity(destination['fid'],self._checkpoint_destination['fid'])
                    or self.children(destination['fid'])):
                raise VfsError('INVALID_CHECKPOINT','Destination identity or empty state changed.')
            for job in jobs:
                if job['action']=='move' and any(same_browse_identity(item['input'].get('source',{}).get('fid',''),source['fid']) for item in job['items']):
                    raise VfsError('RECONCILIATION_REQUIRED','A source move job exists; this is not a zero-dispatch checkpoint.')
            payload = b'M1 synthetic document\n'
            self.download_equal(source,payload)
            nested = self.unique(tree['fid'],'nested.md')
            self.download_equal(nested,b'synthetic nested content\n')
            samples,tree,entry,payload = self.finish_after_destination(samples,tree,source,payload,self._checkpoint_source,destination)
            if phase=='all':
                self.recovery(binding,management,samples,tree,entry,payload)
            self.record('completed')
            self.summary['ok']=True
        except (VfsError,OSError,ValueError) as error:
            self.record('stopped',error_code=getattr(error,'code','LOCAL_IO_ERROR'))
            self.summary.update(ok=False,stopped_at_step=self.step,error_code=getattr(error,'code','LOCAL_IO_ERROR'))
        return self.summary

    @classmethod
    def from_restore_checkpoint(cls, config_path, journal_path, *, progress=None):
        import hashlib

        from quark_vfs.core.catalog import secure_bytes
        raw, _ = secure_bytes(Path(journal_path),Path(journal_path).parent,max_bytes=4194304)
        events = [json.loads(line) for line in raw.splitlines()]
        markers = [i for i,e in enumerate(events) if e.get('event')=='resume_predispatch_move_checkpoint']
        if len(markers)!=1:
            raise VfsError('INVALID_CHECKPOINT','Expected one consumed zero-dispatch move checkpoint.')
        obj = cls.from_predispatch_move_checkpoint(config_path,journal_path,progress=progress,_events=events[:markers[0]])
        tail = events[markers[0]+1:]
        commands = ['list','list','list','download','list','download','move','list','rename','download',
                    'upload','list','download','upload','list','upload','jobs','jobs','list','download','list','trash','restore']
        if len(tail)!=48 or tail[-2].get('event')!='stopped' or tail[-1].get('event')!='restore_payload_readonly_verified':
            raise VfsError('INVALID_CHECKPOINT','Only the verified pre-intent restore failure is accepted.')
        permitted_failures = {10:'PARTIAL_FAILURE',15:'PARTIAL_FAILURE',16:'NAME_CONFLICT',17:'PARTIAL_FAILURE',22:'EXPECTED_CHANGED'}
        for i,command in enumerate(commands):
            intent,response=tail[2*i:2*i+2]
            step=str(obj.sequence+i+1)+'-'+command
            result=response.get('result',{})
            if intent.get('event')!='intent' or response.get('event')!='response' or intent['step']!=step or response['step']!=step or intent['argv'][2]!=command:
                raise VfsError('INVALID_CHECKPOINT','Restore prefix differs.')
            if i in permitted_failures:
                local_partial_read = (i == 17 and result.get('ok') is True
                                      and result.get('data', {}).get('state') == 'partial'
                                      and result.get('data', {}).get('local_only') is True)
                if not local_partial_read and (result.get('ok') is not False or result.get('error',{}).get('code')!=permitted_failures[i]):
                    raise VfsError('INVALID_CHECKPOINT','Expected prefix failure differs.')
            elif result.get('ok') is not True:
                raise VfsError('INVALID_CHECKPOINT','Successful prefix differs.')
        if (not set(tail[45]['result'].get('data',{})) <= {'operation_id'}
                or tail[45]['result'].get('error',{}).get('job_id')):
            raise VfsError('INVALID_CHECKPOINT','A dispatched recovery job is not resumable.')
        trashed=tail[43]['result']['data']
        evidence=tail[-1]
        rows=trashed['manifest']['entries']
        if (len(rows)!=1 or evidence.get('operation_id')!=trashed['operation_id']
                or evidence.get('entry_id')!=rows[0]['entry_id'] or evidence.get('mutation_dispatched') is not False
                or evidence.get('bytes_equal_known_source') is not True or evidence.get('size')!=22
                or evidence.get('sha256')!=hashlib.sha256(b'M1 synthetic document\n').hexdigest()):
            raise VfsError('INVALID_CHECKPOINT','Independently verified source digest is absent.')
        obj._checkpoint_trashed=trashed
        obj.sequence+=23
        obj.step=tail[-2]['step']
        obj.summary['resume_mode']='strict_known_payload_pre_intent_restore_checkpoint'
        return obj

    def resume_restore(self, phase):
        import hashlib

        from quark_vfs.core.catalog import Catalog
        from quark_vfs.core.operations import Operations
        from quark_vfs.core.transfers import Transfers
        from quark_vfs.core.trash import (
            CoreTrashBackend,
            StoreTrashJournal,
            TrashService,
        )
        from quark_vfs.provider.quark import QuarkProvider
        self.summary['phase']=phase
        self.record('resume_restore_checkpoint',reason='known_source_digest_verified_post_trash_metadata_observation')
        try:
            binding,management=self.scope()
            samples=self.unique(management['fid'],'M1-samples-'+self.id)
            tree=self.unique(samples['fid'],'tree-'+self.id)
            destination=self.unique(samples['fid'],'destination')
            trashed=self._checkpoint_trashed
            row=trashed['manifest']['entries'][0]
            payload=b'M1 synthetic document\n'
            store=Store(self.config.state_dir/'quark-vfs.sqlite3')
            try:
                with store.account_lock(self.config.account):
                    catalog=Catalog(QuarkProvider(self.config),store,self.local/'restore-observation')
                    operations=Operations(catalog,store)
                    transfers=Transfers(catalog,store,self.local/'restore-observation',excluded_local_roots=(self.config.state_dir,self.config.cache_dir,self.config.official_root))
                    backend=CoreTrashBackend(catalog,operations,transfers,account=self.config.account,workspace=self.local/'restore-observation')
                    service=TrashService(backend,StoreTrashJournal(store),account=self.config.account,trash_root_fid=self.config.trash_root_fid,scope_uuid=binding.scope_uuid)
                    service._observe_checkpoint_payload(trashed['operation_id'],row['entry_id'],hashlib.sha256(payload).hexdigest())
            finally:
                store.close()
            renamed={'name':row['original_name']}
            samples,tree,entry,payload=self.finish_after_trash(samples,tree,renamed,payload,destination,trashed)
            if phase=='all':
                self.recovery(binding,management,samples,tree,entry,payload)
            self.record('completed')
            self.summary['ok']=True
        except (VfsError,OSError,ValueError) as error:
            self.record('stopped',error_code=getattr(error,'code','LOCAL_IO_ERROR'))
            self.summary.update(ok=False,stopped_at_step=self.step,error_code=getattr(error,'code','LOCAL_IO_ERROR'))
        return self.summary

    def record(self, event: str, **facts):
        path = self.local / 'journal.jsonl'
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps({'event': event, 'step': self.step, **facts}) + '\n').encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def call(self, command: str, *arguments: str, config_path: Path | None = None,
             expected_error: str | None = None):
        self.sequence += 1
        self.step = str(self.sequence) + '-' + command
        argv = ['--config', str(config_path or self.config_path), command, *arguments]
        self.record('intent', argv=argv)
        if self.progress is not None:
            self.progress({'step': self.sequence, 'command': command, 'status': 'started'})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli_main(argv)
        result = json.loads(output.getvalue())
        self.record('response', result=result)
        if self.progress is not None:
            self.progress({'step': self.sequence, 'command': command,
                           'status': 'passed' if code == 0 and result.get('ok') else 'expected_failure' if expected_error is not None else 'failed'})
        if expected_error is not None:
            item_errors = [item.get('result', {}).get('error_code')
                           for item in result.get('data', {}).get('items', [])
                           if isinstance(item.get('result'), dict)]
            if code != 1 or (result.get('error', {}).get('code') != expected_error
                             and not item_errors == [expected_error]):
                raise VfsError('ACCEPTANCE_FAILED', 'Expected conflict was not observed; no retry is allowed.')
            return result
        if code != 0 or result.get('ok') is not True:
            raise VfsError(result.get('error', {}).get('code', 'OUTCOME_UNKNOWN'), 'CLI acceptance stopped; inspect private intent and response records.')
        data = result['data']
        if not (command == 'jobs' and '--resume' not in arguments) and isinstance(data, dict) and data.get('state') in ('failed', 'partial', 'needs_reconciliation'):
            raise VfsError('OUTCOME_UNKNOWN', 'A batch did not fully complete; do not replay successful items.')
        return data

    @staticmethod
    def ref(entry: dict) -> str:
        return json.dumps({'fid': entry['fid'], 'parent_fid_hint': entry['parent_fid']})

    def parent_ref(self, fid: str):
        return json.dumps({'fid': fid})

    def children(self, fid: str, *, config_path: Path | None = None):
        page = self.call('list', '--ref', self.parent_ref(fid), config_path=config_path)
        if page['next_page_token'] is not None or page['complete'] is not True:
            raise VfsError('ACCEPTANCE_FAILED', 'Synthetic directory listing was not complete.')
        return page['entries']

    def unique(self, fid: str, name: str, *, config_path: Path | None = None):
        matches = [entry for entry in self.children(fid, config_path=config_path) if entry['name'] == name]
        if len(matches) != 1:
            raise VfsError('OUTCOME_UNKNOWN', 'Synthetic object is not unique.')
        return matches[0]

    def download_equal(self, entry: dict, expected: bytes, *, config_path: Path | None = None):
        target = self.local / ('download-' + str(uuid.uuid4()) + '.bin')
        self.call('download', '--ref', self.ref(entry), '--local-target', str(target), config_path=config_path)
        if target.read_bytes() != expected:
            raise VfsError('CONTENT_MISMATCH', 'Synthetic payload bytes differ.')

    def upload(self, fid: str, path: Path):
        return self.call('upload', '--parent', self.parent_ref(fid), '--local-path', str(path))

    def scope(self):
        store = Store(self.config.state_dir / 'quark-vfs.sqlite3')
        try:
            try:
                binding = store.binding(self.config.account)
            except VfsError as error:
                if error.code != 'ACCOUNT_UNBOUND' or self.parent is None:
                    raise
                self.call('init-scope', '--parent-fid', self.parent)
                binding = store.binding(self.config.account)
            management = store.get_document(self.config.account, 'management', 'root')
            if management is None:
                raise VfsError('LOCATION_UNKNOWN', 'Existing binding lacks project management root.')
            return binding, management
        finally:
            store.close()

    def basic(self, management: dict):
        samples = self.call('mkdir', '--parent', self.parent_ref(management['fid']), '--name', 'M1-samples-' + self.id)
        if self.children(samples['fid']):
            raise VfsError('NAME_CONFLICT', 'Synthetic samples directory is not empty.')
        source = self.local / ('source-' + self.id + '.md')
        payload = b'M1 synthetic document\n'
        source.write_bytes(payload)
        self.upload(samples['fid'], source)
        entry = self.unique(samples['fid'], source.name)
        self.download_equal(entry, payload)
        self.call('stat', '--ref', self.ref(entry))
        self.call('search', '--keyword', self.id)
        return self.finish_basic(samples, entry, payload, source)

    def finish_basic(self, samples, entry, payload, source):
        directory = self.local / ('tree-' + self.id)
        directory.mkdir(mode=0o700)
        (directory / 'nested.md').write_bytes(b'synthetic nested content\n')
        self.upload(samples['fid'], directory)
        tree = self.unique(samples['fid'], directory.name)
        output = self.local / ('tree-download-' + str(uuid.uuid4()))
        self.call('download', '--ref', self.ref(tree), '--local-target', str(output))
        if (output / 'nested.md').read_bytes() != (directory / 'nested.md').read_bytes():
            raise VfsError('CONTENT_MISMATCH', 'Directory payload differs.')
        page = self.call('list', '--ref', self.parent_ref(samples['fid']), '--page-size', '1')
        if page['next_page_token'] is None:
            raise VfsError('ACCEPTANCE_FAILED', 'Snapshot pagination token absent.')
        second = self.call('list', '--ref', self.parent_ref(samples['fid']), '--page-size', '1', '--page-token', page['next_page_token'])
        if page['snapshot_id'] != second['snapshot_id']:
            raise VfsError('ACCEPTANCE_FAILED', 'Local page token changed its snapshot.')
        destination = self.call('mkdir', '--parent', self.parent_ref(samples['fid']), '--name', 'destination')
        return self.finish_after_destination(samples, tree, entry, payload, source, destination)

    def finish_after_destination(self, samples, tree, entry, payload, source, destination):
        self.call('move', '--ref', self.ref(entry), '--target', self.parent_ref(destination['fid']))
        moved = self.unique(destination['fid'], source.name)
        renamed = self.call('rename', '--ref', self.ref(moved), '--name', 'renamed-' + self.id + '.md')
        self.download_equal(renamed, payload)
        collision = self.local / renamed['name']
        collision.write_bytes(b'separate synthetic collision\n')
        self.call('upload', '--parent', self.parent_ref(destination['fid']), '--local-path', str(collision), expected_error='NAME_CONFLICT')
        self.download_equal(self.unique(destination['fid'], renamed['name']), payload)
        self.call('upload', '--parent', self.parent_ref(destination['fid']), '--local-path', str(collision), '--conflict', 'keep_both')
        rows = self.children(destination['fid'])
        if len(rows) != 2 or len({row['name'] for row in rows}) != 2:
            raise VfsError('ACCEPTANCE_FAILED', 'keep_both did not preserve distinct objects.')
        extra = self.local / ('batch-success-' + self.id + '.md')
        extra.write_bytes(b'one successful batch item\n')
        partial = self.call('upload', '--parent', self.parent_ref(destination['fid']),
                            '--local-path', str(extra), '--local-path', str(collision),
                            expected_error='PARTIAL_FAILURE')['data']
        if partial.get('state') != 'partial' or len(partial.get('items', [])) != 2:
            raise VfsError('ACCEPTANCE_FAILED', 'Mixed synthetic batch was not safely partitioned.')
        completed = [item for item in partial['items'] if item['state'] == 'succeeded']
        if len(completed) != 1:
            raise VfsError('ACCEPTANCE_FAILED', 'Successful batch item was not recorded.')
        self.call('jobs', '--job-id', partial['job_id'], '--resume', expected_error='NAME_CONFLICT')
        resumed = self.call('jobs', '--job-id', partial['job_id'])
        if resumed.get('state') != 'partial' or resumed.get('local_only') is not True:
            raise VfsError('ACCEPTANCE_FAILED', 'Local history did not preserve the partial job state.')
        retained = [item for item in resumed['items'] if item['state'] == 'succeeded']
        if retained != completed:
            raise VfsError('ACCEPTANCE_FAILED', 'Resume changed an already successful item.')
        self.download_equal(self.unique(destination['fid'], extra.name), extra.read_bytes())
        self.summary['checks']['wrapper_batch_partial_resume_preserves_success'] = 'passed'
        trashed = self.call('trash', '--ref', self.ref(self.unique(destination['fid'], renamed['name'])))
        return self.finish_after_trash(samples, tree, renamed, payload, destination, trashed)

    def finish_after_trash(self, samples, tree, renamed, payload, destination, trashed):
        self.call('restore', '--operation-id', trashed['operation_id'], '--entry-id', trashed['manifest']['entries'][0]['entry_id'])
        self.download_equal(self.unique(destination['fid'], renamed['name']), payload)
        self.summary['checks'].update(file_roundtrip='passed', directory_roundtrip='passed', snapshot_pages='passed',
                                      stat='passed', search='executed_no_completeness_claim', move_rename='passed',
                                      conflict_fail_keep_both='passed', manifest_trash_restore='passed',
                                      upload_mixed_partial='not_tested_real_service_mock_coverage_only',
                                      search_scope='global_unique_uuid_candidates_not_complete',
                                      parent_filtered_search='upstream21001_not_supported_in_this_environment')
        return samples, tree, self.unique(destination['fid'], renamed['name']), payload

    def write_config(self, target: Path, state: Path, cache: Path, *, create_directories: bool):
        config = self.config
        runtime = {'node_executable': str(config.node_executable), 'official_entry': str(config.official_entry),
                   'timeout_seconds': config.timeout_seconds, 'max_output_bytes': config.max_output_bytes,
                   'login_timeout_seconds': config.login_timeout_seconds}
        if config.expected_version is not None:
            runtime['expected_version'] = config.expected_version
        account = {'name': config.account}
        if config.trash_root_fid is not None:
            account['trash_root_fid'] = config.trash_root_fid
        if config.trash_root_parent_fid is not None:
            account['trash_root_parent_fid'] = config.trash_root_parent_fid
        data = {'runtime': runtime, 'local': {'state_dir': str(state), 'cache_dir': str(cache)},
                'account': account,
                'features': {'file_operations_enabled': config.file_operations_enabled,
                             'trash_enabled': config.trash_enabled, 'mcp_verified': False,
                             'archive_replace_verified': False, 'download_resume_verified': False}}
        if create_directories:
            state.mkdir(mode=0o700)
            cache.mkdir(mode=0o700)
        text = '\n'.join('[' + section + ']\n' + '\n'.join(key + ' = ' + json.dumps(value) for key, value in fields.items())
                         for section, fields in data.items())
        target.write_text(text)
        os.chmod(target, 0o600)
        return target

    def configure_dedicated_trash(self, management):
        directory = self.call('mkdir', '--parent', self.parent_ref(management['fid']),
                              '--name', 'M1-trash-' + self.id)
        from dataclasses import replace
        self.config = replace(self.config, trash_root_fid=directory['fid'],
                              trash_root_parent_fid=management['fid'])
        self.config_path = self.write_config(self.local / 'validation.toml', self.config.state_dir,
                                             self.config.cache_dir, create_directories=False)
        if self.children(directory['fid']):
            raise VfsError('NAME_CONFLICT', 'Dedicated acceptance trash root is not empty.')

    def recovered_config(self):
        return self.write_config(self.local / 'rebuilt.toml', self.local / 'rebuilt-state',
                                 self.local / 'rebuilt-cache', create_directories=True)

    def recovery(self, binding, management, samples, tree, entry, payload):
        state = self.call('trash', '--ref', self.ref(entry), '--ref', self.ref(tree))
        first, remaining = state['manifest']['entries']
        self.call('restore', '--operation-id', state['operation_id'], '--entry-id', first['entry_id'])
        rebuilt = self.recovered_config()
        parent = management.get('parent_fid')
        if not isinstance(parent, str):
            raise VfsError('LOCATION_UNKNOWN', 'Explicit management parent is absent; no full-drive scan is allowed.')
        self.call('bind-scope', '--scope-uuid', binding.scope_uuid, '--management-parent-fid', parent, config_path=rebuilt)
        result = self.call('rebuild-trash', config_path=rebuilt)
        operations = [row for row in result['operations'] if row['operation_id'] == state['operation_id']]
        if len(operations) != 1:
            raise VfsError('ACCEPTANCE_FAILED', 'Rebuilt operation absent.')
        observed = {row['entry_id']: row['status'] for row in operations[0]['entries']}
        if observed.get(first['entry_id']) != 'restored' or observed.get(remaining['entry_id']) != 'trashed':
            raise VfsError('ACCEPTANCE_FAILED', 'Rebuilt recovery history or remaining payload is incorrect.')
        self.call('restore', '--operation-id', state['operation_id'], '--entry-id', remaining['entry_id'], config_path=rebuilt)
        restored_tree = self.unique(samples['fid'], tree['name'], config_path=rebuilt)
        target = self.local / ('restored-tree-' + str(uuid.uuid4()))
        self.call('download', '--ref', self.ref(restored_tree), '--local-target', str(target), config_path=rebuilt)
        if (target / 'nested.md').read_bytes() != b'synthetic nested content\n':
            raise VfsError('CONTENT_MISMATCH', 'Rebuilt directory restore differs.')
        self.summary['checks']['database_loss_rebuild_restored_history_and_directory'] = 'passed'

    def run(self, phase: str):
        self.summary['phase'] = phase
        try:
            binding, management = self.scope()
            self.configure_dedicated_trash(management)
            samples, tree, entry, payload = self.basic(management)
            if phase == 'all':
                self.recovery(binding, management, samples, tree, entry, payload)
            self.record('completed')
            self.summary['ok'] = True
        except (VfsError, OSError, ValueError) as error:
            self.record('stopped', error_code=getattr(error, 'code', 'LOCAL_IO_ERROR'))
            self.summary.update(ok=False, stopped_at_step=self.step, error_code=getattr(error, 'code', 'LOCAL_IO_ERROR'))
        return self.summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--parent-fid', help='Explicit parent only when creating a new unbound namespace')
    parser.add_argument('--phase', choices=('basic', 'all'), default='all')
    parser.add_argument('--resume-after-search', type=Path, help='Resume only a journal that stopped at read-only search after the verified source prefix')
    parser.add_argument('--resume-predispatch-move', type=Path, help='Only the proven pre-job step-22 failure; never an unknown write')
    parser.add_argument('--resume-verified-restore', type=Path, help='Only the known synthetic payload verified before any restore intent')
    args = parser.parse_args(argv)
    if sum(x is not None for x in (args.resume_after_search,args.resume_predispatch_move,args.resume_verified_restore))>1:
        parser.error('Choose one strict checkpoint')
    try:
        def progress(event):
            print(json.dumps(event), flush=True)
        if args.resume_verified_restore is not None:
            harness=M1Acceptance.from_restore_checkpoint(args.config,args.resume_verified_restore,progress=progress)
            result=harness.resume_restore(args.phase)
        elif args.resume_predispatch_move is not None:
            harness = M1Acceptance.from_predispatch_move_checkpoint(args.config,args.resume_predispatch_move,progress=progress)
            result = harness.resume_predispatch_move(args.phase)
        elif args.resume_after_search is not None:
            harness = M1Acceptance.from_search_checkpoint(args.config, args.resume_after_search, progress=progress)
            result = harness.resume_after_search(args.phase)
        else:
            result = M1Acceptance(args.config, parent_fid=args.parent_fid, progress=progress).run(args.phase)
    except (VfsError, OSError, ValueError) as error:
        result = {'ok': False, 'error_code': getattr(error, 'code', 'LOCAL_IO_ERROR'), 'automatic_retries': 0}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

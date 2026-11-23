"""Durable request identities keep a disconnected client from submitting the same mutation twice.

Workers use the existing judge and source locks. A process interruption is uncertain, never an automatic retry:
callers inspect the current source and history before choosing a new request key. Historical results are not
cached correctness verdicts for changed inputs or builds.
"""

import hashlib
import json
import re
import threading
import time
import uuid
from collections import deque
from contextlib import nullcontext

from ..network import NetworkError
from . import compile_tools, specialists

ACTIVE = ('queued', 'validating', 'building_and_judging')
MAX_PENDING = 8
MAX_WORKERS = 2
LEASE_SECONDS = 30
MAX_PAYLOAD = 128 * 1024
_local = threading.local()


def _owned(net, agent, fn):
    """Validate and renew ownership in one transaction, including while other workers renew leases."""
    now = time.time()
    ttl = getattr(net, 'claim_ttl', 300)
    with net.store.tx() as db:
        held = db.execute('SELECT agent_id,expires_at FROM claims WHERE function_id=?', (fn['id'],)).fetchone()
        if not held or held['agent_id'] != agent.id or (held['expires_at'] or 0) <= now:
            raise NetworkError('Hold an unexpired source claim before starting a compile operation.')
        db.execute('UPDATE claims SET expires_at=MAX(expires_at,?) WHERE function_id=? AND agent_id=?',
                   (now + ttl, fn['id'], agent.id))


class _UnitQueue:
    """Let independent units use bounded workers without filling them with waits on the same unit."""
    def __init__(self):
        self.condition = threading.Condition()
        self.pending = deque()
        self.busy = set()
        self.unfinished = 0
        self.closed = False

    def put(self, item):
        with self.condition:
            self.pending.append(item)
            self.unfinished += 1
            self.condition.notify_all()

    def get(self):
        with self.condition:
            while True:
                for item in self.pending:
                    if item[3] not in self.busy:
                        self.pending.remove(item)
                        self.busy.add(item[3])
                        return item
                if self.closed and not self.pending:
                    return None
                self.condition.wait()

    def task_done(self, key):
        with self.condition:
            self.busy.remove(key)
            self.unfinished -= 1
            self.condition.notify_all()

    def join(self):
        with self.condition:
            self.condition.wait_for(lambda: self.unfinished == 0)

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


def _request_hash(payload, function_id):
    """Diagnostic snapshots are not caller arguments, including in records made before this fix."""
    request = json.loads(payload)
    request.pop('parts', None)
    request.pop('function', None)
    request['function_id'] = function_id
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


def _same_request(prior, digest):
    if _request_hash(prior['payload'], prior['function_id']) != digest:
        raise NetworkError('This request_id already identifies different arguments. Read its status or use a new key.')
    return prior['id']


class Operations:
    def __init__(self, net):
        self.net = net
        self.instance = uuid.uuid4().hex
        self.queue = _UnitQueue()
        self.stopped = threading.Event()
        self.changed = threading.Condition()
        with net.store.tx() as db:
            db.execute("UPDATE compile_operations SET state='interrupted',finished_at=?,error=? "
                       "WHERE state IN ('queued','validating','building_and_judging')",
                       (time.time(), 'The previous process ended. Inspect current source and history; do not replay automatically.'))
        self.workers = [threading.Thread(target=self._run, daemon=True, name=f'compile-operations-{n}')
                        for n in range(MAX_WORKERS)]
        for worker in self.workers:
            worker.start()
        self.lease = threading.Thread(target=self._keep_claims, daemon=True, name='compile-operation-claims')
        self.lease.start()

    def close(self):
        """Stop idle service threads after draining work (also used by isolated test networks)."""
        self.queue.join()
        self.stopped.set()
        self.queue.close()
        for worker in self.workers:
            worker.join(2)
        self.lease.join(2)

    def _renew_claims(self):
        """Keep only existing, unexpired source ownership alive; never resurrect a released or reassigned claim."""
        now = time.time()
        ttl = getattr(self.net, 'claim_ttl', 300)
        with self.net.store.tx() as db:
            db.execute("UPDATE claims SET expires_at=MAX(expires_at,?) WHERE expires_at>? AND EXISTS ("
                       "SELECT 1 FROM compile_operations o WHERE o.function_id=claims.function_id "
                       "AND o.agent_id=claims.agent_id AND o.instance=? "
                       "AND o.state IN ('queued','validating','building_and_judging'))",
                       (now + ttl, now, self.instance))

    def _keep_claims(self):
        interval = min(LEASE_SECONDS, getattr(self.net, 'claim_ttl', 300) / 3)
        while not self.stopped.wait(max(0.01, interval)):
            try:
                if self.queue.unfinished:
                    self._renew_claims()
            except Exception:
                import logging
                logging.getLogger('decompdecomp').exception('could not renew active compile-operation claims')

    def _run(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            operation, agent, slug, key = item
            _local.operation = operation
            try:
                row = self.net.store.one('SELECT * FROM compile_operations WHERE id=?', operation)
                self._update(operation, 'validating', started=True)
                fn = self.net.store.one('SELECT * FROM functions WHERE id=?', row['function_id'])
                if fn is None:
                    raise NetworkError('The function was removed before this operation started.')
                _owned(self.net, agent, fn)
                specialists.guard(self.net, agent, slug, 'compile_start')
                args = json.loads(row['payload'])
                stored_parts = args.pop('parts', None)
                action = args.pop('action')
                region_id = args.pop('region_id', 0)
                from . import region_work
                proj, _, _ = self.net.forks.get(slug)
                with self.net.forks._tree(proj).exclusive() if region_id else nullcontext():
                    baseline = None
                    if region_id and action in {'patch', 'definition'}:
                        _, current_source, current_revision = compile_tools.snapshot(self.net.forks, slug, args['function'])
                        if current_revision != args['revision']:
                            raise NetworkError('Stale regional revision: refresh the brief and record a new trial.')
                        preview = self.net.forks._attempt(agent, slug, args['function'], current_source, probe=True)
                        if preview.get('score') is None:
                            raise NetworkError('Cannot establish a fresh regional baseline; no edit was applied.')
                        baseline = preview.get('rows')
                    self._update(operation, 'building_and_judging')
                    if action == 'patch':
                        result = compile_tools.compile_patch(self.net, agent, slug, **args)
                    elif action == 'recipes':
                        result = compile_tools.try_recipes(self.net, agent, slug, **args)
                    elif action == 'diagnostics':
                        from . import compiler_diagnostics
                        result = compiler_diagnostics.run(self.net, agent, slug, **args)
                    else:
                        revision = args.pop('revision')
                        # The unit's own locks, as compile_patch: the revision check and the compile are one step for
                        # this unit, while other units build alongside (only a unit linked into the game takes the
                        # project to itself). The whole tree held for every definition stalled every other agent.
                        with compile_tools._locked(self.net.forks, slug, args['function']):
                            try:
                                current = compile_tools.snapshot(self.net.forks, slug, args['function'])[2]
                            except NetworkError as exc:
                                if 'Add the definition' not in str(exc):
                                    raise
                                current = ''
                            if current != revision:
                                try:
                                    now = compile_tools.revision_parts(self.net.forks, slug, args['function'])[3]
                                except NetworkError:  # no definition yet: say what moved as far as it can
                                    now = {}
                                moved = sorted(k for k in set(now) | set(stored_parts or {})
                                               if (now.get(k) != (stored_parts or {}).get(k)))
                                raise NetworkError(
                                    'Stale revision: ' + (', '.join(moved) + ' changed since the brief'
                                                          if moved else 'refresh function_brief')
                                    + ' (refresh function_brief before submitting a new request key).')
                            result = self.net.compile_and_diff(agent, args['function'], args['source'], slug)
                    if region_id:
                        try:
                            region_work.complete(self.net, agent, slug, region_id, operation, baseline, result)
                        except Exception:  # advisory analysis must not obscure a completed ordinary judge result
                            import logging
                            logging.getLogger('decompdecomp').exception('regional comparison for %s failed', operation)
                            result = str(result) + '\nRegional comparison unavailable; the judge result above is unchanged.' 
                self._update(operation, 'completed', result=str(result))
            except Exception as exc:  # preserve the operation's outcome even when a tool or compiler fails unexpectedly
                import logging
                logging.getLogger('decompdecomp').exception('compile operation %s failed', operation)
                error = str(exc) if isinstance(exc, NetworkError) else 'Unexpected tool failure; inspect the app log and current source before retrying.'
                self._update(operation, 'failed', error=error)
                with self.net.store.tx() as db:
                    db.execute("UPDATE region_experiments SET outcome='blocked',findings=?,updated_at=? WHERE operation_id=?",
                               (error, time.time(), operation))
            finally:
                _local.operation = None
                self.queue.task_done(key)

    def _update(self, operation, state, result='', error='', started=False):
        with self.net.store.tx() as db:
            db.execute('UPDATE compile_operations SET state=?,result=?,error=?,started_at=COALESCE(started_at,?),'
                       'finished_at=? WHERE id=?', (state, result, error, time.time() if started else None,
                                                  time.time() if state in ('completed', 'failed') else None, operation))
        with self.changed:
            self.changed.notify_all()


def manager(net):
    with net.store.lock:
        if not hasattr(net, '_compile_operations'):
            net._compile_operations = Operations(net)
        return net._compile_operations


def start(net, agent, slug, function, request_id, revision='', action='patch', edits=None, source='', recipes=None,
          apply=False, region_id=0):
    """Reserve one mutation identity before queuing; a repeated key returns its original operation only."""
    if isinstance(function, str) and ',' in function:
        raise NetworkError('compile_start accepts one function. For a batch, use compile_and_diff with the comma-separated functions and their complete definitions.')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', request_id):
        raise NetworkError('Use a stable request_id of 1–80 letters, digits, dots, underscores or hyphens.')
    if action not in {'patch', 'definition', 'recipes', 'diagnostics'}:
        raise NetworkError('Choose patch, definition, recipes, or diagnostics.')
    if not isinstance(revision, str) or not isinstance(region_id, int) or region_id < 0:
        raise NetworkError('Supply a revision string and a nonnegative region_id.')
    proj, fp, _ = net.forks.get(slug)
    fn = net._function(function, slug)
    args = dict(function=fn['name'], revision=revision)
    if action == 'patch':
        if not isinstance(edits, list) or not 1 <= len(edits) <= 16:
            raise NetworkError('Supply between 1 and 16 exact edits for a patch.')
        args['edits'] = edits
    elif action == 'definition':
        if not isinstance(source, str) or not source.strip():
            raise NetworkError('Supply a complete source definition.')
        args['source'] = source
    elif action == 'recipes':
        if not isinstance(recipes, list) or not 1 <= len(recipes) <= 8 or not isinstance(apply, bool):
            raise NetworkError('Supply 1–8 recipes and a boolean apply value.')
        args.update(recipes=recipes, apply=apply)
    args.update(action=action, region_id=region_id)
    # The request key names caller arguments, not a new snapshot of a source the first request may have changed.
    digest = _request_hash(json.dumps(args, sort_keys=True), fn['id'])
    service = manager(net)
    prior = net.store.one('SELECT id,payload,function_id FROM compile_operations WHERE project_id=? AND agent_id=? AND request_key=?',
                          proj['id'], agent.id, request_id)
    if prior is not None:
        return status(net, agent, slug, _same_request(prior, digest), wait_ms=0)
    try:
        args['parts'] = compile_tools.revision_parts(net.forks, slug, fn['name'])[3]
    except Exception:  # a diagnostic only: absent definitions must still queue
        pass
    payload = json.dumps(args, sort_keys=True)
    if len(payload.encode()) > MAX_PAYLOAD:
        raise NetworkError('Compile request exceeds 128 KiB; use a scoped patch.')
    unit_key = (proj['id'], fn['unit'])
    if fp is not None:
        unit = fp.unit(fn['unit'])
        if unit.source:
            unit_key = (proj['id'], str(fp.safe_path(unit.source)))
    enqueue = False
    with net.store.tx() as db:
        prior = db.execute('SELECT id,payload,function_id FROM compile_operations WHERE project_id=? AND agent_id=? AND request_key=?',
                           (proj['id'], agent.id, request_id)).fetchone()
        if prior:
            operation = _same_request(prior, digest)
        else:
            _owned(net, agent, fn)
            active = db.execute("SELECT id FROM compile_operations WHERE state IN ('queued','validating','building_and_judging') "
                                "AND (function_id=? OR agent_id=?)", (fn['id'], agent.id)).fetchone()
            if active:
                raise NetworkError('An operation is still active; use compile_status(operation_id=' + repr(active['id']) + ').')
            count = db.execute("SELECT count(*) FROM compile_operations WHERE state IN ('queued','validating','building_and_judging')").fetchone()[0]
            if count >= MAX_PENDING:
                raise NetworkError('Compile queue is full. Wait briefly and retry the same request_id; no operation was created.')
            if region_id:
                from . import region_work
                region_work.bind(db, fn, agent, region_id, revision)
            operation = uuid.uuid4().hex
            db.execute('INSERT INTO compile_operations(id,project_id,function_id,agent_id,request_key,payload_hash,state,payload,instance,created_at) '
                       "VALUES(?,?,?,?,?,?,'queued',?,?,?)", (operation, proj['id'], fn['id'], agent.id, request_id, digest,
                                                             payload, service.instance, time.time()))
            if region_id:
                db.execute('UPDATE region_experiments SET operation_id=?,updated_at=? WHERE id=?', (operation, time.time(), region_id))
            enqueue = True
    if enqueue:
        service.queue.put((operation, agent, slug, unit_key))
    return status(net, agent, slug, operation, wait_ms=0)


def status(net, agent, slug, operation_id, wait_ms=5000):
    service = manager(net)  # mark uncertain operations left by a previous process; never replay them
    proj = net._project(slug)
    row = net.store.one('SELECT * FROM compile_operations WHERE id=? AND project_id=? AND agent_id=?',
                        str(operation_id), proj['id'], agent.id)
    if row is None:
        raise NetworkError('No compile operation with that ID for this agent and project.')
    if row['state'] in ACTIVE and wait_ms:
        wait_ms = max(0, min(10000, int(wait_ms)))
        previous_state = row['state']
        with service.changed:
            service.changed.wait_for(lambda: net.store.one('SELECT state FROM compile_operations WHERE id=?',
                                                           row['id'])['state'] != previous_state, wait_ms/1000)
        row = net.store.one('SELECT * FROM compile_operations WHERE id=?', row['id'])
    end = row['finished_at'] or time.time()
    out = [f"Operation {row['id']}: {row['state']}; elapsed {end-row['created_at']:.2f}s.",
           'Poll this ID; reuse the same request_id and identical arguments after a transport timeout. '
           'A completed result describes its original build, not the current source.']
    if row['result']:
        out.append(row['result'][:24000])
    if row['error']:
        out.append(row['error'])
    return '\n'.join(out)


def guard(net, agent, slug, tool_name):
    """Protect the caller's outstanding request; tree/unit locks serialize other owners.

    A project-global guard starves edits under concurrent helpers and refers callers
    to operation IDs they cannot inspect. Shared edits already acquire the tree's
    writer-priority exclusive lock; source edits/builds acquire tree and unit locks.
    Keep status private and only ask callers to poll their own pending operations.
    """
    if not getattr(net, '_compile_operations', None) or getattr(_local, 'operation', None) or tool_name not in {
            'compile_and_diff', 'compile_patch', 'try_recipes', 'edit_file', 'release_function', 'try_compiler',
            'make_readable', 'split_file', 'join_functions', 'edit_context', 'rename_symbol'}:
        return
    proj = net._project(slug)
    row = net.store.one("SELECT id FROM compile_operations WHERE project_id=? AND agent_id=? AND state IN ('queued','validating','building_and_judging') LIMIT 1", proj['id'], agent.id)
    if row:
        raise NetworkError('A compile operation is active. Poll compile_status before editing or releasing: ' + row['id'])

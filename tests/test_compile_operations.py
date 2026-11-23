"""Durable operations survive client retries without replaying an edit or bypassing the ordinary judge."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from decompnet.app import compile_operations as operations
from decompnet.app.forkengine import _TreeLock
from decompnet.network import NetworkError
from decompnet.store import Store


@pytest.fixture
def rig(tmp_path, monkeypatch):
    store = Store(tmp_path/'state.db')
    with store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,root,config) VALUES(1,'demo','demo',?,'{}')", (str(tmp_path),))
        db.execute("INSERT INTO agents(id,name,token_hash) VALUES(1,'owner','test-only')")
        db.execute("INSERT INTO functions(id,project_id,name,unit) VALUES(1,1,'work','unit')")
        db.execute('INSERT INTO claims VALUES(1,1,?,?)', (time.time(),time.time()+600))
    fn = store.one('SELECT * FROM functions WHERE id=1')
    proj = dict(id=1,slug='demo')
    tree = _TreeLock()
    engine = SimpleNamespace(get=lambda slug:(proj,None,{}),_tree=lambda proj:tree)
    net = SimpleNamespace(cfg=lambda proj: {"kind":"fork", "platform":"ps4"},
                          progress=SimpleNamespace(sync=lambda *a, **kw: None), store=store,forks=engine,_function=lambda name,slug:fn,_project=lambda slug:proj,_last_diffs={})
    owner = SimpleNamespace(id=1,name='owner')
    entered, release = threading.Event(), threading.Event()
    calls = []

    def compile_patch(*args, **kw):
        calls.append(kw)
        entered.set()
        assert release.wait(5)
        return 'ordinary strict judge: score12; not a match'

    monkeypatch.setattr(operations.compile_tools,'compile_patch',compile_patch)
    args = dict(function='work', request_id='trial-1', revision='rev', edits=[{'old':'1','new':'2'}])
    yield SimpleNamespace(net=net,owner=owner,entered=entered,release=release,calls=calls,args=args)
    release.set()
    if hasattr(net,'_compile_operations'):
        net._compile_operations.close()


def operation_id(rig):
    return rig.net.store.one('SELECT id FROM compile_operations')['id']


def test_start_returns_while_compile_is_blocked_and_retries_share_one_operation(rig):
    started=time.monotonic()
    result=operations.start(rig.net,rig.owner,'demo',**rig.args)
    assert time.monotonic()-started<1
    assert rig.entered.wait(2)
    again=operations.start(rig.net,rig.owner,'demo',**rig.args)
    assert operation_id(rig) in result and operation_id(rig) in again
    assert len(rig.calls)==1
    rig.release.set();rig.net._compile_operations.queue.join()
    text=operations.status(rig.net,rig.owner,'demo',operation_id(rig))
    assert 'completed' in text and 'ordinary strict judge' in text
    operations.start(rig.net,rig.owner,'demo',**rig.args)
    assert len(rig.calls)==1


def test_same_key_with_different_arguments_is_rejected(rig):
    operations.start(rig.net,rig.owner,'demo',**rig.args)
    with pytest.raises(NetworkError,match='different arguments'):
        operations.start(rig.net,rig.owner,'demo',**(rig.args|{'revision':'new'}))


def test_new_key_cannot_edit_while_a_previous_mutation_is_running(rig):
    operations.start(rig.net,rig.owner,'demo',**rig.args)
    with pytest.raises(NetworkError,match='still active'):
        operations.start(rig.net,rig.owner,'demo',**(rig.args|{'request_id':'trial-2'}))
    with pytest.raises(NetworkError,match='active'):
        operations.guard(rig.net,rig.owner,'demo','release_function')
    operations.guard(rig.net,rig.owner,'demo','inspect_blocks')


def test_results_are_private_to_the_submitting_agent_and_project(rig):
    operations.start(rig.net,rig.owner,'demo',**rig.args)
    with pytest.raises(NetworkError,match='No compile operation'):
        operations.status(rig.net,SimpleNamespace(id=2),'demo',operation_id(rig))
    rig.net._project=lambda slug:{'id':2}
    with pytest.raises(NetworkError,match='No compile operation'):
        operations.status(rig.net,rig.owner,'other',operation_id(rig))


def test_unclaimed_functions_cannot_enqueue_mutations(rig):
    with rig.net.store.tx() as db:db.execute('DELETE FROM claims')
    with pytest.raises(NetworkError,match='claim'):
        operations.start(rig.net,rig.owner,'demo',**rig.args)
    assert not rig.calls


def test_failed_job_is_durable_and_same_key_never_retries_it(rig,monkeypatch):
    def fail(*args,**kw):
        rig.calls.append(1)
        raise NetworkError('Stale revision; refresh')
    monkeypatch.setattr(operations.compile_tools,'compile_patch',fail)
    operations.start(rig.net,rig.owner,'demo',**rig.args)
    rig.net._compile_operations.queue.join()
    result=operations.start(rig.net,rig.owner,'demo',**rig.args)
    assert 'failed' in result and 'Stale revision' in result and rig.calls==[1]


def test_previous_process_work_is_interrupted_and_never_replayed(rig):
    payload=json.dumps(dict(action='patch',region_id=0,function='work',revision='rev',edits=rig.args['edits']),sort_keys=True)
    import hashlib
    with rig.net.store.tx() as db:
        db.execute("INSERT INTO compile_operations(id,project_id,function_id,agent_id,request_key,payload_hash,state,payload,instance,created_at) VALUES('old',1,1,1,'trial-1',?,'building_and_judging',?,'old-process',?)",(hashlib.sha256(payload.encode()).hexdigest(),payload,time.time()))
    result=operations.start(rig.net,rig.owner,'demo',**rig.args)
    assert 'interrupted' in result and not rig.calls


def test_queue_rechecks_claim_before_executing(rig):
    service=operations.manager(rig.net)
    # Hold the writer lock until both submission and claim removal commit, so the worker cannot observe a valid claim.
    with rig.net.store.lock:
        operations.start(rig.net,rig.owner,'demo',**rig.args)
        with rig.net.store.tx() as db:
            db.execute('DELETE FROM claims')
    service.queue.join()
    assert not rig.calls
    assert 'failed' in operations.status(rig.net,rig.owner,'demo',operation_id(rig))


def test_both_agent_interfaces_expose_the_durable_tools(rig):
    import asyncio

    from decompnet.agents.api import api_tool_schemas
    from decompnet.app import tools

    names={'compile_start','compile_status','record_region','region_history','compare_region',
           'check_integer_behavior','save_recipe','recipe_candidates'}
    schema_names={item['function']['name'] for item in api_tool_schemas(True)}
    assert names<=schema_names
    assert not names & {item['function']['name'] for item in api_tool_schemas(False)}
    mcp=tools.build_mcp(rig.net,lambda ctx:(rig.owner,'demo'))
    registered=asyncio.run(mcp.list_tools())
    assert names<={tool.name for tool in registered}
    rig.net.resolve_function=lambda agent,slug,function:function
    rig.net.fork_slug=lambda slug:slug
    result=asyncio.run(mcp.call_tool('compile_start',rig.args))
    assert not result.is_error
    assert operation_id(rig) in str(result.content)
    assert rig.entered.wait(2)
    rig.release.set();rig.net._compile_operations.queue.join()
    status=asyncio.run(mcp.call_tool('compile_status',{'operation_id':operation_id(rig)}))
    assert not status.is_error and 'completed' in str(status.content)


def test_unsupported_mutations_are_not_available_to_a_specialist(rig):
    from decompnet.app import tools

    rig.net.resolve_function=lambda agent,slug,function:function
    rig.net.fork_slug=lambda slug:slug
    rig.net._specialist_sessions={(rig.owner.id,'demo'):{'id':1,'reads':0}}
    result=tools.call(rig.net,rig.owner,'demo','compile_start',rig.args)
    assert 'advisory specialist' in result and not rig.calls


@pytest.mark.parametrize('tool', ['edit_file', 'release_function', 'compile_and_diff', 'compile_patch',
                                  'try_recipes', 'try_compiler', 'make_readable', 'split_file',
                                  'join_functions', 'edit_context', 'rename_symbol'])
def test_foreign_operation_does_not_block_other_owners(rig, tool):
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    other = SimpleNamespace(id=2, name='other')
    operations.guard(rig.net, other, 'demo', tool)
    with pytest.raises(NetworkError, match=operation_id(rig)):
        operations.guard(rig.net, rig.owner, 'demo', tool)
    with pytest.raises(NetworkError, match='No compile operation'):
        operations.status(rig.net, other, 'demo', operation_id(rig), wait_ms=0)


def test_guard_only_reports_callers_own_operation_when_foreign_operation_is_first(rig):
    service = operations.manager(rig.net)
    with rig.net.store.tx() as db:
        db.execute("INSERT INTO agents(id,name,token_hash) VALUES(2,'other','other-test')")
        db.execute("INSERT INTO functions(id,project_id,name,unit) VALUES(2,1,'other_work','other_unit')")
        db.execute("INSERT INTO compile_operations(id,project_id,function_id,agent_id,request_key,payload_hash,state,payload,instance,created_at) VALUES('foreign',1,2,2,'foreign','hash','queued','{}',?,?)",
                   (service.instance, time.time()))
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    with pytest.raises(NetworkError, match='active') as error:
        operations.guard(rig.net, rig.owner, 'demo', 'edit_file')
    # Polling advice must identify an operation this caller can actually inspect.
    own = rig.net.store.one('SELECT id FROM compile_operations WHERE agent_id=1')['id']
    assert own in str(error.value) and 'foreign' not in str(error.value)


def test_foreign_shared_edit_waits_for_build_and_precedes_new_readers(rig):
    tree = rig.net.forks._tree(None)
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    other = SimpleNamespace(id=2, name='other')
    edit_entered = threading.Event()
    edit_release = threading.Event()
    reader_entered = threading.Event()
    order = []

    def edit():
        operations.guard(rig.net, other, 'demo', 'edit_file')
        with tree.exclusive():
            order.append('edit')
            edit_entered.set()
            assert edit_release.wait(5)

    def reader():
        with tree.shared():
            order.append('build')
            reader_entered.set()

    with tree.shared():
        writer = threading.Thread(target=edit)
        writer.start()
        with tree._cond:
            assert tree._cond.wait_for(lambda: tree._waiting == 1, timeout=2)
        later = threading.Thread(target=reader)
        later.start()
        assert not edit_entered.is_set() and not reader_entered.is_set()
    try:
        assert edit_entered.wait(2)
        assert not reader_entered.is_set()
    finally:
        edit_release.set()
        writer.join(3)
        later.join(3)
    assert not writer.is_alive() and not later.is_alive()
    assert order == ['edit', 'build']


def test_batch_requests_explain_the_supported_submission_path(rig):
    with pytest.raises(NetworkError, match='For a batch, use compile_and_diff'):
        operations.start(rig.net, rig.owner, 'demo', **(rig.args | {'function': 'work, other_work'}))
    assert not rig.calls


def _extra_owner(rig, number, unit='other-unit'):
    name = f'work{number}'
    with rig.net.store.tx() as db:
        db.execute('INSERT INTO agents(id,name,token_hash) VALUES(?,?,?)', (number, name, name))
        db.execute('INSERT INTO functions(id,project_id,name,unit) VALUES(?,1,?,?)', (number, name, unit))
        db.execute('INSERT INTO claims VALUES(?,?,?,?)', (number, number, time.time(), time.time()+600))
    rig.net._function = lambda name, slug: rig.net.store.one('SELECT * FROM functions WHERE name=?', name)
    return SimpleNamespace(id=number, name=name), rig.args | {'function': name, 'request_id': name}


def test_completed_request_retry_does_not_rehash_changed_inputs(rig, monkeypatch):
    stamps = ['before']
    reads = []
    def parts(*args):
        reads.append(1)
        return None, '', 'revision', {'unit': stamps[0]}
    monkeypatch.setattr(operations.compile_tools, 'revision_parts', parts)
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    rig.release.set()
    rig.net._compile_operations.queue.join()
    stamps[0] = 'source changed by the completed request'
    before = len(reads)
    result = operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert 'completed' in result and len(rig.calls) == 1
    assert len(reads) == before
    assert operation_id(rig) in result


def test_legacy_request_hashes_with_diagnostic_parts_are_still_retryable(rig):
    import hashlib
    payload = json.dumps(dict(action='patch', region_id=0, function='work', revision='rev',
                              edits=rig.args['edits'], parts={'unit': 'old snapshot'}), sort_keys=True)
    with rig.net.store.tx() as db:
        db.execute("INSERT INTO compile_operations(id,project_id,function_id,agent_id,request_key,payload_hash,state,payload,instance,created_at) "
                   "VALUES('legacy',1,1,1,'trial-1',?,'completed',?,'old',?)",
                   (hashlib.sha256(payload.encode()).hexdigest(), payload, time.time()))
    result = operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert 'legacy: completed' in result and not rig.calls


def test_independent_unit_starts_while_first_compile_is_blocked(rig, monkeypatch):
    other, args = _extra_owner(rig, 2)
    second = threading.Event()
    original = operations.compile_tools.compile_patch
    def compile_patch(*pos, **kw):
        if kw['function'] == args['function']:
            second.set()
            return 'independent unit compiled'
        return original(*pos, **kw)
    monkeypatch.setattr(operations.compile_tools, 'compile_patch', compile_patch)
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    operations.start(rig.net, other, 'demo', **args)
    assert second.wait(2), 'independent work waited for the first build'
    assert not rig.release.is_set()


def test_same_unit_wait_does_not_occupy_the_other_worker(rig, monkeypatch):
    same, same_args = _extra_owner(rig, 2, 'unit')
    other, other_args = _extra_owner(rig, 3, 'other')
    entered = {2: threading.Event(), 3: threading.Event()}
    original = operations.compile_tools.compile_patch
    def compile_patch(net, agent, *pos, **kw):
        if agent.id in entered:
            entered[agent.id].set()
            return 'judged'
        return original(net, agent, *pos, **kw)
    monkeypatch.setattr(operations.compile_tools, 'compile_patch', compile_patch)
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    operations.start(rig.net, same, 'demo', **same_args)
    operations.start(rig.net, other, 'demo', **other_args)
    assert entered[3].wait(2)
    assert not entered[2].is_set(), 'the same source unit ran concurrently'
    rig.release.set()
    rig.net._compile_operations.queue.join()
    assert entered[2].is_set()


def test_worker_count_is_bounded(rig, monkeypatch):
    second, args2 = _extra_owner(rig, 2, 'second')
    third, args3 = _extra_owner(rig, 3, 'third')
    entered = {1: threading.Event(), 2: threading.Event(), 3: threading.Event()}
    def blocked(net, agent, *pos, **kw):
        entered[agent.id].set()
        assert rig.release.wait(5)
        return 'judged'
    monkeypatch.setattr(operations.compile_tools, 'compile_patch', blocked)
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    operations.start(rig.net, second, 'demo', **args2)
    assert entered[1].wait(2) and entered[2].wait(2)
    operations.start(rig.net, third, 'demo', **args3)
    assert not entered[3].wait(0.1)
    rig.release.set()
    rig.net._compile_operations.queue.join()
    assert entered[3].is_set()


def test_heartbeat_renews_queued_and_running_claims(rig, monkeypatch):
    monkeypatch.setattr(operations, 'LEASE_SECONDS', 0.02)
    rig.net.claim_ttl = 1200
    renewed = threading.Event()
    original = operations.Operations._renew_claims
    def renew(service):
        original(service)
        renewed.set()
    monkeypatch.setattr(operations.Operations, '_renew_claims', renew)
    other, args = _extra_owner(rig, 2, 'unit')
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    operations.start(rig.net, other, 'demo', **args)
    renewed.clear()
    before = {r['function_id']: r['expires_at'] for r in rig.net.store.all('SELECT * FROM claims')}
    assert renewed.wait(2)
    after = {r['function_id']: r['expires_at'] for r in rig.net.store.all('SELECT * FROM claims')}
    assert all(after[fid] > before[fid] for fid in (1, 2))
    assert rig.net.store.one('SELECT state FROM compile_operations WHERE agent_id=2')['state'] == 'queued'


@pytest.mark.parametrize('state', ['expired', 'released', 'reassigned'])
def test_heartbeat_never_recovers_lost_ownership(rig, state):
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    with rig.net.store.tx() as db:
        if state == 'expired':
            db.execute('UPDATE claims SET expires_at=1')
        elif state == 'released':
            db.execute('DELETE FROM claims')
        else:
            db.execute("INSERT INTO agents(id,name,token_hash) VALUES(2,'other','other')")
            db.execute('UPDATE claims SET agent_id=2,expires_at=9999999999')
    before = [dict(r) for r in rig.net.store.all('SELECT * FROM claims')]
    rig.net._compile_operations._renew_claims()
    assert [dict(r) for r in rig.net.store.all('SELECT * FROM claims')] == before


def test_retry_identity_survives_a_function_rename(rig):
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    rig.release.set()
    rig.net._compile_operations.queue.join()
    with rig.net.store.tx() as db:
        db.execute("UPDATE functions SET name='renamed_work' WHERE id=1")
    rig.net._function = lambda *args: rig.net.store.one('SELECT * FROM functions WHERE id=1')
    assert 'completed' in operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert len(rig.calls) == 1


def test_two_units_sharing_a_source_file_do_not_compile_together(rig, tmp_path, monkeypatch):
    other, args = _extra_owner(rig, 2, 'different-unit-name')
    fp = SimpleNamespace(unit=lambda name: SimpleNamespace(source='shared.c'), safe_path=lambda path: tmp_path/path)
    rig.net.forks.get = lambda slug: ({'id': 1, 'slug': slug}, fp, {})
    second = threading.Event()
    original = operations.compile_tools.compile_patch
    def compile_patch(net, agent, *pos, **kw):
        if agent.id == 2:
            second.set()
            return 'judged'
        return original(net, agent, *pos, **kw)
    monkeypatch.setattr(operations.compile_tools, 'compile_patch', compile_patch)
    operations.start(rig.net, rig.owner, 'demo', **rig.args)
    assert rig.entered.wait(2)
    operations.start(rig.net, other, 'demo', **args)
    assert not second.wait(0.1)
    rig.release.set()
    rig.net._compile_operations.queue.join()
    assert second.is_set()


@pytest.mark.parametrize('linked', [False, True])
def test_a_definition_compile_holds_only_its_units_locks(rig, tmp_path, monkeypatch, linked):
    """A background definition compile held the whole tree for the compile, stalling every other agent in the
    project; it takes this unit's locks as compile_patch does: the tree to itself only for a unit linked into the game."""
    from threading import RLock

    unit = SimpleNamespace(name='unit', source='unit.c', complete=linked)
    fp = SimpleNamespace(root=tmp_path, unit=lambda name: unit, safe_path=lambda rel, **kw: tmp_path / rel,
                         linkable=lambda: linked)
    engine = rig.net.forks
    proj = engine.get('demo')[0]
    engine.get = lambda slug: (proj, fp, {})
    engine._fn = lambda slug, name: rig.net._function(name, slug)
    lock = RLock()
    engine._unit_lock = lambda proj, name: lock
    monkeypatch.setattr(operations.compile_tools, 'snapshot', lambda *a: (None, 'int work(void){return 1;}', 'rev'))
    monkeypatch.setattr(operations.compile_tools, 'revision_parts', lambda *a: (None, '', 'rev', {}))
    tree, seen = engine._tree(proj), []

    def compile_and_diff(agent, function, source, slug):
        seen.append((tree._writer is not None, tree._readers, lock._is_owned()))
        return 'ordinary strict judge'

    rig.net.compile_and_diff = compile_and_diff
    operations.start(rig.net, rig.owner, 'demo', function='work', request_id='definition-1', revision='rev',
                     action='definition', source='int work(void){return 2;}')
    rig.net._compile_operations.queue.join()
    assert 'completed' in operations.status(rig.net, rig.owner, 'demo', operation_id(rig), wait_ms=0)
    assert seen == [(True, 0, True)] if linked else seen == [(False, 1, True)]

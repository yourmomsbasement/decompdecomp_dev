"""Real helper-session dispatch with a synthetic game and stubbed execution, never a model or agent CLI."""

import json
import threading

import pytest

from decompnet import agents as kinds
from decompnet.app import agents, specialists, tools
from decompnet.app.engine import RomNetwork


class OneSession(threading.Event):
    """End after the first completed session without sleeping or marking a successful report stopped."""

    def wait(self, timeout=None):
        self.set()
        return True


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for kind in kinds.AGENTS:
        monkeypatch.setattr(kind, 'config_model', lambda: None)
    monkeypatch.setattr(agents, 'detect', lambda quick=False: [])
    monkeypatch.setattr(agents.Manager, '_strays', lambda self: set())
    monkeypatch.setattr(specialists, 'revision', lambda *args: 'a' * 64)
    net = RomNetwork(tmp_path / 'app.db', tmp_path)
    with net.store.tx() as db:
        project = db.execute('INSERT INTO projects(slug,name,arch,root,config) VALUES(?,?,?,?,?)',
                             ('game', 'Synthetic game', 'x86', str(tmp_path), json.dumps({'kind': 'rom'}))).lastrowid
        for name in ['work', 'other']:
            db.execute('INSERT INTO functions(project_id,name,size,n_insns,status,unit) VALUES(?,?,?,?,?,?)',
                       (project, name, 4, 250, 'open', ''))
            db.execute('UPDATE functions SET vram=0,best_score=80 WHERE name=?', (name,))
    manager = agents.Manager(net, tmp_path, 'http://127.0.0.1:1/mcp')
    owner, _ = net.local_agent('source-owner')
    fn = net._function('work', 'game')
    with net.store.tx() as db:
        db.execute('INSERT INTO claims(function_id,agent_id,claimed_at,expires_at) VALUES(?,?,?,?)',
                   (fn['id'], owner.id, 0, 1e12))
    session = manager._record('game', 'opencode', 'existing-helper', 'existing-helper', {})
    session.stop = OneSession()
    return net, owner, manager, session


def queue(net, owner):
    return specialists.request(net, owner, 'game', 'work', 'codegen',
                               'Which copy width explains the mismatch in the first block?',
                               'The last two width recipes gave the same differing load instruction.')


def test_existing_helper_reports_without_compiles_or_new_helpers_and_owner_keeps_claim(rig, monkeypatch):
    net, owner, manager, session = rig
    queue(net, owner)
    helper_count = net.store.one('SELECT count(*) AS n FROM helpers')['n']
    calls = []

    def run(current, token, workdir):
        helper = net.store.one('SELECT id,name,owner,trusted,token_hash FROM agents WHERE name=?', current.agent_name)
        agent = agents.network.Agent(helper['id'], helper['name'], helper['owner'], bool(helper['trusted']))
        prompt = manager._prompt(current)
        assert 'advisory specialist' in prompt and 'experiment' in prompt
        assert net.store.one('SELECT token_hash FROM agents WHERE id=?', agent.id)['token_hash'] == helper['token_hash']
        assert 'advisory specialist' in tools.call(net, agent, 'game', 'claim_function', {'function': 'other', 'strategy': ''})
        text = tools.call(net, agent, 'game', 'report_specialist', {
            'request_id': current.options['_specialist_request'],
            'findings': 'The first copy uses two independent 64-bit loads; owner should test a two-word temporary.',
            'proposals': []})
        assert ': completed ' in text
        calls.append(agent.id)
        return True

    monkeypatch.setattr(manager, '_run_cli', run)
    manager._run(session)
    row = net.store.one('SELECT * FROM helper_sessions WHERE helper_id=?', session.id)
    assert row['role'] == 'specialist' and row['request_id'] == 1
    assert row['outcome'] == 'ok' and row['compiles'] == 0
    assert len(calls) == 1 and net.store.one('SELECT count(*) AS n FROM helpers')['n'] == helper_count
    assert net.store.one('SELECT agent_id FROM claims WHERE function_id=(SELECT id FROM functions WHERE name=?)',
                         'work')['agent_id'] == owner.id
    assert not net._specialist_sessions and '_specialist_request' not in session.options
    specialists.guard(net, agents.network.Agent(calls[0], 'existing-helper', '', False), 'game', 'compile_and_diff')


@pytest.mark.parametrize('failure', [RuntimeError('synthetic runner failure'), agents.Fatal('synthetic sign-in required')])
def test_runner_failures_finish_assignment_and_clear_specialist_guard(rig, monkeypatch, failure):
    net, owner, manager, session = rig
    queue(net, owner)

    def run(*args):
        raise failure

    monkeypatch.setattr(manager, '_run_cli', run)
    manager._run(session)
    request = net.store.one('SELECT * FROM specialist_requests WHERE id=1')
    assert request['status'] == 'failed'
    assert not net._specialist_sessions and '_specialist_request' not in session.options
    row = net.store.one('SELECT * FROM helper_sessions WHERE helper_id=?', session.id)
    assert row['role'] == 'specialist' and row['request_id'] == 1
    assert row['outcome'] == ('paused' if isinstance(failure, agents.Fatal) else 'failed')
    assert net.store.one('SELECT count(*) AS n FROM claims WHERE agent_id=?', owner.id)['n'] == 1


def test_normal_workers_keep_claim_and_compile_tools_available(rig, monkeypatch):
    net, owner, manager, session = rig

    def run(current, token, workdir):
        helper = net.store.one('SELECT id,name,owner,trusted FROM agents WHERE name=?', current.agent_name)
        agent = agents.network.Agent(helper['id'], helper['name'], helper['owner'], bool(helper['trusted']))
        for tool in ['claim_function', 'compile_and_diff', 'try_recipes', 'request_specialist']:
            specialists.guard(net, agent, 'game', tool)
        text = tools.call(net, agent, 'game', 'list_functions', {'limit': 2})
        assert not text.startswith('error:')
        current.stop.set()
        return True

    monkeypatch.setattr(manager, '_run_cli', run)
    manager._run(session)
    row = net.store.one('SELECT * FROM helper_sessions WHERE helper_id=?', session.id)
    assert row['role'] == 'owner' and row['request_id'] is None
    assert net.store.one('SELECT count(*) AS n FROM claims WHERE agent_id=?', owner.id)['n'] == 1


def test_session_setup_failure_also_finishes_assignment_and_clears_guard(rig, monkeypatch):
    net, owner, manager, session = rig
    queue(net, owner)

    def failed_begin(current):
        raise RuntimeError('synthetic session bookkeeping failure')

    monkeypatch.setattr(manager, '_session_begin', failed_begin)
    with pytest.raises(RuntimeError, match='bookkeeping failure'):
        manager._run(session)
    assert net.store.one('SELECT status FROM specialist_requests WHERE id=1')['status'] == 'failed'
    assert not net._specialist_sessions and '_specialist_request' not in session.options
    assert net.store.one('SELECT count(*) AS n FROM claims WHERE agent_id=?', owner.id)['n'] == 1


def test_source_edit_cannot_change_sibling_definition_with_another_active_owner(rig, tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from decompnet.app.forkengine import ForkEngine, _TreeLock
    from decompnet.network import NetworkError

    net, owner, _, _ = rig
    editor, _ = net.local_agent('unit-editor')
    with net.store.tx() as db:
        db.execute("UPDATE functions SET unit='shared'")
        db.execute('INSERT INTO claims(function_id,agent_id,claimed_at,expires_at) '
                   "VALUES((SELECT id FROM functions WHERE name='other'),?,?,?)", (editor.id, 0, 1e12))
    source = tmp_path / 'shared.c'
    original = 'int work(void) { return 1; }\nint other(void) { return 2; }\n'
    source.write_text(original)
    engine = ForkEngine.__new__(ForkEngine)
    engine.net = net
    tree = _TreeLock()
    engine._tree = lambda proj: tree
    engine._units_locked = lambda *args: nullcontext()
    engine._meta = lambda fn: {}
    engine._baseline = lambda *args: pytest.fail('Ownership refusal must happen before baseline or compilation.')
    unit = SimpleNamespace(name='shared', complete=False)
    with pytest.raises(NetworkError, match='work has another source owner'):
        engine._edit_source(editor, net._project('game'), SimpleNamespace(), source, 'shared.c', [unit],
                            'return 1;', 'return 3;', 'Edit a sibling definition')
    assert source.read_text() == original
    assert net.store.one("SELECT agent_id FROM claims JOIN functions ON functions.id=claims.function_id "
                         "WHERE functions.name='work'")['agent_id'] == owner.id
    assert net.store.one("SELECT agent_id FROM claims JOIN functions ON functions.id=claims.function_id "
                         "WHERE functions.name='other'")['agent_id'] == editor.id
    assert net.store.one('SELECT count(*) AS n FROM attempts')['n'] == 0

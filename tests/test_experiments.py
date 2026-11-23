"""Persistent attempt evidence and soft retry ordering need no compiler or game."""

import json

import pytest

from decompnet.agents.api import api_tool_schemas
from decompnet.app import tools
from decompnet.app.engine import RomNetwork
from decompnet.network import Network, NetworkError


@pytest.fixture
def rig(tmp_path):
    net = RomNetwork(tmp_path / 'history.db', tmp_path)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,arch,root,config) VALUES(?,?,?,?)",
                         ('demo', 'x86_64', str(tmp_path), json.dumps({'kind': 'rom'}))).lastrowid
        for name, instructions, attempts in [('retried', 4, 40), ('fresh', 5, 0)]:
            db.execute("INSERT INTO functions(project_id,name,n_insns,size,attempts) VALUES(?,?,?,?,?)",
                       (pid, name, instructions, instructions * 4, attempts))
    agent, _ = net.local_agent('test')
    return net, agent, net._function('retried', 'demo'), tmp_path


def test_history_survives_restart_and_distinguishes_score_from_assembly(rig):
    net, agent, fn, path = rig
    net._record(fn, agent, 12, 'return 1;', 1)
    net._record(fn, agent, 12, 'return 2;', 1)
    net._record(fn, agent, 12, 'return 2;', 2)
    text = Network(path / 'history.db').experiment_history('retried', 'demo')
    assert 'Source identical to #2; build equivalence unknown' in text
    assert 'Context version changed' in text
    assert 'Same recorded score' in text and 'assembly equivalence unknown' in text
    assert '+return 2;' in text and 'agent=test' in text


def test_history_pagination_compares_against_predecessor_and_is_scoped(rig):
    net, agent, fn, _ = rig
    for source in ('one', 'two', 'three'):
        net._record(fn, agent, 12, source, 1)
    net._record(net._function('fresh', 'demo'), agent, 12, 'unrelated secret', 1)
    text = net.experiment_history('retried', 'demo', limit=1)
    assert '#3 agent=' in text and 'Source change from #2' in text
    assert 'before=3' in text and 'unrelated secret' not in text
    page = net.experiment_history('retried', 'demo', limit=1, before=3)
    assert '#2 agent=' in page and '#3 agent=' not in page and 'Source change from #1' in page
    assert 'No recorded experiments' in net.experiment_history('retried', 'demo', before=1)


def test_history_bounds_changes_and_labels_failed_compiles(rig):
    net, agent, fn, _ = rig
    net._record(fn, agent, 12, 'first', 1)
    net._record(fn, agent, None, 'x' * 10000, 1)
    text = net.experiment_history('retried', 'demo', limit=999)
    assert 'compiled=False unscored' in text and '[change truncated]' in text
    assert len(text) < 2000
    with pytest.raises(NetworkError, match='no function'):
        net.experiment_history('missing', 'demo')


@pytest.mark.parametrize('strategy', ['easiest', 'finish'])
def test_automatic_claims_prefer_fewer_attempts_in_a_size_band(rig, strategy):
    net, agent, _, _ = rig
    assert net.claim_row(agent, 'demo', strategy=strategy)['name'] == 'fresh'
    other, _ = net.local_agent('other')
    assert net.claim_row(other, 'demo', strategy=strategy)['name'] == 'retried'


def test_named_claim_can_resume_a_retried_function(rig):
    net, agent, _, _ = rig
    assert net.claim_row(agent, 'demo', 'retried')['name'] == 'retried'


def test_history_available_to_both_interfaces_and_modes(rig):
    net, agent, _, _ = rig
    for fork in (False, True):
        assert 'experiment_history' in {t['function']['name'] for t in api_tool_schemas(fork=fork)}
    assert 'No recorded experiments' in tools.HANDLERS['experiment_history'](net, agent, 'demo', 'retried')


def test_history_caps_entries_and_clamps_invalid_page_bounds(rig):
    net, agent, fn, _ = rig
    for number in range(25):
        net._record(fn, agent, number, str(number), 1)
    text = net.experiment_history('retried', 'demo', limit=999, before=-4)
    assert sum(line.startswith('#') for line in text.splitlines()) == 20
    assert 'before=6' in text
    text = net.experiment_history('retried', 'demo', limit=0)
    assert sum(line.startswith('#') for line in text.splitlines()) == 1


def test_history_cannot_mix_functions_with_the_same_name_in_other_projects(rig):
    net, agent, fn, _ = rig
    net._record(fn, agent, 12, 'local source', 1)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,root,config) VALUES('other','','{}')").lastrowid
        db.execute("INSERT INTO functions(project_id,name) VALUES(?, 'retried')", (pid,))
    other = net._function('retried', 'other')
    net._record(other, agent, 12, 'other source', 1)
    text = net.experiment_history('retried', 'demo')
    assert '#1 agent=' in text and '#2 agent=' not in text
    with pytest.raises(NetworkError, match='several projects'):
        net.experiment_history('retried')


def test_preview_ledger_survives_restart_without_changing_claims_or_attempts(rig):
    from decompnet import experiments
    net, agent, fn, path = rig
    before = net.store.one('SELECT attempts,status FROM functions WHERE id=?', fn['id'])
    result = dict(score=12, compiled=True, matched=False, output_fingerprint='a' * 64)
    experiments.record(net, fn, agent, 'revision-one', 'first hypothesis', 'return 1;', result, .1)
    experiments.record(net, fn, agent, 'revision-two', 'second hypothesis', 'return 2;', result, .2)
    reopened = Network(path / 'history.db')
    text = reopened.experiment_history('retried', 'demo')
    assert 'first hypothesis' in text and 'second hypothesis' in text
    assert 'same code/relocations/local data as E1' in text
    assert 'no verdict reuse' in text and 'revision-two' in text
    after = reopened.store.one('SELECT attempts,status FROM functions WHERE id=?', fn['id'])
    assert tuple(after) == tuple(before)
    assert reopened.store.one('SELECT count(*) AS n FROM claims')['n'] == 0
    assert reopened.store.one('SELECT count(*) AS n FROM attempts')['n'] == 0

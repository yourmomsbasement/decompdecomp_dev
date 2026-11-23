"""Finishing proposals must earn strict matches; percentages and renamed symbols alone are not verdicts."""
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from decompnet.agents.api import api_tool_schemas
from decompnet.app import finishing, tools


def diff(rows=(), renamed=(), percent=99):
    return SimpleNamespace(rows=rows, renamed=renamed, match_percent=percent)


def test_scheduling_only_when_instructions_are_a_permutation():
    source = 'void f(void) {}'
    d = diff([('i', 'mov eax, 1', 'mov ecx, 2'), ('i', 'mov ecx, 2', 'mov eax, 1')])
    assert finishing.candidates(source, d, 'x86_64')[0][0] == 'btver2'
    assert finishing.candidates(source, d, 'ppc') == []
    assert finishing.candidates(source, diff([('i', 'je 0x20', 'je 0x24')]), 'x86_64') == []


def test_alias_uses_original_type_and_does_not_rewrite_comments_or_strings():
    source = 'void f(void) { func_1111(); /* func_1111 */ const char *s="func_1111"; }'
    result = finishing.candidates(source, diff(renamed=[('func_2222', 'func_1111')]), 'x86_64')[0][1]
    assert '__typeof__(func_1111)' in result and '__asm__("func_2222")' in result
    assert 'decomp_finish_' in result and '/* func_1111 */' in result and '"func_1111"' in result


@pytest.mark.parametrize('pairs', [[('D_1111', '[.rodata]')], [('func_1111', 'D_2222')],
    [('func_1111', 'func_3333'), ('func_2222', 'func_3333')]])
def test_ambiguous_local_table_and_cross_kind_references_are_not_aliased(pairs):
    assert finishing.candidates('void f(void) { func_3333(); }', diff(renamed=pairs), 'x86_64') == []


@pytest.fixture
def rig(tmp_path, monkeypatch):
    db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE functions(id,name,status,unit); CREATE TABLE claims(function_id,agent_id,claimed_at,expires_at);'
                     'INSERT INTO functions VALUES(1,"f","open","u"); INSERT INTO functions VALUES(2,"g","matched","u");')
    @contextmanager
    def tx():
        yield db
    @contextmanager
    def exclusive():
        yield
    source = tmp_path / 'u.c'; source.write_text('void f(void) {}')
    base = tmp_path / 'u.o'; base.write_bytes(b'object')
    unit = SimpleNamespace(source='u.c', base=base)
    d = diff([('i', 'mov eax, 1', 'mov ecx, 2'), ('i', 'mov ecx, 2', 'mov eax, 1')])
    fp = SimpleNamespace(unit=lambda _: unit, safe_path=lambda _: source, diff=lambda *a: d)
    calls = []; revision = ['r']; outcome = {'matched': True}
    net = SimpleNamespace(store=SimpleNamespace(tx=tx), _expire_claims=lambda db: None, claim_ttl=300)
    engine = SimpleNamespace(_tree=lambda _: SimpleNamespace(exclusive=exclusive),
                             get=lambda _: ({}, fp, {}), _meta=lambda _: {}, _baseline=lambda *a: ({}, True))
    def probe(*a, **kw):
        calls.append('probe'); return outcome
    def apply(*a, **kw):
        calls.append('apply'); assert kw == {'keep': False}; return {'matched': True}
    engine._attempt = probe; engine.attempt = apply; net.forks = engine
    monkeypatch.setattr(finishing, 'snapshot', lambda *a: ({}, source.read_text(), revision[0]))
    yield SimpleNamespace(net=net, db=db, calls=calls, outcome=outcome, revision=revision,
                          proj={'slug': 'p', 'arch': 'x86_64'}, agent=SimpleNamespace(id=7), d=d)
    db.close()


def test_audit_does_not_compile_and_claim_is_released(rig):
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1).startswith('candidate')
    assert rig.calls == [] and not rig.db.execute('SELECT * FROM claims').fetchall()


def test_only_exact_preview_can_apply(rig):
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True) == 'matched'
    assert rig.calls == ['probe', 'apply']


def test_failed_trial_is_cached_and_dependencies_invalidate_cache(rig):
    rig.outcome.update(matched=False, score=10)
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True).startswith('review')
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True).startswith('skipped: already')
    assert rig.calls == ['probe']
    rig.revision[0] = 'new'
    finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True)
    assert rig.calls == ['probe', 'probe']


def test_dependency_change_after_preview_prevents_apply(rig):
    def probe(*a, **kw):
        rig.revision[0] = 'changed'; return {'matched': True}
    rig.net.forks._attempt = probe
    assert 'dependencies changed' in finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True)
    assert rig.calls == []


def test_matched_and_claimed_functions_are_skipped(rig):
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 2, True).startswith('skipped')
    rig.db.execute('INSERT INTO claims VALUES(1,8,0,99999999999)')
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True).startswith('skipped')
    assert rig.db.execute('SELECT agent_id FROM claims').fetchone()[0] == 8 and rig.calls == []


def test_baseline_failure_never_compiles_a_candidate(rig):
    rig.net.forks._baseline = lambda *a: (None, False)
    assert 'baseline cannot' in finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True)
    assert rig.calls == []


def test_tool_is_exposed_to_both_agent_transports():
    assert 'finish_near_matches' in tools.HANDLERS
    assert 'finish_near_matches' in {s['function']['name'] for s in api_tool_schemas()}


def test_local_alias_names_do_not_collide_between_functions():
    d = diff(renamed=[("func_2222", "func_1111")])
    a = finishing.candidates("void f(void) { func_1111(); }", d, "x86_64")[0][1]
    b = finishing.candidates("void g(void) { func_1111(); }", d, "x86_64")[0][1]
    import re
    assert re.search(r"decomp_finish_\w+", a)[0] != re.search(r"decomp_finish_\w+", b)[0]


def test_finishing_progress_and_completed_summary():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if node is None:
        pytest.skip('needs node')
    subprocess.run([node, str(Path(__file__).with_name('gui_finishing.js'))], check=True, timeout=30)


def test_typed_linkage_candidates_pass_source_guard_but_instructions_do_not():
    from decompnet.app.forkengine import _CHEAT, _strip_comments
    source = 'void f(void) { func_1111(); }'
    candidate = finishing.candidates(source, diff(renamed=[('func_2222', 'func_1111')]), 'x86_64')[0][1]
    assert not _CHEAT.search(_strip_comments(candidate))
    for text in ['__asm__("nop");', 'asm volatile("nop");',
                 candidate.replace('"func_2222"', '"func_2222\\n nop"'),
                 candidate + ' __asm__("nop");']:
        assert _CHEAT.search(_strip_comments(text))


def test_blank_project_arch_uses_target_processor(rig, monkeypatch):
    rig.proj['arch'] = ''
    monkeypatch.setattr(finishing, 'processor', lambda *a: 'x86_64')
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1).startswith('candidate: btver2')
    monkeypatch.setattr(finishing, 'processor', lambda *a: '')
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1).startswith('skipped: unsupported')
    assert rig.calls == []


@pytest.mark.parametrize('failure', ['BUILD ERROR', 'comparison unavailable'])
def test_unjudged_trials_are_retried_without_source_changes(rig, failure):
    rig.outcome.update(matched=False, score=None, text=failure)
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True).startswith('retry:')
    rig.outcome.update(matched=True, score=0)
    assert finishing.finish_one(rig.net, rig.agent, rig.proj, 1, True) == 'matched'
    assert rig.calls == ['probe', 'probe', 'apply']

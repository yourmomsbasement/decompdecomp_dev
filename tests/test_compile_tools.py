"""Preview transactions use the real fork judge with deterministic synthetic build/diff verdicts.

No compiler/game installation is needed: every guard and rollback branch runs in CI.
"""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from decompnet.agents.api import api_tool_schemas
from decompnet.app import compile_tools, tools
from decompnet.app.forkengine import ForkEngine, _TreeLock
from decompnet.network import NetworkError

SOURCE = 'int work(int x) { return x + 1; }\n'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    path = tmp_path / 'unit.c'
    path.write_text(SOURCE + 'int neighbor(void) { return 4; }\n')
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    from decompnet.store import SCHEMA
    db.executescript(SCHEMA)

    @contextmanager
    def tx():
        with db:
            yield db

    net = SimpleNamespace(store=SimpleNamespace(tx=tx), _generation={},
                          compile_key=lambda *args: "test-key", _expire_claims=lambda db: None, _agent_name=lambda id: 'another agent', claim_ttl=300)
    engine = ForkEngine.__new__(ForkEngine)
    engine.net = net
    net.forks = engine
    fn = dict(id=1, project_id=1, name='work', unit='unit', status='open', best_score=20,
              best_source=SOURCE, n_insns=5)
    unit = SimpleNamespace(name='unit', source='unit.c', complete=False, target=None)
    monkeypatch.setattr(compile_tools, 'processor', lambda fp, unit, symbol, recorded: recorded)
    proj = dict(id=1, arch='x86_64')
    fp = SimpleNamespace(root=tmp_path, manifest=None, unit=lambda name: unit,
                         safe_path=lambda rel, **kw: tmp_path / rel)
    engine.get = lambda slug: (proj, fp, {})
    engine._fn = lambda slug, function: fn
    engine._meta = lambda fn: {}
    engine._names = lambda meta, fn: ['work']
    tree = _TreeLock()
    engine._tree = lambda proj: tree
    from threading import RLock
    lock = RLock()
    engine._unit_lock = lambda proj, unit: lock
    engine._baseline = lambda *args: ({'work': 80, 'neighbor': 100}, True)
    engine._held = lambda *args: set()
    engine._keep_scores = lambda *args: None
    engine._apply_scores = lambda *args, **kw: pytest.fail('preview changed scores')
    state = SimpleNamespace(builds=[], diffs=0, broken=False, literals=False, raises=False,
                            bad_build=False, restore_fail=False, commits=[])

    def build(*args):
        text = path.read_text()
        state.builds.append(text)
        return (not (state.bad_build and '+ 2' in text or state.restore_fail and text.startswith(SOURCE)), 'test log')

    def diff(*args):
        state.diffs += 1
        if state.raises:
            raise RuntimeError('unexpected diff failure')
        matched = '+ 2' in path.read_text() and not state.literals
        return SimpleNamespace(matched=matched, match_percent=100 if matched else 80,
                               unit_scores={'neighbor': 50 if state.broken else 100},
                               literals=['wrong literal'] if state.literals else [])

    engine._build_unit = build
    fp.diff = diff
    agent = SimpleNamespace(id=7, name='tester')

    def attempt(agent, slug, function, source):
        state.commits.append(source)
        # Applying is covered by the pre-existing judge tests; here prove which candidate is handed to it.
        path.write_text(source + '\nint neighbor(void) { return 4; }\n')
        return dict(text='normal judge', score=0, matched=True)

    engine.attempt = attempt
    yield SimpleNamespace(net=net, engine=engine, path=path, state=state, agent=agent, db=db, fn=fn, unit=unit, fp=fp)
    db.close()


def revision(rig):
    return compile_tools.snapshot(rig.engine, 'test', 'work')[2]


def recipes():
    return [dict(name='two', edits=[dict(old='+ 1', new='+ 2')])]


def test_repeated_preview_rejudges_and_shares_new_observation(rig):
    """Source identity alone cannot prove full build-state identity or reuse a correctness verdict."""
    rig.net.compile_key = lambda fn, *state: 'key:' + '|'.join(state)
    out = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes())
    assert 'Preview only' in out
    builds, diffs = len(rig.state.builds), rig.state.diffs
    assert builds and diffs  # the baseline and the one candidate were judged
    again = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes())
    assert 'Preview only' in again
    assert len(rig.state.builds) > builds and rig.state.diffs > diffs
    assert 'E4 two:' in again and 'E2 two:' in out


def test_a_matching_recipe_stops_the_batch(rig):
    """An exact match cannot be beaten: the recipes after it are not built."""
    rig.net.compile_key = lambda fn, *state: 'key:' + '|'.join(state)
    both = [dict(name='two', edits=[dict(old='+ 1', new='+ 2')]),
            dict(name='three', edits=[dict(old='+ 1', new='+ 3')])]
    out = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), both)
    assert 'matches exactly' in out
    assert not any('+ 3' in text for text in rig.state.builds), "the second recipe was built after the first matched"


def test_exact_preview_restores_source_build_claims_and_scores(rig):
    before = rig.path.read_bytes()
    result = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes())
    assert 'Best: two' in result
    assert rig.path.read_bytes() == before
    assert rig.state.builds[-1] == before.decode()
    assert not rig.db.execute('SELECT * FROM claims').fetchall()
    assert not rig.state.commits


def test_only_strictly_better_winner_is_applied(rig):
    compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes(), apply=True)
    assert len(rig.state.commits) == 1
    assert '+ 2' in rig.state.commits[0]


def test_equal_candidate_is_not_applied(rig):
    recipe = [dict(name='equal', edits=[dict(old='+ 1', new='+ 3')])]
    result = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipe, apply=True)
    assert 'No strictly better' in result
    assert not rig.state.commits


@pytest.mark.parametrize('flag', ['broken', 'bad_build', 'raises', 'literals'])
def test_preview_failure_or_wrong_literal_cannot_write_a_match(rig, flag):
    before = rig.path.read_bytes()
    setattr(rig.state, flag, True)
    if flag == 'raises':
        with pytest.raises(RuntimeError):
            rig.engine._attempt(rig.agent, 'test', 'work', SOURCE.replace('+ 1', '+ 2'), probe=True)
    else:
        result = rig.engine._attempt(rig.agent, 'test', 'work', SOURCE.replace('+ 1', '+ 2'), probe=True)
        assert not result['matched']
        if flag == 'literals':
            assert result['score'] > 0
    assert rig.path.read_bytes() == before
    assert not rig.state.commits


def test_failed_restore_build_is_reported_and_source_is_restored(rig):
    rig.state.restore_fail = True
    with pytest.raises(NetworkError, match='rebuilding it failed'):
        rig.engine._attempt(rig.agent, 'test', 'work', SOURCE.replace('+ 1', '+ 2'), probe=True)
    assert rig.path.read_text().startswith(SOURCE)


def test_claim_owned_by_other_agent_is_respected(rig):
    rig.db.execute('INSERT INTO claims VALUES(1,99,0,9999999999)')
    with pytest.raises(NetworkError, match='claimed by'):
        compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes())
    assert not rig.state.builds


@pytest.mark.parametrize('change', ['source', 'header', 'build_script', 'target'])
def test_stale_patch_is_rejected_before_build(rig, change):
    rev = revision(rig)
    if change == 'source':
        # the function's own definition moved; a neighbour's edit is no longer a staleness case (see the test below)
        rig.path.write_text(rig.path.read_text().replace('x + 1', 'x + 9'))
    elif change == 'header':
        (rig.path.parent / 'types.h').write_text('typedef int Int;')
    elif change == 'build_script':
        (rig.path.parent / 'tools').mkdir()
        (rig.path.parent / 'tools' / 'bind.py').write_text('# binding changed')
    else:
        rig.unit.target = rig.path.parent / 'target.o'
        rig.unit.target.write_bytes(b'synthetic')
    with pytest.raises(NetworkError, match='Stale'):
        compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', rev, recipes()[0]['edits'])
    assert not rig.state.builds
    assert not rig.state.commits


def test_the_revision_covers_the_whole_compiled_unit(rig):
    """An edit next to the function can change its compiled output through inlining or shared optimization."""
    before = revision(rig)
    rig.path.write_text(SOURCE + 'int neighbor(void) { return 5; }\n')  # someone else's edit, same file
    assert revision(rig) != before
    rig.path.write_text('int work(int x) { return x + 2; }\nint neighbor(void) { return 4; }\n')
    assert revision(rig) != before  # our own definition moved, so the patch really is stale


def test_patch_uses_normal_judge_and_compact_revision(rig):
    result = compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', revision(rig), recipes()[0]['edits'])
    assert 'normal judge' in result and 'revision:' in result
    assert '+ 2' in rig.state.commits[0]


@pytest.mark.parametrize('edits', [[], [{'old': '', 'new': 'a'}], [{'old': 'x', 'new': 'a'}],
                                    [{'old': '+ 1', 'new': '' ,'extra': True}], [{'old': 1, 'new': ''}]])
def test_invalid_edits_do_not_build(rig, edits):
    with pytest.raises(NetworkError):
        compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', revision(rig), edits)
    assert not rig.state.commits


def test_cannot_inject_sibling_or_shared_definition(rig):
    with pytest.raises(NetworkError, match='only the complete'):
        compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', revision(rig),
                                    [dict(old='}', new='}\nint other(void) { return 0; }')])
    assert not rig.state.commits


def test_recipe_validation_happens_before_any_build(rig):
    with pytest.raises(NetworkError):
        compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes() * 9)
    with pytest.raises(NetworkError):
        compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes() * 2)
    assert not rig.state.builds


@pytest.mark.parametrize('name', ['btver2', 'no-sse'])
def test_named_cpu_recipe_without_observed_mismatch_is_skipped(rig, name):
    text = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), [dict(name=name)])
    assert 'current mismatch evidence does not support' in text
    assert not any('__attribute__((target(' in source for source in rig.state.builds)
    assert rig.path.read_text().startswith(SOURCE)


@pytest.mark.parametrize('name', ['btver2', 'no-sse'])
def test_supported_named_cpu_recipe_remains_function_local(rig, name, monkeypatch):
    monkeypatch.setattr(compile_tools.x86, 'recipe_suggestions', lambda *args: [{'name': name}])
    compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), [dict(name=name)])
    assert any('__attribute__((target(' in text for text in rig.state.builds)
    assert rig.path.read_text().startswith(SOURCE)


def test_compact_brief_and_schema_parity(rig):
    brief = compile_tools.brief(rig.net, rig.agent, 'test', 'work')
    assert brief.count(SOURCE.strip()) == 1
    assert revision(rig) in brief
    new = {'function_brief', 'compile_patch', 'try_recipes'}
    fork_names = {t['function']['name'] for t in api_tool_schemas(fork=True)}
    rom_names = {t['function']['name'] for t in api_tool_schemas()}
    assert new <= fork_names
    assert not new & rom_names
    assert new <= set(tools.tool_names(True))


def test_same_line_injection_is_rejected(rig):
    with pytest.raises(NetworkError, match='only the complete'):
        compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', revision(rig),
                                    [dict(old='int work', new='int other(void) { return 0; } int work')])


def test_unknown_baseline_fails_closed(rig):
    rig.engine._baseline = lambda *args: (None, False)
    with pytest.raises(NetworkError, match='existing matches'):
        compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes(), apply=True)
    with pytest.raises(NetworkError, match='existing matches'):
        compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', revision(rig), recipes()[0]['edits'])
    assert not rig.state.commits


def test_linked_preview_reverifies_and_rebuilds_original(rig):
    rig.unit.complete = True
    rig.fp.linkable = lambda: True
    verifies = []
    rig.engine._verify = lambda *args: (verifies.append(rig.path.read_text()) or True, '')
    rig.engine._build_all = rig.engine._build_unit
    before = rig.path.read_bytes()
    result = rig.engine._attempt(rig.agent, 'test', 'work', SOURCE.replace('+ 1', '+ 2'), probe=True)
    assert result['matched']
    assert verifies and '+ 2' in verifies[0]
    assert rig.path.read_bytes() == before
    assert rig.state.builds[-1] == before.decode()


def test_real_x86_compiler_and_strict_objdiff(rig, monkeypatch):
    """Opt-in pinned compiler path; runs without downloads and never uses a compiler from PATH."""
    import os
    import subprocess
    from pathlib import Path

    from decompnet import toolchains
    from decompnet.forks import objdiff

    clang = os.environ.get('DECOMP_TEST_CLANG')
    if not clang or not Path(clang).is_file() or toolchains.installed('objdiff-cli') is None:
        pytest.skip('set DECOMP_TEST_CLANG to a pinned Clang; needs installed objdiff-cli')
    target = rig.path.parent / 'target.o'
    base = rig.path.parent / 'unit.o'
    neighbor = 'int neighbor(int x) { return (x + 1) * 4; }\n'
    rig.path.write_text(SOURCE.replace('+ 1', '+ 2') + neighbor)

    def build(*args):
        p = subprocess.run([clang, '-O2', '-mavx', '-c', str(rig.path), '-o', str(base)],
                           capture_output=True, text=True, timeout=30)
        return p.returncode == 0, p.stderr

    assert build()[0]
    target.write_bytes(base.read_bytes())
    rig.unit.target, rig.unit.base = target, base
    rig.path.write_text(SOURCE + neighbor)
    rig.engine._build_unit = build
    rig.fp.diff = lambda unit, symbol: objdiff.diff(target, base, symbol, rig.path.parent,
                                                  options=objdiff.judge_options())
    rig.engine._baseline = lambda *args: (rig.fp.diff(rig.unit, None).unit_scores, True) if build()[0] else (None, False)
    before = rig.path.read_bytes()
    result = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes())
    assert 'Best: two' in result
    assert rig.path.read_bytes() == before
    assert not rig.fp.diff(rig.unit, 'work').matched
    assert rig.fp.diff(rig.unit, 'neighbor').matched
    # A pragma affecting the following matched neighbor must fail even if the requested function matches.
    broken = rig.engine._attempt(rig.agent, 'test', 'work',
                                 SOURCE.replace('+ 1', '+ 2') + '#pragma clang optimize off\n', probe=True)
    assert broken['score'] is None and 'neighbor' in broken['broken']
    assert rig.path.read_bytes() == before
    assert rig.fp.diff(rig.unit, 'neighbor').matched
    # Finally pass the winner through the real keeping path (only bookkeeping/git are stubbed).
    rig.net.compile_key = lambda *args: 'key'
    rig.net.repeat_of = lambda *args: None
    rig.net.keep_diff = lambda *args: None
    rig.net._record = lambda *args, **kwargs: None
    rig.net.dependents_note = lambda *args: ''
    rig.net.keep_diff = lambda *args: None
    rig.engine._apply_scores = lambda *args, **kwargs: None
    rig.engine._hold = lambda *args: None
    rig.engine._remember = lambda *args: None
    rig.engine._file_done = lambda *args: ''
    rig.engine._commit = lambda *args: rig.state.commits.append(rig.path.read_text())
    rig.net.links = {}
    rig.engine.attempt = ForkEngine.attempt.__get__(rig.engine)
    result = compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', revision(rig), recipes()[0]['edits'])
    assert 'MATCH!' in result
    assert rig.fp.diff(rig.unit, 'work').matched
    assert rig.fp.diff(rig.unit, 'neighbor').matched
    assert len(rig.state.commits) == 1


def test_two_concurrent_patches_cannot_use_the_same_revision(rig):
    # Source revision validation must stay inside the tree lock, rather than race the write.
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    rev, barrier = revision(rig), Barrier(2)

    def run():
        barrier.wait(timeout=5)
        try:
            return compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', rev, recipes()[0]['edits'])
        except NetworkError as e:
            return str(e)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum('Stale' in text for text in results) == 1
    assert len(rig.state.commits) == 1


def test_apply_requires_boolean(rig):
    with pytest.raises(NetworkError, match='true or false'):
        compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes(), apply='false')
    assert not rig.state.builds


def test_mcp_definitions_register(rig):
    # Ensure MCP's own schema builder accepts nested patch arguments, as well as our API schema builder.
    server = tools.build_mcp(rig.net, lambda ctx: (rig.agent, 'test'))
    assert server is not None


def test_progress_and_unrelated_generation_do_not_invalidate_patch(rig):
    original_get = rig.engine.get
    proj, fp, cfg = original_get("test")
    rig.engine.get = lambda slug: (proj, fp, cfg)
    rev = revision(rig)
    cfg["measures"] = {"matched_functions": 42}
    cfg["spend"] = {"usd": 1, "tokens": 100}
    cfg["suggestions"] = [{"text": "new report"}]
    cfg["autopilot"] = {"tried": 2}
    rig.net._generation[1] = 10
    assert revision(rig) == rev
    cfg["compiler"] = "changed"
    assert revision(rig) != rev


def test_preview_history_records_negative_results_and_deduplicates_identical_recipes(rig):
    trials = [dict(name='first', edits=[dict(old='+ 1', new='+ 3')]),
              dict(name='same source', edits=[dict(old='+ 1', new='+ 3')])]
    result = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), trials)
    assert 'duplicate source in this batch' in result
    rows = rig.db.execute('SELECT name,score,compiled,matched FROM experiments ORDER BY id').fetchall()
    assert [(r['name'], r['score'], r['compiled'], r['matched']) for r in rows] == [('baseline',100,1,0),('first',100,1,0)]
    assert rig.path.read_text().startswith(SOURCE)


def test_every_new_agent_tool_is_in_both_interfaces_and_expected_modes():
    shared = {'inspect_blocks', 'request_specialist', 'specialist_status', 'specialist_brief', 'report_specialist'}
    assert shared <= tools.HANDLERS.keys()
    for fork in (False, True):
        names = {t['function']['name'] for t in api_tool_schemas(fork=fork)}
        assert shared <= names
        assert ('suggest_recipes' in names) == fork


def test_patches_preserve_function_local_loop_pragmas_without_allowing_defines(rig):
    source='int work(int x) {\n#pragma clang loop interleave_count(1)\nfor(int i=0;i<x;++i)x+=i; return x;\n}'
    assert compile_tools.definition(rig.engine,rig.fn,source)==source
    with pytest.raises(NetworkError,match='complete function'):
        compile_tools.definition(rig.engine,rig.fn,source.replace('#pragma clang loop interleave_count(1)','#define x 1'))


@pytest.mark.parametrize('quiet', [None, 0])
@pytest.mark.parametrize('outcome', ['success', 'matched', 'build_error', 'broken_neighbor', 'diff_error'])
def test_normal_compile_paths_measure_the_candidate_before_using_its_diff(rig, monkeypatch, outcome, quiet):
    from decompnet.app import function_parts
    from decompnet.forks.objdiff import ObjdiffError

    rig.engine._trials = {}
    rig.engine._apply_scores = lambda *args, **kwargs: None
    rig.engine._remember = lambda *args: None
    rig.engine._hold = lambda *args: None
    rig.engine._file_done = lambda *args: ''
    rig.engine._commit = lambda *args: None
    rig.engine._commit_paths = lambda *args: None
    rig.net.dependents_note = lambda *args: ''
    rig.net.keep_diff = lambda *args: None
    recorded = []
    rig.net._record = lambda *args, **kwargs: recorded.append((args, kwargs))
    original_diff = rig.fp.diff
    def diff(*args):
        result = original_diff(*args)
        result.render = lambda: 'synthetic instruction diff'
        result.renamed = []
        result.rows = []
        return result
    rig.fp.diff = diff
    rig.fp.build_problem = lambda: None
    monkeypatch.setattr(function_parts, 'guard', lambda *args, **kwargs: None)
    original = rig.path.read_text()
    source = SOURCE.replace('+ 1', '+ 3')
    if outcome in ('matched', 'build_error'):
        source = SOURCE.replace('+ 1', '+ 2')
    if outcome == 'build_error':
        rig.state.bad_build = True
    elif outcome == 'broken_neighbor':
        rig.state.broken = True
    elif outcome == 'diff_error':
        def fail(*args):
            raise ObjdiffError('synthetic comparison failed')
        rig.fp.diff = fail
    result = rig.engine._attempt(rig.agent, 'test', 'work', source, keep=False, quiet=quiet)
    if outcome == 'matched':
        assert result['matched'] and result['score'] == 0 and len(recorded) == 1
        assert '+ 2' in rig.path.read_text()
        return
    assert not result['matched']
    assert rig.path.read_text() == original
    if outcome == 'success':
        assert result['score'] is not None and rig.state.diffs == 1
    elif outcome == 'build_error':
        assert 'BUILD ERROR' in result['text'] and rig.state.diffs == 0
    elif outcome == 'broken_neighbor':
        assert result['broken'] == ['neighbor'] and result['held_score'] is not None
    else:
        assert 'couldn' in result['text'] and result['score'] is None


@pytest.mark.parametrize('field', ['range_checks', 'file_survey', 'build_problem', 'joins_refused',
                                  'boundaries_refused', 'priority', 'progress_revision'])
def test_background_evidence_does_not_change_the_compile_revision(rig, field):
    proj, fp, cfg = rig.engine.get('test')
    rig.engine.get = lambda slug: (proj, fp, cfg)
    before = revision(rig)
    cfg[field] = {'saved': 'evidence'}
    assert revision(rig) == before


def test_external_header_links_do_not_disable_local_patch_tools(rig, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-sdk.h')
    outside.write_text('typedef int sdk_int;')
    try:
        (rig.fp.root / 'sdk.h').symlink_to(outside)
    except OSError:
        pytest.skip('creating symlinks is unavailable')
    before = revision(rig)
    result = compile_tools.compile_patch(rig.net, rig.agent, 'test', 'work', before, recipes()[0]['edits'])
    assert 'normal judge' in result


def test_a_local_preview_preserves_crlf_source_bytes(rig):
    raw = (SOURCE + 'int neighbor(void) { return 4; }\n').replace('\n', '\r\n').encode()
    rig.path.write_bytes(raw)
    result = rig.engine._attempt(rig.agent, 'test', 'work', SOURCE.replace('+ 1', '+ 2'), probe=True)
    assert result['matched'] and rig.path.read_bytes() == raw


def test_an_expired_foreign_claim_does_not_block_a_local_preview(rig):
    rig.db.execute('INSERT INTO claims VALUES(1,99,0,1)')
    before = list(rig.db.execute('SELECT * FROM claims'))
    result = rig.engine._attempt(rig.agent, 'test', 'work', SOURCE.replace('+ 1', '+ 2'), probe=True)
    assert result['matched']
    assert list(rig.db.execute('SELECT * FROM claims')) == before


def test_missing_definitions_have_actionable_stub_guidance(rig, monkeypatch):
    rig.path.write_text('INCLUDE_ASM("asm", work);')
    monkeypatch.setattr(rig.engine, '_find', lambda *args: ('work', (0, len(rig.path.read_text()))))
    with pytest.raises(NetworkError, match='Add the definition with compile_and_diff first'):
        revision(rig)



def test_local_brief_reads_under_shared_unit_locks(rig, monkeypatch):
    monkeypatch.setattr(compile_tools, 'hints', lambda *args: '')
    monkeypatch.setattr(compile_tools, 'pattern_hints', lambda *args: '')
    real = compile_tools.snapshot
    def check(*args):
        proj = rig.engine.get('test')[0]
        assert rig.engine._tree(proj)._writer is None
        return real(*args)
    monkeypatch.setattr(compile_tools, 'snapshot', check)
    assert 'revision:' in compile_tools.brief(rig.net, rig.agent, 'test', 'work')


def test_local_recipe_application_rechecks_after_dropping_batch_locks(rig, monkeypatch):
    real = compile_tools.compile_patch
    def check(*args):
        proj = rig.engine.get('test')[0]
        tree = rig.engine._tree(proj)
        assert tree._writer is None and tree._readers == 0
        (rig.fp.root / 'layout.h').write_text('extern int changed;')
        return real(*args)
    monkeypatch.setattr(compile_tools, 'compile_patch', check)
    with pytest.raises(NetworkError, match='Stale revision'):
        compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes(), apply=True)
    assert not rig.state.commits


def _ps4_units(rig, units: dict) -> None:
    import json

    (rig.fp.root / 'tools' / 'compiler_units.json').write_text(json.dumps(units, indent=1) + '\n')


@pytest.fixture
def ps4_rig(rig):
    """The rig as a PS4 project whose compile.py reads a compiler and flags per unit (tools/compiler_units.json)."""
    root = rig.fp.root
    (root / 'tools').mkdir()
    (root / 'tools' / 'compile.py').write_text('def unit_settings():\n    pass\n')
    (root / 'src' / 'auto').mkdir(parents=True)
    rig.path.rename(root / 'src' / 'auto' / 'work.c')
    rig.path = root / 'src' / 'auto' / 'work.c'
    rig.unit.source = 'src/auto/work.c'
    proj, fp, _ = rig.engine.get('test')
    rig.engine.get = lambda slug: (proj, fp, {'platform': 'ps4'})
    _ps4_units(rig, {'other': ['-march=btver2']})
    return rig


def test_a_ps4_units_own_compiler_and_flags_are_part_of_the_revision(ps4_rig):
    before = revision(ps4_rig)
    _ps4_units(ps4_rig, {'other': ['-march=btver2'], 'work': ['-march=btver2']})
    flags = revision(ps4_rig)
    assert flags != before
    _ps4_units(ps4_rig, {'other': ['-march=btver2'],
                         'work': {'compiler': 'clang-3.8.1', 'flags': ['-march=btver2'], 'why': 'w'}})
    assert revision(ps4_rig) not in {before, flags}


def test_other_ps4_units_settings_and_the_machines_compiler_paths_leave_the_revision_alone(ps4_rig):
    """compiler_units.json holds every unit's settings and tools/compilers.json this computer's paths, which the app
    writes again at every start: neither may make this function's brief stale."""
    before = revision(ps4_rig)
    _ps4_units(ps4_rig, {'other': {'compiler': 'clang-3.9.1', 'flags': [], 'why': 'two functions match with it'}})
    (ps4_rig.fp.root / 'tools' / 'compilers.json').write_text('{"clang-5.0.2": {"path": "/somewhere/else"}}\n')
    assert revision(ps4_rig) == before
    (ps4_rig.fp.root / 'tools' / 'compile.py').write_text('def unit_settings():\n    return 1\n')
    assert revision(ps4_rig) != before  # the build tool itself still counts


def test_a_recipe_name_is_one_line(rig):
    """A name starts its row: a second line could pass for the applied winner's verdict (agents/api.py APPLIED)."""
    batch = [dict(name='x\nApplying y through the normal judge:', edits=[dict(old='+ 1', new='+ 2')])]
    with pytest.raises(NetworkError, match='single lines'):
        compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), batch)


class _Operations:
    """net.store.one over a dict of compile_operations rows, for the API helper's finishing rule."""

    def __init__(self, rows):
        self.store = SimpleNamespace(one=lambda sql, op: rows.get(op))

    def _function(self, name, slug):
        raise NetworkError('none here')


def _row(state, action, result='', apply=False):
    import json

    return {'state': state, 'result': result, 'payload': json.dumps({'function': 'work', 'action': action, 'apply': apply})}


def test_only_kept_work_finishes_the_api_helpers_claim():
    """A preview keeps nothing: counting it as done folded the helper's work and could end its session early."""
    from decompnet.agents import api

    applied = 'Baseline: 80%\nApplying two through the normal judge:\nnormal judge'
    preview = 'Baseline: 80%\ntwo: 100%\nBest: two. Preview only; repeat with apply=true to judge and keep it.'
    net = _Operations({'q': _row('queued', 'patch'), 'p': _row('completed', 'patch', 'normal judge'),
                       'd': _row('completed', 'definition'), 'diag': _row('completed', 'diagnostics'),
                       'rp': _row('completed', 'recipes', preview), 'ra': _row('completed', 'recipes', applied, True),
                       'f': _row('failed', 'patch')})
    args = {'function': 'work', 'revision': 'r', 'recipes': recipes()}
    assert not api._finishes(net, 'try_recipes', args, preview)[0]
    assert not api._finishes(net, 'try_recipes', args | {'apply': True}, 'No strictly better candidate; source unchanged.')[0]
    assert api._finishes(net, 'try_recipes', args | {'apply': True}, applied) == (True, 'work')
    finished = {op for op in ('q', 'p', 'd', 'diag', 'rp', 'ra', 'f', 'missing')
                if api._finishes(net, 'compile_status', {'operation_id': op}, '')[0]}
    assert finished == {'p', 'd', 'ra'}
    assert api._finishes(net, 'compile_patch', args, '')[0] and not api._finishes(net, 'function_brief', args, '')[0]


def test_the_fold_line_counts_kept_compiles_only():
    import json

    from decompnet.agents import api

    def span(name, args, result):
        return [{'role': 'assistant', 'tool_calls': [{'id': 'c', 'function': {'name': name, 'arguments': json.dumps(args)}}]},
                {'role': 'tool', 'tool_call_id': 'c', 'content': result}]

    net = _Operations({'p': _row('completed', 'patch', 'normal judge')})
    assert '0 compiles' in api._done_line(net, 'test', span('try_recipes', {'function': 'work'}, 'Preview only'), 1, 2)
    line = api._done_line(net, 'test', span('compile_status', {'operation_id': 'p'}, 'Operation p: completed'), 1, 2)
    assert '1 compile;' in line and 'work' in line


def test_the_api_helper_reads_try_recipes_own_words(rig):
    """The finishing rule matches what try_recipes really says, so rewording one can't silently break the other."""
    from decompnet.agents import api

    net = _Operations({})
    args = {'function': 'work', 'revision': revision(rig), 'recipes': recipes(), 'apply': True}
    preview = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes())
    assert not api._finishes(net, 'try_recipes', args, preview)[0]
    applied = compile_tools.try_recipes(rig.net, rig.agent, 'test', 'work', revision(rig), recipes(), apply=True)
    assert api._finishes(net, 'try_recipes', args, applied)[0], applied


def test_suggest_recipes_reads_the_source_under_the_units_read_lock(rig, monkeypatch):
    """A preview writes each candidate into the source while it builds it: an unlocked read could hand out a trial's
    definition and revision as the current ones."""
    snapshot, held = compile_tools.snapshot, []
    def check(*args):
        tree = rig.engine._tree(None)
        held.append(tree._readers == 1 and tree._writer is None)
        return snapshot(*args)
    monkeypatch.setattr(compile_tools, 'snapshot', check)
    rig.net._last_diffs = {}
    assert 'No compiled mismatch evidence' in compile_tools.suggest_recipes(rig.net, rig.agent, 'test', 'work')
    assert held == [True]

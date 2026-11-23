"""Synthetic families and read-only SQLite fixtures; no game assets or compiler needed."""

import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from decompnet import patterns
from decompnet.agents.api import api_tool_schemas
from decompnet.app import function_patterns as app
from decompnet.app import tools
from decompnet.arch.patterns import families
from decompnet.codeblocks import Instruction

VECTOR = ('vmulps', 'vaddps', 'vshufps', 'vsubps', 'vdivps', 'vsqrtps', 'vmaxps', 'vminps', 'ret')
INTEGER = ('mov', 'xor', 'shl', 'add', 'and', 'or', 'shr', 'mov', 'ret')


def test_retrieval_finds_held_out_family_without_using_source():
    entries = [patterns.Entry(1, 'vector', 'a', VECTOR, 'float f(){return _mm_add_ps(a,b);}'),
               patterns.Entry(2, 'integer', 'a', INTEGER, 'unsigned f(){return a^b;}')]
    index = patterns.Index(entries)
    query = VECTOR[:-2]+('vmaxps', 'ret')
    assert index.rank(query)[0][1].id == 1
    swapped = [patterns.Entry(e.id, e.name, e.unit, e.ops, entries[1-i].source) for i, e in enumerate(entries)]
    assert patterns.Index(swapped).rank(query)[0][1].id == 1
    assert all(e.id != 1 for _, e in index.rank(query, exclude=1))


def test_traits_ignore_comments_and_ordinary_strings_and_track_exceptions():
    assert not patterns.source_traits('/* volatile */ void f(){puts("unsigned memcpy(x)"); // for(;;)\n}')
    a = patterns.Entry(1, 'a', 'unit', VECTOR, '__attribute__((target("arch=btver2"))) unsigned f(){return 0;}')
    b = patterns.Entry(2, 'b', 'unit', VECTOR, 'int f(){return 0;}')
    result = patterns.evidence([a, b])
    assert ('target attribute arch=btver2', 1, 2, ['b']) in result
    assert ('explicit unsigned type', 1, 2, ['b']) in result


def test_held_out_evaluation_excludes_self_and_clones():
    source = 'unsigned f(){return x+1;}'
    a = patterns.Entry(1, 'a', 'u', INTEGER, source)
    b = patterns.Entry(2, 'b', 'u', INTEGER, 'unsigned g(){return y+1;}')
    assert patterns.evaluate([a, b], 2) == {'tested': 2, 'retrieved': 0, 'trait_agreement': 0, 'random_trait_agreement': 0}
    c = patterns.Entry(3, 'c', 'u', INTEGER[:-2]+('xor', 'ret'), 'unsigned h(){return x+2;}')
    result = patterns.evaluate([a, b, c], 3)
    assert result == {'tested': 3, 'retrieved': 3, 'trait_agreement': 3, 'random_trait_agreement': 3}
    assert patterns.evaluate([], 3)['tested'] == 0


def test_architecture_family_labels_are_observations():
    assert 'AVX floating-point operations' in families('x86_64', VECTOR)
    assert 'contains calls' in families('mips', ('jal', 'nop', 'jr', 'nop'))
    assert 'AVX floating-point operations' not in families('mips', VECTOR)


@pytest.fixture
def project(tmp_path, monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE projects(id,slug,arch);
    CREATE TABLE functions(id,project_id,name,unit,meta,vram,size,n_insns,status,matched_source);
    INSERT INTO projects VALUES(1,'test','x86_64');''')
    rows = [(1, 1, 'query', 'u', '{}', 0, 40, 200, 'open', None),
            (2, 1, 'donor', 'u', '{}', 40, 40, 9, 'matched', 'unsigned f(){return 1;}'),
            (3, 1, 'other', 'v', '{}', 80, 40, 9, 'matched', 'void f(){__atomic_signal_fence(4);}'),
            (4, 1, 'unmatched', 'u', '{}', 120, 40, 9, 'open', 'unsigned f(){return 2;}')]
    db.executemany('INSERT INTO functions VALUES(?,?,?,?,?,?,?,?,?,?)', rows)
    class Store:
        def all(self, sql, *args):
            return db.execute(sql, args).fetchall()

        def one(self, sql, *args):
            return db.execute(sql, args).fetchone()
    target = tmp_path/'target.o'
    target.write_bytes(b'unchanged')
    units = [SimpleNamespace(name=name, target=target) for name in ('u', 'v')]
    indexed = {name: {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'ops': {'query': VECTOR, 'donor': VECTOR,
               'other': VECTOR[:-2]+('vmaxps', 'ret'), 'unmatched': VECTOR}} for name in ('u', 'v')}
    fp = SimpleNamespace(units=lambda: units, unit=lambda name: next(u for u in units if u.name == name))
    net = SimpleNamespace(store=Store(), fork_slug=lambda slug: slug)
    net._project = lambda slug: net.store.one('SELECT * FROM projects WHERE slug=?', slug)
    net._function = lambda name, slug: net.store.one('SELECT * FROM functions WHERE name=?', name)
    net.forks = SimpleNamespace(_ilock=threading.RLock(), _index_of=lambda proj, fp: indexed,
                               get=lambda slug: (None, fp, None), _meta=lambda row: json.loads(row['meta']))
    monkeypatch.setattr(app, 'finder', lambda net: SimpleNamespace(
        unit_code=lambda fp, unit: {'query': [Instruction(i, op) for i, op in enumerate(VECTOR)]},
        rom_code=lambda fn, proj: [Instruction(i, op) for i, op in enumerate(VECTOR)]))
    lib = app.Library(net)
    monkeypatch.setattr(app, 'library', lambda net: lib)
    yield SimpleNamespace(net=net, lib=lib, db=db, target=target, indexed=indexed)
    db.close()


def test_tool_is_read_only_scoped_and_never_uses_unmatched_source(project):
    before = '\n'.join(project.db.iterdump())
    project.db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in (
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK)
    text = app.function_patterns(project.net, 'test', 'query')
    assert 'donor [u]' in text and 'other [v]' in text
    assert 'unmatched' not in text
    assert 'single example' in text and 'different unit; compiler compatibility unverified' in text
    assert 'u: explicit unsigned type: 1/1' in text
    assert 'v: signal fence: 1/1' in text
    assert '\n'.join(project.db.iterdump()) == before
    assert project.target.read_bytes() == b'unchanged'


def test_cache_refreshes_for_revoked_matches_source_and_target_changes(project):
    proj = project.net._project('test')
    first, _ = project.lib.corpus(proj)
    assert project.lib.corpus(proj)[0] is first
    project.db.execute('UPDATE functions SET matched_source="void f(){}" WHERE id=2')
    assert 'explicit unsigned' not in app.function_patterns(project.net, 'test', 'query')
    project.db.execute('UPDATE functions SET status="open" WHERE id=2')
    assert 'donor [u]' not in app.function_patterns(project.net, 'test', 'query')
    assert project.lib.corpus(proj)[0] is not first
    project.indexed['v']['sha256'] = 'stale'
    assert 'No sufficiently similar' in app.function_patterns(project.net, 'test', 'query')


def test_rom_mode_and_bounded_report(project):
    project.net.fork_slug = lambda slug: None
    project.net._rom_ops = lambda proj: {(40, 40): VECTOR, (80, 40): VECTOR[:-2]+('vmaxps', 'ret')}
    text = app.function_patterns(project.net, 'test', 'query')
    assert 'donor [u]' in text
    report = app.pattern_index_report(project.net, 'test', 2)
    assert '2 queries' in report and 'not semantic-family accuracy' in report


def test_hints_fail_open_and_tools_exposed_in_both_modes(project, monkeypatch):
    fn = project.net._function('query', 'test')
    assert 'Matched-function patterns' in app.hints(project.net, fn)
    monkeypatch.setattr(project.lib, 'find', lambda *args: (_ for _ in ()).throw(ValueError('bad index')))
    assert app.hints(project.net, fn) == ''
    for fork in (False, True):
        names = {t['function']['name'] for t in api_tool_schemas(fork)}
        assert {'function_patterns', 'pattern_index_report'} <= names
        assert {'function_patterns', 'pattern_index_report'} <= set(tools.tool_names(fork))
    assert tools.build_mcp(project.net, lambda ctx: (None, 'test'))


def test_unrelated_functions_abstain_and_snapshot_budget_is_visible(project, monkeypatch):
    index = patterns.Index([patterns.Entry(1, 'a', 'u', ('xor',)*8)])
    assert not index.rank(('vaddps',)*8)
    monkeypatch.setattr(app, 'MAX_TOTAL_OPS', 8)
    index, coverage = project.lib.corpus(project.net._project('test'))
    assert not index.entries
    assert '0/2' in coverage and 'limits excluded' in coverage


def test_oversized_source_is_not_mined_or_echoed(project):
    project.db.execute('UPDATE functions SET matched_source=? WHERE id=2', ('volatile '+ ' '*patterns.MAX_SOURCE,))
    text = app.function_patterns(project.net, 'test', 'query')
    assert 'donor [u]' in text and 'volatile spelling' not in text


def test_rom_brief_and_autopilot_include_pattern_hints(monkeypatch):
    from decompnet.app import code_blocks
    from decompnet.app.engine.briefs import BriefMixin

    class Base:
        def _brief(self, fn, agent, header):
            return 'Original brief'
    class Brief(BriefMixin, Base):
        pass
    fn = {'id': 1, 'project_id': 1, 'name': 'query', 'n_insns': 200, 'status': 'open', 'best_source': 'existing C'}
    net = Brief()
    net.store = SimpleNamespace(one=lambda *args: {'id': 1, 'slug': 'test', 'config': '{}'}, all=lambda *args: [fn])
    net.cfg, net.is_rom = lambda row: {'kind': 'rom'}, lambda row: True
    monkeypatch.setattr(code_blocks, 'hints', lambda *args: '')
    monkeypatch.setattr(app, 'hints', lambda *args: '\nPattern donor evidence')
    assert net._brief(fn, None, 'CLAIM') == 'Original brief\nPattern donor evidence'
    state = {'stop': threading.Event()}
    code_blocks.warm(net, {'id': 1}, state)
    assert state['pattern_hints'] == 1 and state['block_scanned'] == 1



def test_traversal_bundle_keeps_all_three_observations_together():
    source = '''__attribute__((target("arch=btver2"))) void *f(void **next) {
        void *p = __atomic_load_n((void **)(next), __ATOMIC_ACQUIRE);
        __atomic_signal_fence(__ATOMIC_ACQ_REL); return p;
    }'''
    traits = patterns.source_traits(source)
    assert patterns.TRAVERSAL_BUNDLE in traits
    for before, after in [('__ATOMIC_ACQUIRE', '__ATOMIC_RELAXED'),
                          ('__ATOMIC_ACQ_REL', '__ATOMIC_ACQUIRE'), ('arch=btver2', 'avx')]:
        assert patterns.TRAVERSAL_BUNDLE not in patterns.source_traits(source.replace(before, after))
    donor = patterns.Entry(1, 'donor', 'unit', VECTOR, source)
    assert patterns.evidence([donor])[0][0] == patterns.TRAVERSAL_BUNDLE
    text = app.render({'unit': 'unit'}, 'coverage', [(1, donor)], compact=True)
    assert patterns.TRAVERSAL_BUNDLE in text and 'single example' in text


def test_weak_noinline_is_a_scoped_spelling_not_a_proven_fix():
    assert patterns.WEAK_NOINLINE in patterns.source_traits('__attribute__((noinline, weak)) void f(void){}')
    for source in ['__attribute__((noinline)) void weak(void){}',
                   '/* __attribute__((weak,noinline)) */ void f(void){}',
                   'void f(void){puts("__attribute__((weak,noinline))");}']:
        assert patterns.WEAK_NOINLINE not in patterns.source_traits(source)
    assert 'acquire atomic load' not in patterns.source_traits(
        'void f(void){__atomic_load_n(p,__ATOMIC_RELAXED); other(x,__ATOMIC_ACQUIRE);}')


def test_donor_content_changes_cannot_reuse_the_index_with_preserved_mtime(project):
    import os
    assert 'donor [u]' in app.function_patterns(project.net, 'test', 'query')
    stamp = project.target.stat()
    project.target.write_bytes(b'different')
    os.utime(project.target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert 'No sufficiently similar' in app.function_patterns(project.net, 'test', 'query')


def test_repeated_donor_lookups_share_target_digests(project, monkeypatch):
    from pathlib import Path

    from decompnet.app import target_stamps
    now = target_stamps.time.time_ns()
    monkeypatch.setattr(target_stamps.time, 'time_ns', lambda: now + 3_000_000_000)
    opened = []
    original = Path.open
    def read(path, *args, **kwargs):
        if path == project.target:
            opened.append(1)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', read)
    first = app.function_patterns(project.net, 'test', 'query')
    assert 'donor [u]' in first
    assert app.function_patterns(project.net, 'test', 'query') == first
    assert len(opened) == 1, 'the unchanged object was reread for each corpus lookup'

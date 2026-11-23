"""Persistent blockers use synthetic claims and revisions, without launching helpers or touching source trees."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from decompnet.app import specialists
from decompnet.network import NetworkError

SCHEMA = '''
CREATE TABLE claims(function_id INTEGER PRIMARY KEY,agent_id INTEGER,expires_at REAL);
CREATE TABLE functions(id INTEGER PRIMARY KEY,project_id INTEGER,name TEXT,n_insns INTEGER);
CREATE TABLE specialist_requests(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,function_id INTEGER NOT NULL,
owner_id INTEGER NOT NULL,assigned_id INTEGER,category TEXT NOT NULL,question TEXT NOT NULL,evidence TEXT NOT NULL,
revision TEXT NOT NULL,status TEXT NOT NULL,findings TEXT NOT NULL DEFAULT '',proposals TEXT NOT NULL DEFAULT '[]',
created_at REAL NOT NULL,expires_at REAL NOT NULL,finished_at REAL);
INSERT INTO functions VALUES(1,1,'work',250);
INSERT INTO claims VALUES(1,1,1500);
'''
QUESTION = 'Which instruction sequence causes the first branch mismatch?'
EVIDENCE = 'Block +0x20 branches to +0x40; the last two recipes left the same mismatch.'


@pytest.fixture
def rig(monkeypatch):
    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    lock = threading.RLock()

    @contextmanager
    def tx():
        with lock:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def one(sql, *args):
        with lock:
            return db.execute(sql, args).fetchone()

    state = SimpleNamespace(revision='definition:first')
    monkeypatch.setattr(specialists.time, 'time', lambda: 1000)
    monkeypatch.setattr(specialists, 'revision', lambda *args: state.revision)
    net = SimpleNamespace(store=SimpleNamespace(tx=tx, one=one),
                          _project=lambda slug: {'id': 1, 'slug': slug},
                          _function=lambda *args: one('SELECT * FROM functions WHERE id=1'),
                          experiment_history=lambda *args, **kwargs: 'Shared result: recipe A already tried; no improvement.')
    state.net, state.db, state.owner = net, db, SimpleNamespace(id=1)
    state.helper = SimpleNamespace(id=2)
    state.request = lambda: specialists.request(net, state.owner, 'p', 'work', 'codegen', QUESTION, EVIDENCE)
    yield state
    db.close()


def test_request_requires_current_owner_and_deduplicates_same_blocker(rig):
    with pytest.raises(NetworkError, match='unexpired claim'):
        specialists.request(rig.net, rig.helper, 'p', 'work', 'codegen', QUESTION, EVIDENCE)
    rig.request()
    assert 'Existing blocker' in rig.request()
    assert rig.db.execute('SELECT count(*) FROM specialist_requests').fetchone()[0] == 1
    with pytest.raises(NetworkError, match='already covers'):
        specialists.request(rig.net, rig.owner, 'p', 'work', 'layout', QUESTION, EVIDENCE)


def test_only_one_concurrent_helper_takes_a_blocker_and_owner_cannot(rig):
    rig.request()
    assert specialists.take(rig.net, rig.owner, 'p') is None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda agent: specialists.take(rig.net, SimpleNamespace(id=agent), 'p'), [2, 3]))
    assert sum(result is not None for result in results) == 1


def test_helper_with_own_claim_cannot_take_or_publish(rig):
    rig.request()
    with rig.net.store.tx() as db:
        db.execute('INSERT INTO claims VALUES(2,2,1500)')
    assert specialists.take(rig.net, rig.helper, 'p') is None
    with rig.net.store.tx() as db:
        db.execute('DELETE FROM claims WHERE function_id=2')
    specialists.take(rig.net, rig.helper, 'p')
    with rig.net.store.tx() as db:
        db.execute('INSERT INTO claims VALUES(2,2,1500)')
    with pytest.raises(NetworkError, match='hold no source claim'):
        specialists.report(rig.net, rig.helper, 'p', 1, 'Observed a different stack copy width in the target.')


def test_reports_share_evidence_and_proposals_without_applying_them(rig):
    rig.request()
    task = specialists.take(rig.net, rig.helper, 'p')
    assert task['owner_id'] == 1 and task['assigned_id'] == 2
    assert 'already tried' in specialists.brief(rig.net, rig.helper, 'p', 1)
    proposal = [{'name': 'copy width', 'edits': [{'old': 'old code', 'new': 'proposed code'}]}]
    result = specialists.report(rig.net, rig.helper, 'p', 1, 'The target uses two 64-bit loads for this copy.', proposal)
    assert 'completed' in result and 'not applied' in result
    assert specialists.completed(rig.net, rig.helper, 'p', 1)
    assert 'proposed code' in specialists.status(rig.net, rig.owner, 'p', 'work')
    assert 'Existing blocker' in rig.request()


def test_source_change_marks_report_stale_and_preserves_proposal(rig):
    rig.request()
    specialists.take(rig.net, rig.helper, 'p')
    rig.revision = 'definition:second'
    assert 'STALE revision' in specialists.brief(rig.net, rig.helper, 'p', 1)
    text = specialists.report(rig.net, rig.helper, 'p', 1, 'Instruction scheduling differs around the second load.')
    assert ': stale ' in text
    assert 'scheduling differs' in specialists.status(rig.net, rig.owner, 'p', 'work')


def test_completed_report_becomes_stale_when_owner_changes_source(rig):
    rig.request()
    specialists.take(rig.net, rig.helper, 'p')
    specialists.report(rig.net, rig.helper, 'p', 1, 'Instruction scheduling differs around the second load.')
    rig.revision = 'definition:second'
    assert ': stale ' in specialists.status(rig.net, rig.owner, 'p', 'work')


def test_owner_claim_loss_and_ttl_cancel_outstanding_tasks(rig):
    rig.request()
    specialists.take(rig.net, rig.helper, 'p')
    with rig.net.store.tx() as db:
        db.execute('DELETE FROM claims WHERE function_id=1')
    assert specialists.completed(rig.net, rig.helper, 'p', 1)
    assert ': cancelled ' in specialists.status(rig.net, rig.owner, 'p', 'work')
    with pytest.raises(NetworkError, match='active request'):
        specialists.report(rig.net, rig.helper, 'p', 1, 'This late report must not replace cancellation.')


def test_guard_remains_read_only_after_cancellation_until_session_finishes(rig):
    rig.request()
    specialists.take(rig.net, rig.helper, 'p')
    specialists.status(rig.net, rig.owner, 'p', 'work', cancel=True)
    for name in ['compile_and_diff', 'try_recipes', 'claim_function', 'set_context', 'unknown_tool']:
        with pytest.raises(NetworkError, match='advisory specialist'):
            specialists.guard(rig.net, rig.helper, 'p', name)
    for name in ['inspect_blocks', 'experiment_history', 'specialist_report']:
        specialists.guard(rig.net, rig.helper, 'p', name)
    specialists.finish(rig.net, rig.helper, 'p', 1)
    specialists.guard(rig.net, rig.helper, 'p', 'claim_function')


def test_finish_marks_abandoned_assignment_failed_and_preserves_completed_reports(rig):
    rig.request()
    specialists.take(rig.net, rig.helper, 'p')
    specialists.finish(rig.net, rig.helper, 'p', 1, 'Helper stopped before findings were available.')
    assert ': failed ' in specialists.status(rig.net, rig.owner, 'p', 'work')


@pytest.mark.parametrize('proposals', [[{}] * 5, [{'name': 'recipe', 'edits': [{'old': '', 'new': 'x'}]}],
                                      [{'name': 'recipe', 'edits': [{'old': str(n), 'new': 'b'} for n in range(17)]}],
                                      [{'name': 'recipe', 'edits': [{'old': 'a', 'new': 'x' * 65536}]}]])
def test_proposal_limits_reject_unbounded_or_inexact_recipes(rig, proposals):
    rig.request()
    specialists.take(rig.net, rig.helper, 'p')
    with pytest.raises(NetworkError):
        specialists.report(rig.net, rig.helper, 'p', 1, 'A sufficiently concrete finding about the target.', proposals)
    assert not specialists.completed(rig.net, rig.helper, 'p', 1)


def test_expiration_is_bounded_even_when_owner_claim_remains_valid(rig, monkeypatch):
    rig.request()
    with rig.net.store.tx() as db:
        db.execute('UPDATE claims SET expires_at=5000')
    monkeypatch.setattr(specialists.time, 'time', lambda: 1601)
    assert specialists.take(rig.net, rig.helper, 'p') is None
    assert ': cancelled ' in specialists.status(rig.net, rig.owner, 'p', 'work')


def test_only_owner_or_assigned_specialist_can_read_results(rig):
    rig.request()
    specialists.take(rig.net, rig.helper, 'p')
    outsider = SimpleNamespace(id=3)
    with pytest.raises(NetworkError, match='owner or assigned'):
        specialists.brief(rig.net, outsider, 'p', 1)
    with pytest.raises(NetworkError, match='owner or assigned'):
        specialists.status(rig.net, outsider, 'p', 'work')
    with pytest.raises(NetworkError, match='Only the source owner'):
        specialists.status(rig.net, rig.helper, 'p', 'work', cancel=True)


def test_specialist_read_budget_requires_a_report_after_sixteen_inspections(rig):
    rig.request()
    specialists.take(rig.net, rig.helper, 'p')
    for _ in range(specialists.MAX_READ_TOOLS):
        specialists.guard(rig.net, rig.helper, 'p', 'inspect_blocks')
    with pytest.raises(NetworkError, match='budget reached'):
        specialists.guard(rig.net, rig.helper, 'p', 'experiment_history')
    specialists.guard(rig.net, rig.helper, 'p', 'report_specialist')
    specialists.guard(rig.net, rig.helper, 'p', 'specialist_status')


def test_first_definition_revision_falls_back_to_source_header_target_and_build_context(tmp_path, monkeypatch):
    from decompnet.app import compile_tools

    def absent(*args):
        raise NetworkError('Add the definition first.')

    monkeypatch.setattr(compile_tools, 'snapshot', absent)
    (tmp_path / 'unit.c').write_text('/* definition not written yet */')
    (tmp_path / 'types.h').write_text('typedef int value;')
    target = tmp_path / 'unit.o'
    target.write_bytes(b'synthetic target')
    unit = SimpleNamespace(source='unit.c', target=target)
    project = SimpleNamespace(root=tmp_path, unit=lambda name: unit, safe_path=lambda path: tmp_path / path)
    config = {'compiler': 'synthetic'}
    net = SimpleNamespace(_project=lambda slug: {'id': 1, 'config': '{}'}, fork_slug=lambda slug: slug,
                          forks=SimpleNamespace(get=lambda slug: ({}, project, config)))
    fn = {'id': 1, 'name': 'work', 'unit': 'unit', 'best_source': None}
    first = specialists.revision(net, 'p', fn)
    assert first.startswith('context-advisory:')
    (tmp_path / 'types.h').write_text('typedef long value;')
    second = specialists.revision(net, 'p', fn)
    assert first != second
    config['compiler'] = 'another synthetic compiler'
    assert second != specialists.revision(net, 'p', fn)


def test_rom_revision_changes_with_shared_header_and_candidate_source():
    context = {'header': 'typedef int value;'}
    net = SimpleNamespace(_project=lambda slug: {'id': 1, 'config': '{}'}, fork_slug=lambda slug: None,
                          _context=lambda *args: context)
    fn = {'id': 1, 'name': 'work', 'unit': '', 'target': b'\x90\xc3', 'best_source': None}
    first = specialists.revision(net, 'p', fn)
    context['header'] = 'typedef long value;'
    second = specialists.revision(net, 'p', fn)
    assert first != second
    fn['best_source'] = 'int work(void) { return 1; }'
    assert second != specialists.revision(net, 'p', fn)


def test_existing_definition_revision_remains_usable_by_try_recipes(monkeypatch):
    from decompnet.app import compile_tools

    stamp = 'a' * 64
    monkeypatch.setattr(compile_tools, 'snapshot', lambda *args: ({}, 'int work(void) { return 1; }', stamp))
    net = SimpleNamespace(_project=lambda slug: {'id': 1}, fork_slug=lambda slug: slug, forks=object())
    assert specialists.revision(net, 'p', {'name': 'work'}) == stamp

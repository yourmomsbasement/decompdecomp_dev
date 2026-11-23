"""Bounded tiny-function solving through the existing strict judge, before autopilot drafts.

Claims are acquired transactionally, project previews restore the tree, and only exact winners are applied through
ordinary attempt handling. Unsupported signatures are reported, never guessed. No persistent negative rule is
learned across units or compiler settings: identical instructions can need different C under different ABIs.
"""

import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager

from .. import elfobj, scoring, tiny
from .code_blocks import finder

MAX_FUNCTIONS = 100


@contextmanager
def held(net, agent, fid):
    """Reserve only a fresh open/unclaimed function, and release just this solver's claim even on errors."""
    with net.store.tx() as db:
        net._expire_claims(db)
        fn = db.execute('SELECT * FROM functions WHERE id=?', (fid,)).fetchone()
        available = fn is not None and fn['status'] == 'open' and not db.execute(
            'SELECT 1 FROM claims WHERE function_id=?', (fid,)).fetchone()
        if available:
            now = time.time()
            db.execute('INSERT INTO claims VALUES(?,?,?,?)', (fid, agent.id, now, now+net.claim_ttl))
    try:
        yield fn if available else None
    finally:
        if available:
            with net.store.tx() as db:
                db.execute('DELETE FROM claims WHERE function_id=? AND agent_id=?', (fid, agent.id))


def processor(fp, unit, symbol, recorded_arch):
    """Prefer a verified target function's ELF processor; never infer an ABI from assembly spellings.

    Project imports can leave their architecture blank. COFF and other unsupported readers yield no recipe;
    the fallback only supports callers without an object target interface, such as lightweight engine adapters.
    """
    from elftools.common.exceptions import ELFError

    if not hasattr(unit, 'target'):
        return recorded_arch or ''
    if unit.target is None:
        return ''
    try:
        obj = elfobj.extract_function(unit.target.read_bytes(), symbol)
        return obj.arch if obj is not None else ''
    except (ELFError, OSError, ValueError, KeyError, TypeError, IndexError):
        return ''


def inspect(net, proj, fn):
    """Read actual target instructions and existing C signatures, not an inferred database instruction count."""
    blocks = finder(net)
    if fork := net.fork_slug(proj['slug']):
        engine = net.forks
        _, fp, _ = engine.get(fork)
        unit = fp.unit(fn['unit'])
        if not unit.source:
            return None, 'no source file'
        symbol = engine._meta(fn).get('symbol') or fn['name']
        actual_arch = processor(fp, unit, symbol, proj['arch'])
        if not actual_arch:
            return None, 'unsupported or missing target architecture'
        code = blocks.unit_code(fp, unit).get(symbol, [])
        text = fp.safe_path(unit.source).read_text(encoding='utf-8')
        return tiny.candidate(text, symbol, actual_arch, code)
    ctx = net._context(proj['id'], fn['unit'])
    text = ctx['header']+'\n'+(fn['best_source'] or fn['seed'] or '')
    return tiny.candidate(text, fn['name'], proj['arch'], blocks.rom_code(fn, proj))


def solve_one(net, agent, proj, fid):
    """Keep strict matches only. Project probes establish and preserve all existing matched neighbors."""
    with held(net, agent, fid) as fn:
        if fn is None:
            return 'skipped: claimed or no longer open'
        if fork := net.fork_slug(proj['slug']):
            engine = net.forks
            # Re-read source/signatures under the same project lock as the preview and final ordinary attempt.
            with engine._tree(proj).exclusive():
                source, reason = inspect(net, proj, fn)
                if source is None:
                    return 'skipped: '+reason
                trial = engine._attempt(agent, fork, fn['name'], source, probe=True)
                if not trial['matched']:
                    return 'failed: '+reason
                result = engine.attempt(agent, fork, fn['name'], source, keep=False)
                return 'matched' if result['matched'] else 'failed: revalidation'
        source, reason = inspect(net, proj, fn)
        if source is None:
            return 'skipped: '+reason
        ctx = net._context(proj['id'], fn['unit'])
        result = net._compile(proj, ctx['header'], source, fn)
        if not result.ok:
            return 'failed: compile'
        cand, verdict, _ = net._evaluate(fn, proj, result.obj)
        if cand is None:
            return 'failed: judge'
        diff = scoring.diff(net._target_obj(fn, proj), cand)
        exact = verdict if verdict is not None else diff.bytes_match
        if not exact:
            return 'failed: '+reason
        net._record(fn, agent, 0, source, ctx['version'], matched=True)
        return 'matched'


def run(net, agent, proj, limit=100, apply=False, state=None):
    """Audit by default; autopilot opts into applying matches. One pass examines each selected function once."""
    if proj['slug'] in net.links:
        return 'Tiny solver skipped: room members leave bulk solving to the host.'
    limit = max(1, min(int(limit), MAX_FUNCTIONS))
    stop = state['stop'] if state is not None else threading.Event()
    # where the last pass stopped: this run's (autopilot runs passes in a row), else the last run's
    cursor = (state.get('tiny_cursor') or (net.cfg(proj).get('autopilot') or {}).get('tiny_cursor', 0) or 0
              if state is not None else 0)
    rows = net.store.all('SELECT f.* FROM functions f LEFT JOIN claims c ON c.function_id=f.id '
                         'WHERE f.project_id=? AND f.status="open" AND f.n_insns BETWEEN 1 AND 3 '
                         'AND (c.function_id IS NULL OR c.expires_at<?) ORDER BY (f.id<=?),f.id LIMIT ?',
                         proj['id'], time.time(), cursor, limit)
    outcomes = Counter()
    if state is not None:
        state.update(tiny_total=len(rows), tiny_scanned=0, tiny_matched=0)
    for fn in rows:
        if stop.is_set():
            break
        try:
            if apply:
                outcome = solve_one(net, agent, proj, fn['id'])
            else:
                source, reason = inspect(net, proj, fn)
                outcome = ('candidate: ' if source else 'skipped: ')+reason
        except Exception:  # one malformed target/build cannot stop the remaining autopilot stages
            logging.getLogger('decompdecomp').debug('tiny solver failed for %s', fn['name'], exc_info=True)
            outcome = 'failed: analysis or build (see app log)'
        outcomes[outcome] += 1
        if state is not None:
            state['tiny_cursor'] = fn['id']
            state['tiny_scanned'] += 1
            state['tiny_matched'] += outcome == 'matched'
    mode = 'applied through strict judge' if apply else 'audit only; no compilation or writes'
    return f'Tiny functions: {sum(outcomes.values())}/{len(rows)} examined; {mode}.\n'+'\n'.join(
        f'- {key}: {count}' for key, count in sorted(outcomes.items()))


def solve_tiny_functions(net, agent, slug, limit=100, apply=False):
    if not isinstance(apply, bool):
        from ..network import NetworkError
        raise NetworkError('apply must be true or false.')
    return run(net, agent, net._project(slug), limit, apply)

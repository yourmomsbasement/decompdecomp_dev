"""Bounded source-tree finishing: classify near matches and keep only strict, neighbor-safe winners.

Targets are evidence, never editable candidates. Raw addresses, local tables and branch semantics need review;
this pass only tries local scheduling and unambiguous named-reference corrections through ordinary probes.
"""

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter

from ..arch import x86
from ..forks import csource
from ..network import NetworkError
from .compile_tools import snapshot
from .tiny_functions import held, processor

MAX_FUNCTIONS = 50
MAX_SECONDS = 120


def candidates(source, diff, arch):
    """At most two evidence-directed alternatives; no score rounding or target rewriting."""
    variants = []
    if arch in {'x86', 'x86_64'}:
        variants.extend((item['name'], x86.compiler_recipe(item['name'], source))
                        for item in x86.recipe_suggestions(source, diff.rows))
        rows = [(a, b) for marker, a, b in diff.rows if marker != ' ']
        if rows and Counter(a for a, _ in rows) == Counter(b for _, b in rows) and 'btver2' in source[:source.find('{')]:
            plain = re.sub(r'__attribute__\(\(target\("arch=btver2"\)\)\)\s*', '', source, count=1)
            if plain != source:
                variants.append(('generic scheduling', plain))
    pairs = {}
    for target, old in diff.renamed:
        # Generated local tables and symbols whose meaning changes between data and code are not aliases.
        if not re.fullmatch(r'(?:D|func)_[0-9A-Fa-f]+', target) or not re.fullmatch(
                r'(?:D|func)_[0-9A-Fa-f]+', old) or target.split('_')[0] != old.split('_')[0]:
            return variants
        if old in pairs and pairs[old] != target:
            return variants
        pairs[old] = target
    if not pairs or 'decomp_finish_' in source:
        return variants
    start = source.find('{') + 1
    clean = csource._blank(source)
    edits, declarations = [], []
    for index, (old, target) in enumerate(sorted(pairs.items())):
        uses = list(re.finditer(r'\b' + re.escape(old) + r'\b', clean[start:]))
        if not uses:
            return variants
        alias = f'decomp_finish_{hashlib.sha256(source.encode()).hexdigest()[:12]}_{index}'
        declarations.append(f'extern __typeof__({old}) {alias} __asm__("{target}") '
                            '__attribute__((visibility("hidden")));')
        edits.extend((start + m.start(), start + m.end(), alias) for m in uses)
    candidate = source
    for lo, hi, alias in sorted(edits, reverse=True):
        candidate = candidate[:lo] + alias + candidate[hi:]
    candidate = candidate[:start] + '\n' + '\n'.join(declarations) + '\n' + candidate[start:]
    variants.append(('named references', candidate))
    return variants


def finish_one(net, agent, proj, fid, apply=False):
    with held(net, agent, fid) as fn:
        if fn is None:
            return 'skipped: claimed or no longer open'
        engine = net.forks
        with engine._tree(proj).exclusive():
            _, fp, _ = engine.get(proj['slug'])
            unit = fp.unit(fn['unit'])
            symbol = engine._meta(fn).get('symbol') or fn['name']
            actual_arch = processor(fp, unit, symbol, proj['arch'])
            if not actual_arch:
                return 'skipped: unsupported or missing target architecture'
            if not unit.source or not unit.base or not unit.base.exists():
                return 'skipped: no compiled source'
            diff = fp.diff(unit, symbol)
            if diff.match_percent is None or not 98 <= diff.match_percent < 100:
                return 'skipped: outside 98–100%'
            _, source, revision = snapshot(engine, proj['slug'], fn['name'])
            variants = candidates(source, diff, actual_arch)
            if not variants:
                return 'review: addresses, tables, branches or source layout'
            if not apply:
                return 'candidate: ' + ', '.join(name for name, _ in variants)
            # A cached failed trial is valid only while the source and every hashed build dependency agree.
            seen = getattr(net, '_finishing_seen', None)
            if seen is None:
                seen = net._finishing_seen = set()
            key = (fn['id'], revision)
            if key in seen:
                return 'skipped: already tried this revision'
            text = fp.safe_path(unit.source).read_text(encoding='utf-8')
            if engine._baseline(proj, fp, unit, text)[0] is None:
                return 'skipped: baseline cannot be judged'
            # Re-read the diff after building: stale objects must not supply reference mappings.
            diff = fp.diff(unit, symbol)
            if diff.match_percent is None or not 98 <= diff.match_percent < 100:
                return 'skipped: fresh baseline outside 98–100%'
            variants = candidates(source, diff, actual_arch)
            judged_all = True
            for name, candidate in variants:
                trial = engine._attempt(agent, proj['slug'], fn['name'], candidate, probe=True)
                if snapshot(engine, proj['slug'], fn['name'])[2] != revision:
                    return 'skipped: dependencies changed during preview'
                if trial.get('score') is None and not trial.get('matched'):
                    judged_all = False
                if trial.get('matched'):
                    result = engine.attempt(agent, proj['slug'], fn['name'], candidate, keep=False)
                    return 'matched' if result.get('matched') else 'failed: revalidation'
            if not judged_all:
                return 'retry: a trial could not be judged (build or comparison failed)'
            if len(seen) >= 2048:
                seen.clear()
            seen.add(key)
            return 'review: no exact candidate'


def run(net, agent, proj, limit=50, apply=False, state=None):
    if not isinstance(apply, bool):
        raise NetworkError('apply must be true or false.')
    if not net.fork_slug(proj['slug']):
        return 'Finishing pass currently supports source-tree projects; ROM finishing is not implemented.'
    if proj['slug'] in net.links:
        return 'Finishing skipped: room members leave bulk work to the host.'
    limit = max(1, min(int(limit), MAX_FUNCTIONS))
    stop = state['stop'] if state is not None else threading.Event()
    cursor = (net.cfg(proj).get('autopilot') or {}).get('finish_cursor', 0) or 0 if state is not None else 0
    rows = net.store.all('SELECT f.* FROM functions f LEFT JOIN claims c ON c.function_id=f.id '
                         'WHERE f.project_id=? AND f.status="open" AND f.best_score>0 '
                         'AND (c.function_id IS NULL OR c.expires_at<?) '
                         'ORDER BY (f.id<=?),f.id', proj['id'], time.time(), cursor)
    # The last report is only a cheap shortlist; each candidate is remeasured under the tree lock.
    _, fp, _ = net.forks.get(proj['slug'])
    report = fp.root / '.decomp-wtf' / 'report.json'
    if report.is_file():
        try:
            data = json.loads(report.read_text(encoding='utf-8'))
            near = {(u['name'], f['name']) for u in data.get('units', []) for f in u.get('functions', [])
                    if 98 <= (f.get('fuzzy_match_percent') or 0) < 100}
            rows = [f for f in rows if (f['unit'], net.forks._meta(f).get('symbol') or f['name']) in near]
        except (ValueError, KeyError, OSError):
            pass
    rows = rows[:limit]
    outcomes = Counter()
    if state is not None:
        state.update(finish_total=len(rows), finish_scanned=0, finish_matched=0)
    deadline = time.monotonic() + MAX_SECONDS
    for fn in rows:
        if stop.is_set() or time.monotonic() >= deadline:
            break
        try:
            outcome = finish_one(net, agent, proj, fn['id'], apply)
        except Exception:
            logging.getLogger('decompdecomp').debug('finishing failed for %s', fn['name'], exc_info=True)
            outcome = 'failed: analysis or build (see app log)'
        outcomes[outcome] += 1
        if state is not None:
            state['finish_cursor'] = fn['id']
            state['finish_scanned'] += 1
            state['finish_matched'] += outcome == 'matched'
    mode = 'exact winners applied through strict judge' if apply else 'audit only; no compilation or source writes'
    return f'Finishing: {sum(outcomes.values())}/{len(rows)} examined; {mode}.\n' + '\n'.join(
        f'- {key}: {value}' for key, value in sorted(outcomes.items()))

"""Revision-bound regional hypotheses and advisory before/after differences survive agent handoffs.

These records describe recovery work, including failed ideas and model-based boundary checks. Neither a local
row count nor a supplied integer model proves the whole function correct; acceptance always uses the strict judge.
"""

import hashlib
import json
import time

from ..network import NetworkError
from . import block_inspection, compile_tools

OUTCOMES = {'planned', 'improved', 'unchanged', 'regressed', 'falsified', 'recovered', 'blocked', 'measured'}


def target(net, slug, fn):
    proj = net._project(slug)
    code, obj = block_inspection._target(net, proj, fn)
    digest = hashlib.sha256(json.dumps([(i.offset, i.comparison) for i in code]).encode()
                            + (obj.code if obj else b'')).hexdigest()
    return code, obj, digest


def record(net, agent, slug, function, revision, start, end, hypothesis, evidence, outcome='planned', findings=''):
    """Create immutable evidence for one region and one hypothesis, with an exact current revision."""
    if outcome not in OUTCOMES:
        raise NetworkError('Choose planned, improved, unchanged, regressed, falsified, recovered, or blocked.')
    if not all(isinstance(v, str) for v in (hypothesis, evidence, findings, revision)):
        raise NetworkError('Supply text for the hypothesis, evidence, findings and revision.')
    if not 8 <= len(hypothesis) <= 2000 or not 8 <= len(evidence) <= 4000 or len(findings) > 8000:
        raise NetworkError('Supply a concrete hypothesis/evidence (8–2000/4000 characters); findings at most 8000.')
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        raise NetworkError('Use ordered nonnegative target instruction offsets for start and end (inclusive).')
    engine = net.forks
    proj, _, _ = engine.get(slug)
    with engine._tree(proj).exclusive():
        fn, _, current = compile_tools.snapshot(engine, slug, function)
        if revision != current:
            raise NetworkError('Stale revision: refresh function_brief before recording a new regional experiment.')
        code, _, digest = target(net, slug, fn)
        offsets = {i.offset for i in code}
        if start not in offsets or end not in offsets:
            raise NetworkError('Choose target instruction offsets reported by inspect_blocks.')
        now = time.time()
        with net.store.tx() as db:
            row_id = db.execute('INSERT INTO region_experiments(project_id,function_id,agent_id,revision,target_digest,'
                                'start_offset,end_offset,hypothesis,evidence,outcome,findings,created_at,updated_at) '
                                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (proj['id'], fn['id'], agent.id, revision, digest,
                                 start, end, hypothesis, evidence, outcome, findings, now, now)).lastrowid
    return f'R{row_id}: {outcome}; revision {revision}. Use region_id={row_id} with compile_start. Findings are advisory.'


def history(net, slug, function, limit=6, before=0):
    fn = net._function(function, slug)
    limit = max(1, min(20, int(limit)))
    rows = net.store.all('SELECT * FROM region_experiments WHERE function_id=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?',
                         fn['id'], int(before), int(before), limit)
    if not rows:
        return 'No regional experiments recorded.'
    out = [f"Regional experiments for {fn['name']}; historical hypotheses, not current correctness verdicts:"]
    for row in rows:
        out += [f"R{row['id']} +0x{row['start_offset']:x}..+0x{row['end_offset']:x}: {row['outcome']}; revision {row['revision']}",
                '  Hypothesis: ' + row['hypothesis'], '  Evidence: ' + row['evidence']]
        if row['findings']:
            out.append('  Findings: ' + row['findings'])
        if row['comparison']:
            out.append('  Measured comparison: ' + row['comparison'])
        if row['operation_id']:
            out.append('  Operation: ' + row['operation_id'])
        if row['checks'] != '[]':
            out.append('  Model checks (not executions of target/C): ' + row['checks'][:3000])
    return '\n'.join(out)


def bind(db, fn, agent, region_id, revision):
    row = db.execute('SELECT * FROM region_experiments WHERE id=? AND project_id=? AND function_id=?',
                      (region_id, fn['project_id'], fn['id'])).fetchone()
    if row is None or row['agent_id'] != agent.id or row['revision'] != revision:
        raise NetworkError('Use your own regional experiment for this function at the submitted revision.')
    if row['operation_id']:
        raise NetworkError('That regional experiment already has an operation; record a new hypothesis/trial.')


def last_rows(net, agent, fn):
    last = net._last_diffs.get((agent.id, fn['id']))
    return list(last[0]) if last else None


def compare(code, processor, obj, before, after, start, end):
    if before is None or after is None:
        return 'Unavailable: both fresh target-compatible diffs are needed; no change inferred.'
    left, note1 = block_inspection.localized_rows(code, before)
    right, note2 = block_inspection.localized_rows(code, after)
    if note1 or note2:
        return 'Unavailable: target/diff alignment changed or contains noninstruction rows. No local comparison inferred.'
    blocks, _, _ = block_inspection.analyze(code, processor, obj)
    lines = []
    for block in blocks:
        a = sum(row[0] != ' ' for ins in block.code for row in left.get(ins.offset, []))
        b = sum(row[0] != ' ' for ins in block.code for row in right.get(ins.offset, []))
        if a != b:
            scoped = any(start <= ins.offset <= end for ins in block.code)
            lines.append(f"+0x{block.start:x}: {a}->{b} differing rows ({'selected region' if scoped else 'outside region'}).")
    return (' '.join(lines[:40]) or 'No block-level differing-row count changed; instructions may still differ.') + \
           ' Advisory counts include adjacent candidate insertions, not semantic equivalence or a match score.'


def complete(net, agent, slug, region_id, operation, before, result):
    row = net.store.one('SELECT * FROM region_experiments WHERE id=? AND operation_id=? AND agent_id=?',
                        region_id, operation, agent.id)
    if row is None:
        raise NetworkError('The regional experiment no longer belongs to this operation.')
    fn = net.store.one('SELECT * FROM functions WHERE id=?', row['function_id'])
    code, obj, digest = target(net, slug, fn)
    if digest != row['target_digest']:
        comparison = 'Target evidence changed; regional comparison is stale and unavailable.'
    else:
        proj = net._project(slug)
        comparison = compare(code, obj.arch if obj else proj['arch'], obj, before, last_rows(net, agent, fn),
                             row['start_offset'], row['end_offset'])
    with net.store.tx() as db:
        db.execute("UPDATE region_experiments SET outcome='measured',comparison=?,findings=?,updated_at=? WHERE id=?",
                   (comparison, str(result)[:8000], time.time(), region_id))


def comparison(net, agent, slug, function, region_id):
    fn = net._function(function, slug)
    row = net.store.one('SELECT * FROM region_experiments WHERE id=? AND function_id=?', int(region_id), fn['id'])
    if row is None:
        raise NetworkError('No regional experiment with that ID for this function.')
    _, _, digest = target(net, slug, fn)
    stale = digest != row['target_digest']
    return f"R{row['id']}: " + ('Target evidence changed; historical comparison only. ' if stale else '') + \
           (row['comparison'] or 'No before/after comparison yet. Attach this experiment to compile_start.')


def evaluate(expr, case, depth=0):
    """Small integer-model evaluator; no C evaluation, file access, Python eval or candidate execution."""
    if depth > 24:
        raise NetworkError('Integer model nesting exceeds 24 levels.')
    if isinstance(expr, bool):
        return int(expr)
    if isinstance(expr, int):
        if abs(expr) > 2**128:
            raise NetworkError('Integer model value is too large.')
        return expr
    if isinstance(expr, str) and expr in case:
        return evaluate(case[expr], {}, depth+1)
    if not isinstance(expr, list) or not expr or not isinstance(expr[0], str):
        raise NetworkError('Models use integer literals, input names, and prefix-operation arrays.')
    op, values = expr[0], expr[1:]
    if op == 'select' and len(values) == 3:
        return evaluate(values[1] if evaluate(values[0], case, depth+1) else values[2], case, depth+1)
    if op in {f'{kind}{bits}' for kind in ('u', 'i') for bits in (8, 16, 32, 64)} and len(values) == 1:
        bits = int(op[1:]);v = evaluate(values[0], case, depth+1) & ((1 << bits)-1)
        return v-(1 << bits) if op[0] == 'i' and v >= (1 << (bits-1)) else v
    if len(values) != 2:
        raise NetworkError('Use a supported binary operation, integer cast, or select.')
    a, b = (evaluate(v, case, depth+1) for v in values)
    operations = {'add': lambda: a+b, 'sub': lambda: a-b, 'mul': lambda: a*b, 'and': lambda: a & b,
                  'or': lambda: a | b, 'xor': lambda: a ^ b, 'eq': lambda: int(a == b),
                  'lt': lambda: int(a < b), 'le': lambda: int(a <= b)}
    if op not in operations:
        raise NetworkError('Supported integer operations: add/sub/mul/and/or/xor/eq/lt/le, casts, select.')
    return evaluate(operations[op](), {}, depth+1)


def check(net, agent, slug, function, region_id, target_model, candidate_model, cases):
    fn = net._function(function, slug)
    row = net.store.one('SELECT * FROM region_experiments WHERE id=? AND function_id=? AND agent_id=?',
                        int(region_id), fn['id'], agent.id)
    if row is None:
        raise NetworkError('Choose your own regional experiment for this function.')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1024:
        raise NetworkError('Supply 1–1024 explicit input cases.')
    if len(json.dumps([target_model, candidate_model, cases])) > 64*1024:
        raise NetworkError('Boundary models and cases must fit in 64 KiB.')
    mismatches = []
    mismatch_count = 0
    for case in cases:
        if not isinstance(case, dict) or len(case) > 16 or not all(isinstance(v, int) for v in case.values()):
            raise NetworkError('Each case maps up to16 input names to integer values.')
        a, b = evaluate(target_model, case), evaluate(candidate_model, case)
        if a != b:
            mismatch_count += 1
        if a != b and len(mismatches) < 8:
            mismatches.append({'inputs': case, 'target_model': a, 'candidate_model': b})
    result = {'cases': len(cases), 'mismatch_count': mismatch_count, 'counterexamples': mismatches, 'target_model': target_model,
              'candidate_model': candidate_model, 'scope': 'supplied integer models only; not execution of C or target'}
    checks = json.loads(row['checks'])[-7:] + [result]
    with net.store.tx() as db:
        db.execute('UPDATE region_experiments SET checks=?,updated_at=? WHERE id=?',
                   (json.dumps(checks), time.time(), region_id))
    return json.dumps(result) + '\nA passing model check cannot establish behavior or a strict match.'

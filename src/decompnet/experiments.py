"""Compact, persistent attempt history shared by ROM and source-tree projects.

The existing attempts table is the evidence: context versions are not complete build identities,
so history never reuses verdicts or claims equal scores mean equal generated code.
"""

from __future__ import annotations

import difflib
import hashlib
import time

MAX_HISTORY = 20
CHANGE_CHARS = 480


def history(store, fn, limit: int = 6, before: int = 0) -> str:
    """Read a bounded page plus its predecessor so changes remain meaningful across pages."""
    limit = max(1, min(int(limit), MAX_HISTORY))
    before = max(0, int(before))
    rows = store.all(
        "SELECT t.*, a.name AS agent FROM attempts t LEFT JOIN agents a ON a.id=t.agent_id "
        "WHERE t.function_id=? AND (?=0 OR t.id < ?) ORDER BY t.id DESC LIMIT ?",
        fn["id"], before, before, limit + 1)
    previews = '\n'.join(text for text in (trial_history(store, fn, limit), regional_history(store, fn, limit)) if text)
    if not rows:
        return previews or f"No recorded experiments for {fn['name']} in this page."
    out = [f"Experiment history for {fn['name']} (newest first):",
           "Historical scores only: context versions omit compiler, target, headers and unit state. "
           "Equal scores or source hashes do not prove equal assembly or a current match."]
    for index, row in enumerate(rows[:limit]):
        previous = rows[index + 1] if index + 1 < len(rows) else None
        source = row["source"] or ""
        digest = hashlib.sha256(source.encode()).hexdigest()[:16]
        outcome = f"score={row['score']}" if row["score"] is not None else "unscored"
        out.append(f"#{row['id']} agent={row['agent'] or 'unknown'} via={row['via'] or 'unknown'} "
                   f"context={row['context_version']} compiled={bool(row['compiled'])} {outcome} source={digest}")
        if row['output_fingerprint']:
            same = store.one("SELECT id FROM attempts WHERE function_id=? AND id<? AND output_fingerprint=? "
                             "ORDER BY id DESC LIMIT 1", fn['id'], row['id'], row['output_fingerprint'])
            out.append(f"  Compiled function output {row['output_fingerprint'][:16]}"
                       + (f" repeats #{same['id']} (code/relocations/local data; not a correctness verdict)." if same else "."))
        if previous is None:
            out.append("  First recorded source; no preceding experiment:\n" + source[:CHANGE_CHARS]
                       + ("\n  [source truncated]" if len(source) > CHANGE_CHARS else ""))
            continue
        if source == (previous["source"] or ""):
            out.append(f"  Source identical to #{previous['id']}; build equivalence unknown.")
        else:
            changes = difflib.unified_diff((previous["source"] or "").splitlines(), source.splitlines(),
                                           n=0, lineterm="")
            delta = "\n".join(line for line in changes if not line.startswith(("---", "+++")))
            out.append(f"  Source change from #{previous['id']}:\n" + delta[:CHANGE_CHARS]
                       + ("\n  [change truncated]" if len(delta) > CHANGE_CHARS else ""))
        if row["context_version"] != previous["context_version"]:
            out.append("  Context version changed; scores may not be comparable.")
        elif row["score"] is not None and row["score"] == previous["score"]:
            out.append("  Same recorded score as preceding experiment; assembly equivalence unknown.")
    if len(rows) > limit:
        out.append(f"Older experiments: experiment_history(function={fn['name']!r}, before={rows[limit - 1]['id']}, "
                   f"limit={limit}).")
    if previews:
        out += ["", previews]
    return "\n".join(out)


def record(net, fn, agent, revision: str, name: str, source: str, result: dict, elapsed: float) -> int:
    """Share even negative previews without touching attempts, claims or accepted source.

    A revision scopes observations, not complete-state cached verdicts. Keeping any winner always compiles again.
    """
    digest = hashlib.sha256(source.encode()).hexdigest()
    compiled = bool(result.get('compiled', result.get('score') is not None))
    outcome = ('rejected: ' + ', '.join(result.get('broken') or []) if result.get('broken') else
               'exact preview, not applied' if result.get('matched') else
               'compiled preview' if compiled else 'unscored preview')
    with net.store.tx() as db:
        return db.execute(
            "INSERT INTO experiments(project_id,function_id,agent_id,revision,name,source_hash,source,score,compiled,"
            "matched,output_fingerprint,outcome,elapsed,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fn['project_id'], fn['id'], agent.id, revision, name[:80], digest, source, result.get('score'), compiled,
             bool(result.get('matched')), result.get('output_fingerprint'), outcome, elapsed, time.time())).lastrowid


def trial_history(store, fn, limit=6):
    """Names, outcomes and duplicate generated outputs from previews persist across helpers and restarts."""
    rows = store.all("SELECT e.*,a.name AS agent FROM experiments e LEFT JOIN agents a ON a.id=e.agent_id "
                     "WHERE function_id=? ORDER BY e.id DESC LIMIT ?", fn['id'], max(1, min(int(limit), MAX_HISTORY)))
    if not rows:
        return ''
    out = ["Shared recipe previews (E IDs; advisory history, no verdict reuse):"]
    for row in rows:
        out.append(f"E{row['id']} {row['name']} by {row['agent'] or 'unknown'}: {row['outcome']}; "
                   f"score={row['score']}; {row['elapsed']:.3f}s; revision={row['revision']}")
        if row['output_fingerprint']:
            same = store.one("SELECT id,name FROM experiments WHERE function_id=? AND id<? AND output_fingerprint=? "
                             "ORDER BY id DESC LIMIT 1", fn['id'], row['id'], row['output_fingerprint'])
            out.append('  function output=' + row['output_fingerprint'][:16]
                       + (f"; same code/relocations/local data as E{same['id']} ({same['name']})." if same else '.'))
        out.append('  Source: ' + row['source'][:CHANGE_CHARS]
                   + (' [truncated]' if len(row['source']) > CHANGE_CHARS else ''))
    return '\n'.join(out)


def regional_history(store, fn, limit=6):
    """Short cross-session hypotheses complement score-only history, including failed or partial recovery."""
    rows = store.all('SELECT id,start_offset,end_offset,hypothesis,outcome,operation_id,findings FROM region_experiments '
                     'WHERE function_id=? ORDER BY id DESC LIMIT ?', fn['id'], max(1,min(int(limit),MAX_HISTORY)))
    if not rows:
        return ''
    out = ['Regional evidence (historical, advisory; region_history has details):']
    for row in rows:
        out.append(f"R{row['id']} +0x{row['start_offset']:x}..+0x{row['end_offset']:x} {row['outcome']}: "
                   + row['hypothesis'][:240] + ('; operation '+row['operation_id'] if row['operation_id'] else ''))
        if row['findings']:
            out.append('  '+row['findings'][:480])
    return '\n'.join(out)

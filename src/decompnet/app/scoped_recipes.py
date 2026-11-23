"""User/agent recipes retain explicit prerequisites, recorded matched donors and build scope.

Retrieval never applies edits, infers semantics, or turns an observed donor into a compiler rule. Owners test
suggestions against a fresh baseline using try_recipes, with all ordinary judge and neighbor protections.
"""

import hashlib
import json
import time

from ..network import NetworkError


def scope(fp, cfg):
    digest = hashlib.sha256()
    for name in ('build.ninja', 'objdiff.json', 'tools/compile.py', 'tools/compiler_units.json'):
        path = fp.root/name
        if path.exists():
            digest.update(name.encode()+b'\0'+path.read_bytes())
    digest.update(str(cfg.get('platform')).encode()+str(cfg.get('compiler')).encode())
    return digest.hexdigest()


def save(net, agent, slug, name, prerequisites, evidence, donors, edits):
    """Record a bounded recipe proposal and its evidence; only a later strict trial can validate a use."""
    if not all(isinstance(v, str) and 8 <= len(v) <= 4000 for v in (prerequisites, evidence)):
        raise NetworkError('Describe explicit prerequisites and evidence in8–4000 characters each.')
    if not isinstance(name, str) or not 1 <= len(name) <= 80:
        raise NetworkError('Use a recipe name of1–80 characters.')
    if (not isinstance(donors, list) or not 1 <= len(donors) <= 8
            or not all(isinstance(donor, str) for donor in donors) or len(set(donors)) != len(donors)):
        raise NetworkError('Cite1–8 distinct recorded matched donor function names.')
    if not isinstance(edits, list) or not 1 <= len(edits) <= 16 or len(json.dumps(edits)) > 64*1024:
        raise NetworkError('Supply1–16 bounded exact edits for the recipe.')
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {'old', 'new'} or not all(isinstance(v, str) for v in edit.values()) or not edit['old']:
            raise NetworkError('Each edit contains nonempty old text and replacement new text.')
    proj, fp, cfg = net.forks.get(slug)
    saved = []
    for name_ in donors:
        fn = net._function(name_, slug)
        if fn['status'] != 'matched':
            raise NetworkError('Every cited donor must currently have recorded matched status.')
        unit = fp.unit(fn['unit'])
        saved.append({'id': fn['id'], 'name': fn['name'], 'target': hashlib.sha256(unit.target.read_bytes()).hexdigest()
                      if unit.target and unit.target.exists() else ''})
    prerequisites = json.dumps({'text': prerequisites, 'build_scope': scope(fp, cfg)})
    with net.store.tx() as db:
        rid = db.execute('INSERT INTO scoped_recipes(project_id,agent_id,name,prerequisites,evidence,donor_ids,edits,created_at) '
                         'VALUES(?,?,?,?,?,?,?,?)', (proj['id'], agent.id, name, prerequisites, evidence,
                                                    json.dumps(saved), json.dumps(edits), time.time())).lastrowid
    return f'Saved recipe S{rid}; donor matches are recorded evidence, not reverified here. No edits applied.'


def candidates(net, slug, function, limit=5):
    proj, fp, cfg = net.forks.get(slug)
    net._function(function, slug)
    from . import compile_tools

    # under the unit's read lock: a preview writes each candidate into the source while it builds it
    with compile_tools._locked(net.forks, slug, function, read=True):
        _, source, _ = compile_tools.snapshot(net.forks, slug, function)
    stamp = scope(fp, cfg)
    rows = net.store.all('SELECT * FROM scoped_recipes WHERE project_id=? ORDER BY id DESC LIMIT 100', proj['id'])
    out = ['Scoped recipe proposals; prerequisites must be checked by the owner. No automatic application.']
    shown = 0
    for row in rows:
        prereq = json.loads(row['prerequisites'])
        if prereq['build_scope'] != stamp:
            continue
        valid = True
        for donor in json.loads(row['donor_ids']):
            current = net.store.one('SELECT * FROM functions WHERE id=? AND project_id=?', donor['id'], proj['id'])
            if current is None or current['status'] != 'matched':
                valid = False
                break
            unit = fp.unit(current['unit'])
            if not unit.target or not unit.target.exists() or hashlib.sha256(unit.target.read_bytes()).hexdigest() != donor['target']:
                valid = False
                break
        if not valid:
            continue
        trial = source
        for edit in json.loads(row['edits']):
            if trial.count(edit['old']) != 1:
                valid = False
                break
            trial = trial.replace(edit['old'], edit['new'], 1)
        if not valid:
            continue
        out += [f"S{row['id']} {row['name']}: {prereq['text']}", 'Evidence: '+row['evidence'],
                'Recorded matched donors (not fresh verdicts): '+', '.join(d['name'] for d in json.loads(row['donor_ids'])),
                'Test through try_recipes: '+json.dumps({'name':row['name'], 'edits':json.loads(row['edits'])})]
        shown += 1
        if shown >= max(1, min(int(limit), 8)):
            break
    if not shown:
        out.append('No proposal fits the current build scope and exact source strings in the latest100 recipes.')
    return '\n'.join(out)

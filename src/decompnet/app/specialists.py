"""Persistent advisory blockers for a single source owner and an existing idle helper.

Specialists inspect shared experiments and propose exact recipes; they never compile, claim, edit, or approve a
match. Assignment and claim checks are serialized by Store.tx; expensive revision reads happen outside it.
"""

import hashlib
import json
import time

from ..network import NetworkError

TTL = 600
MAX_TEXT = 16 * 1024
MAX_PROPOSALS = 64 * 1024
MAX_READ_TOOLS = 16
CATEGORIES = {'control_flow', 'layout', 'codegen', 'references'}
TERMINAL = {'completed', 'stale', 'cancelled', 'failed'}
READ_ONLY = {'region_history', 'compare_region', 'recipe_candidates', 'get_function', 'function_brief', 'get_context', 'experiment_history', 'last_diff', 'inspect_blocks',
             'find_code_blocks', 'find_similar', 'guess_struct', 'get_class', 'guide', 'project_info',
             'specialist_brief', 'specialist_status', 'specialist_report', 'report_specialist',
             'read_file', 'search_code', 'read_data', 'function_patterns', 'suggest_recipes',
             'search_notes', 'list_functions', 'pattern_index_report', 'file_status', 'find_callers'}


def _text(value, label, minimum=1):
    if not isinstance(value, str) or len(value.strip()) < minimum or len(value.encode()) > MAX_TEXT:
        raise NetworkError(f'{label} must contain {minimum} or more characters and at most 16 KiB.')
    return value.strip()


def revision(net, slug, fn):
    """Source/build evidence revision, never a reusable correctness verdict; missing definitions use full context."""
    from .compile_tools import snapshot

    proj = dict(net._project(slug))
    state = [proj, dict(fn)]
    if net.fork_slug(slug):
        try:
            return snapshot(net.forks, slug, fn['name'])[2]
        except NetworkError:
            # A request may ask how to write the first definition. Hash the whole unit and configuration instead.
            _, project, config = net.forks.get(slug)
            unit = project.unit(fn['unit'])
            state.append(config)
            if unit.source:
                state.append(project.safe_path(unit.source).read_bytes().hex())
            if unit.target and unit.target.is_file():
                state.append(unit.target.read_bytes().hex())
            for path in sorted(project.root.rglob('*')):
                if (path.suffix in {'.h', '.hpp', '.hh', '.hxx', '.inc'}
                        and not set(path.relative_to(project.root).parts) & {'.git', '.venv', 'build', 'orig', 'extract', 'node_modules'}):
                    state.append((str(path.relative_to(project.root)), path.read_bytes().hex()))
    else:
        ctx = net._context(proj['id'], fn['unit'])
        state.append(dict(ctx) if ctx else None)
    return 'context-advisory:' + hashlib.sha256(json.dumps(state, sort_keys=True, default=lambda v: v.hex()
                                                         if isinstance(v, bytes) else str(v)).encode()).hexdigest()


def _expire(db, now):
    db.execute("UPDATE specialist_requests SET status='cancelled',finished_at=? WHERE status IN ('queued','working') "
               "AND (expires_at<=? OR NOT EXISTS (SELECT 1 FROM claims c WHERE "
               "c.function_id=specialist_requests.function_id AND c.agent_id=specialist_requests.owner_id "
               "AND c.expires_at>?))", (now, now, now))


def _owner(db, fn_id, agent_id, now):
    return db.execute('SELECT 1 FROM claims WHERE function_id=? AND agent_id=? AND expires_at>?',
                      (fn_id, agent_id, now)).fetchone() is not None


def _row(db, project_id, request_id):
    row = db.execute('SELECT * FROM specialist_requests WHERE id=? AND project_id=?',
                     (int(request_id), project_id)).fetchone()
    if row is None:
        raise NetworkError('No specialist request with that ID in this project.')
    return dict(row)


def _view(row):
    scope = ('context evidence only; obtain function_brief revision before testing recipes'
             if row['revision'].startswith('context-advisory:') else 'function/build revision for owner try_recipes')
    return (f"Specialist request #{row['id']}: {row['status']} ({row['category']}); revision {row['revision']}.\n"
            f"Revision scope: {scope}.\nQuestion: {row['question']}\nEvidence: {row['evidence']}\n"
            + (f"Findings (advisory): {row['findings']}\nProposed recipes (not applied): {row['proposals']}\n"
               if row['findings'] else '')
            + 'Only the owner may test proposals through the ordinary strict judge. Stale results need rechecking.')


def request(net, agent, slug, function, category, question, evidence):
    """Queue one concrete blocker while the requester retains sole ownership of the function."""
    if category not in CATEGORIES:
        raise NetworkError('Choose control_flow, layout, codegen, or references for the specialist category.')
    question, evidence = _text(question, 'Question', 20), _text(evidence, 'Evidence', 20)
    proj = net._project(slug)
    fn = net._function(function, slug)
    stamp = revision(net, slug, fn)
    now = time.time()
    with net.store.tx() as db:
        _expire(db, now)
        if not _owner(db, fn['id'], agent.id, now):
            raise NetworkError('Hold an unexpired claim before requesting a specialist.')
        if db.execute("SELECT 1 FROM specialist_requests WHERE assigned_id=? AND status='working'", (agent.id,)).fetchone():
            raise NetworkError('Finish the assigned specialist task before requesting another helper.')
        duplicate = db.execute('SELECT * FROM specialist_requests WHERE project_id=? AND function_id=? '
                               'AND category=? AND question=? AND evidence=? AND revision=? ORDER BY id DESC LIMIT 1',
                               (proj['id'], fn['id'], category, question, evidence, stamp)).fetchone()
        if duplicate is not None and duplicate['status'] not in {'failed', 'cancelled'}:
            return 'Existing blocker at this revision; reuse its evidence.\n' + _view(dict(duplicate))
        live = db.execute("SELECT id FROM specialist_requests WHERE function_id=? AND status IN ('queued','working')",
                          (fn['id'],)).fetchone()
        if live:
            raise NetworkError(f"Request #{live['id']} already covers this function; finish or cancel it first.")
        cur = db.execute('INSERT INTO specialist_requests(project_id,function_id,owner_id,category,question,evidence,'
                         'revision,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,\'queued\',?,?)',
                         (proj['id'], fn['id'], agent.id, category, question, evidence, stamp, now, now+TTL))
        return f'Specialist request #{cur.lastrowid} queued for an existing idle helper; no agent was launched. Revision {stamp}.'


def take(net, agent, slug):
    """Assign at most one blocker to an existing helper that has no claim of its own."""
    proj = net._project(slug)
    with net.store.tx() as db:
        now = time.time()
        _expire(db, now)
        if db.execute('SELECT 1 FROM claims WHERE agent_id=? AND expires_at>?', (agent.id, now)).fetchone():
            return None
        if db.execute("SELECT 1 FROM specialist_requests WHERE assigned_id=? AND status='working'", (agent.id,)).fetchone():
            return None
        row = db.execute("SELECT * FROM specialist_requests WHERE project_id=? AND status='queued' AND owner_id!=? "
                         'ORDER BY created_at,id LIMIT 1', (proj['id'], agent.id)).fetchone()
        if row is None:
            return None
        db.execute("UPDATE specialist_requests SET status='working',assigned_id=? WHERE id=? AND status='queued'",
                   (agent.id, row['id']))
        result = _row(db, proj['id'], row['id'])
        if not hasattr(net, '_specialist_sessions'):
            net._specialist_sessions = {}
        net._specialist_sessions[(agent.id, slug)] = {'id': result['id'], 'reads': 0}
    return result


def _access(row, agent):
    if agent.id not in {row['owner_id'], row['assigned_id']}:
        raise NetworkError('Only this request’s owner or assigned specialist may inspect it.')


def _current(net, slug, row):
    fn = net.store.one('SELECT * FROM functions WHERE id=? AND project_id=?', row['function_id'], row['project_id'])
    return revision(net, slug, fn) if fn else None


def brief(net, agent, slug, request_id):
    """A bounded assignment with shared experiment evidence and pointers into target analysis."""
    proj = net._project(slug)
    with net.store.tx() as db:
        _expire(db, time.time())
        row = _row(db, proj['id'], request_id)
        _access(row, agent)
    fn = net.store.one('SELECT * FROM functions WHERE id=?', row['function_id'])
    text = _view(row)
    if _current(net, slug, row) != row['revision']:
        text += '\nSTALE revision: source/build context changed; report advisory findings with this limitation.'
    if fn:
        text += f"\nFunction {fn['name']}; {fn['n_insns']} target instructions. Use inspect_blocks offset/start/limit, "
        text += ('get_context, and experiment_history for focused evidence. Do not claim, compile, edit, or run recipes. '
                 'You have at most 16 read-tool calls, then report findings and unresolved questions.\n')
        text += net.experiment_history(fn['name'], slug, limit=6)[:12000]
    return text


def _proposals(proposals):
    if not isinstance(proposals, list) or len(proposals) > 4:
        raise NetworkError('Send at most four proposed recipes.')
    count = 0
    for recipe in proposals:
        if not isinstance(recipe, dict) or set(recipe) - {'name', 'edits'} or not {'name', 'edits'} <= set(recipe):
            raise NetworkError('Each proposed recipe has a name and exact old/new edits.')
        if 'name' in recipe:
            _text(recipe['name'], 'Recipe name')
        if not isinstance(recipe['edits'], list) or not recipe['edits']:
            raise NetworkError('Each recipe needs exact replacements.')
        selectors = set()
        for edit in recipe['edits']:
            count += 1
            if (not isinstance(edit, dict) or set(edit) != {'old', 'new'} or not isinstance(edit['old'], str)
                    or not edit['old'] or not isinstance(edit['new'], str)):
                raise NetworkError('Each proposed edit contains a nonempty old string and a new string.')
            if edit['old'] in selectors:
                raise NetworkError('Each recipe must use distinct exact old selectors; the owner checks unique source occurrences.')
            selectors.add(edit['old'])
    encoded = json.dumps(proposals)
    if count > 16 or len(encoded.encode()) > MAX_PROPOSALS:
        raise NetworkError('Proposals are limited to 16 total exact replacements and 64 KiB.')
    return encoded


def report(net, agent, slug, request_id, findings, proposals=None):
    """Publish advisory findings only; revalidate ownership and assignment atomically before storing them."""
    findings, recipes = _text(findings, 'Findings', 20), _proposals(proposals if proposals is not None else [])
    proj = net._project(slug)
    row = net.store.one('SELECT * FROM specialist_requests WHERE id=? AND project_id=?', request_id, proj['id'])
    if row is None:
        raise NetworkError('No specialist request with that ID in this project.')
    current = _current(net, slug, row)
    with net.store.tx() as db:
        now = time.time()
        _expire(db, now)
        row = _row(db, proj['id'], request_id)
        if row['assigned_id'] != agent.id or row['status'] != 'working':
            raise NetworkError('Only the assigned specialist may report on an active request.')
        if db.execute('SELECT 1 FROM claims WHERE agent_id=? AND expires_at>?', (agent.id, now)).fetchone():
            raise NetworkError('A specialist must hold no source claim while reporting.')
        state = 'completed' if current == row['revision'] else 'stale'
        db.execute('UPDATE specialist_requests SET status=?,findings=?,proposals=?,finished_at=? WHERE id=?',
                   (state, findings, recipes, now, request_id))
        return _view(_row(db, proj['id'], request_id))


def status(net, agent, slug, function, request_id=0, cancel=False):
    """Read retained results and revision scope; owners can cancel outstanding work."""
    proj = net._project(slug)
    fn = net._function(function, slug)
    if not request_id:
        row = net.store.one('SELECT id FROM specialist_requests WHERE project_id=? AND function_id=? ORDER BY id DESC LIMIT 1',
                            proj['id'], fn['id'])
        if not row:
            return 'No specialist request for this function.'
        request_id = row['id']
    current = revision(net, slug, fn)
    with net.store.tx() as db:
        now = time.time()
        _expire(db, now)
        row = _row(db, proj['id'], request_id)
        if row['function_id'] != fn['id']:
            raise NetworkError('This request belongs to a different function.')
        _access(row, agent)
        if cancel:
            if row['owner_id'] != agent.id:
                raise NetworkError('Only the source owner may cancel a request.')
            if row['status'] not in TERMINAL:
                db.execute("UPDATE specialist_requests SET status='cancelled',finished_at=? WHERE id=?", (now, request_id))
        elif row['status'] == 'completed' and row['revision'] != current:
            db.execute("UPDATE specialist_requests SET status='stale' WHERE id=?", (request_id,))
        return _view(_row(db, proj['id'], request_id))


def guard(net, agent, slug, tool_name):
    """Keep advisory sessions read-only even if ownership expires while the helper is still answering."""
    session = getattr(net, '_specialist_sessions', {}).get((agent.id, slug))
    assigned = net.store.one("SELECT id FROM specialist_requests WHERE assigned_id=? AND status='working'", agent.id)
    if not (session or assigned):
        return
    if tool_name not in READ_ONLY:
        raise NetworkError('This helper is an advisory specialist: only inspect evidence and report proposed recipes; '
                           'the source owner performs all claims, edits, compiles, and tests.')
    if tool_name in {'report_specialist', 'specialist_report', 'specialist_status'}:
        return
    # A focused blocker gets a small inspection budget, rather than another unrestricted decompilation session.
    with net.store.tx():
        if not hasattr(net, '_specialist_sessions'):
            net._specialist_sessions = {}
        session = net._specialist_sessions.setdefault((agent.id, slug), {'id': assigned['id'] if assigned else 0, 'reads': 0})
        if session['reads'] >= MAX_READ_TOOLS:
            raise NetworkError('Specialist inspection budget reached (16 read tools). Report findings and unresolved '
                               'questions with report_specialist; the owner can choose the next experiment.')
        session['reads'] += 1


def completed(net, agent, slug, request_id):
    proj = net._project(slug)
    with net.store.tx() as db:
        _expire(db, time.time())
        row = _row(db, proj['id'], request_id)
        return row['assigned_id'] == agent.id and row['status'] in TERMINAL


def finish(net, agent, slug, request_id, reason=''):
    """End the helper session; preserve reports while marking an abandoned live assignment failed."""
    proj = net._project(slug)
    with net.store.tx() as db:
        _expire(db, time.time())
        row = _row(db, proj['id'], request_id)
        if row['assigned_id'] != agent.id:
            raise NetworkError('Only the assigned specialist may finish this session.')
        if row['status'] not in TERMINAL:
            db.execute("UPDATE specialist_requests SET status='failed',findings=?,finished_at=? WHERE id=?",
                       (str(reason)[:MAX_TEXT] or 'Helper ended without a report.', time.time(), request_id))
    getattr(net, '_specialist_sessions', {}).pop((agent.id, slug), None)

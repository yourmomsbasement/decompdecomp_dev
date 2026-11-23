"""Read-only block findings for both project and ROM briefs, and the find_code_blocks agent tool.

The existing mnemonic index narrows candidates; operand-aware comparison reads only immutable targets. No builds,
claims, attempts, source edits or score updates are performed. Results never authorize automatic C substitution.
"""

import json
import logging
import threading
from collections import OrderedDict
from functools import lru_cache

import capstone

from .. import arch, codeblocks, scoring
from ..forks import objdiff
from ..network import NetworkError
from .target_stamps import target_hashes

LARGE_FUNCTION = 200
MAX_CANDIDATES = 24
AUTO_CANDIDATES = 8
MAX_WARM_UNITS = 24


@lru_cache(maxsize=8)
def finder(net):
    return Finder(net)


class Finder:
    def __init__(self, net):
        self.net = net
        self.units = OrderedDict()
        self.lock = threading.RLock()
        self.cached_insns = 0

    def unit_code(self, fp, unit):
        if unit.target is None or not unit.target.is_file():
            return {}
        digest = target_hashes(self.net, [unit]).get(unit.name)
        if digest is None:
            return {}
        stat = unit.target.stat()
        key = (str(unit.target.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, digest,
               json.dumps(fp.diff_options(unit), sort_keys=True))
        with self.lock:
            if key in self.units:
                self.units.move_to_end(key)
                return self.units[key]
            result = objdiff.diff(unit.target, None, None, cwd=fp.root, options=fp.diff_options(unit), blocks=True)
            if target_hashes(self.net, [unit]).get(unit.name) != digest:
                return {}  # an external rebuild must not cache instructions under an older target's digest
            code = {name: [codeblocks.Instruction(*row) for row in rows] for name, rows in result.block_code.items()}
            size = sum(len(lines) for lines in code.values())
            if size <= 50000:
                self.units[key] = code
                self.cached_insns += size
                while len(self.units) > 32 or self.cached_insns > 50000:
                    _, removed = self.units.popitem(last=False)
                    self.cached_insns -= sum(len(lines) for lines in removed.values())
            return code

    def find(self, project, function, limit=5, automatic=False):
        net = self.net
        proj = net._project(project)
        fn = net._function(function, proj['slug'])
        processor = proj['arch']
        cap = AUTO_CANDIDATES if automatic else MAX_CANDIDATES
        if fork := net.fork_slug(proj['slug']):
            engine = net.forks
            _, fp, _ = engine.get(fork)
            symbol = engine._meta(fn).get('symbol') or fn['name']
            unit = fp.unit(fn['unit'])
            target = self.unit_code(fp, unit).get(symbol, [])
            rows = net.store.all('SELECT id,name,unit,meta,status,n_insns FROM functions WHERE project_id=? '
                                 'AND id!=? AND n_insns>=? ORDER BY status="matched" DESC,id',
                                 proj['id'], fn['id'], codeblocks.MIN_INSNS)
            # Warm a bounded set of absent mnemonic entries, matched donors first. Normal background indexing
            # eventually covers the whole game; a first explicit request can still find something immediately.
            units = {u.name: u for u in fp.units()}
            with engine._ilock:
                entries = dict(engine._index_of(proj, fp))
            stamps = target_hashes(net, units.values())
            warmed = set()
            for row in rows:
                name = row['unit']
                if (name in warmed or name not in stamps
                        or (name in entries and entries[name].get('sha256') == stamps[name])):
                    continue
                if len(warmed) >= (4 if automatic else MAX_WARM_UNITS):
                    break
                warmed.add(name)
                try:
                    entry = engine._unit_index(proj, fp, units[name], save=False)
                except (NetworkError, OSError, ValueError):
                    continue
                if entry:
                    entries[name] = entry
            candidates = []
            for row in rows:
                sym = engine._meta(row).get('symbol') or row['name']
                # Index entries from replaced/removed target objects must not contribute evidence.
                u = units.get(row['unit'])
                entry = entries.get(row['unit'], {})
                if not u or row['unit'] not in stamps or entry.get('sha256') != stamps[row['unit']]:
                    continue
                ops = entry.get('ops', {}).get(sym, [])
                candidates.append((row, ops, (u, sym)))
            covered = len({r['unit'] for r, _, _ in candidates})
            coverage = f'{covered}/{len(units)} units have usable donor index entries'

            def load(item):
                u, sym = item
                return self.unit_code(fp, u).get(sym, [])
        else:
            target = self.rom_code(fn, proj)
            ops_by_address = net._rom_ops(proj)
            rows = net.store.all('SELECT id,name,unit,status,n_insns,vram,size FROM functions WHERE project_id=? '
                                 'AND id!=? AND target IS NOT NULL AND n_insns>=?',
                                 proj['id'], fn['id'], codeblocks.MIN_INSNS)
            candidates = [(r, ops_by_address.get((r['vram'], r['size']), []), r['id']) for r in rows]
            coverage = f'{len(candidates)} donor functions indexed'

            def load(fid):
                row = net.store.one('SELECT * FROM functions WHERE id=?', fid)
                return self.rom_code(row, proj)
        if not target:
            return 'No readable target instructions for this function.', []
        seeds = codeblocks.grams(tuple(ins.op for ins in target))
        ranked = []
        for row, ops, item in candidates:
            overlap = len(seeds & codeblocks.grams(tuple(op.lower() for op in ops)))
            if overlap:
                ranked.append((row, overlap, item))
        ranked.sort(key=lambda x: (x[0]['status'] != 'matched', -x[1], x[0]['id']))
        donors, by_name = {}, {}
        skipped = 0
        for row, _, item in ranked[:cap]:
            try:
                key = str(row['id'])
                code = load(item)
                if not code:
                    skipped += 1
                    continue
                donors[key] = code
                by_name[key] = row
            except (NetworkError, OSError, ValueError, KeyError):
                skipped += 1
        hits, exhausted = codeblocks.search(target, donors, processor, max(1, min(int(limit), 10)))
        facts = [(h, by_name[h.donor], target, donors[h.donor]) for h in hits]
        note = (f'{coverage}; checked {len(donors)}/{len(ranked)} shortlisted donors'
                + (f'; {skipped} unreadable' if skipped else '')
                + ('; comparison budget reached' if exhausted else '')
                + (f'; first {codeblocks.MAX_INSNS} instructions per function' if any(len(c) > codeblocks.MAX_INSNS for c in [target, *donors.values()]) else ''))
        return note, facts

    def rom_code(self, fn, proj):
        obj = self.net._target_obj(fn, proj)
        modes = arch.get(obj.arch).capstone
        if obj.big_endian not in modes:
            return []
        md = capstone.Cs(*modes[obj.big_endian])
        md.detail, md.skipdata = True, True
        address = fn['vram'] or 0
        destinations = {}
        for ins in md.disasm(obj.code, address):
            if ins.id and ins.group(capstone.CS_GRP_JUMP):
                immediates = [op.imm for op in ins.operands if op.type == capstone.CS_OP_IMM]
                if immediates:
                    destinations[ins.address-address] = immediates[-1]-address
        return [codeblocks.Instruction(ins.offset, ins.text, destinations.get(ins.offset))
                for ins in scoring.disassemble(obj)]


def render(note, facts, compact=False):
    if not facts:
        return f'No strong duplicate-code fragments found ({note}).'
    lines = ['Similar code blocks — advisory, not proof of equivalent C or a byte match.', note]
    for hit, row, target, donor in facts[:3 if compact else 10]:
        a, b = target[hit.start], target[hit.start+hit.count-1]
        c, d = donor[hit.donor_start], donor[hit.donor_start+hit.count-1]
        state = 'recorded matched donor' if row['status'] == 'matched' else 'unmatched donor'
        kind = 'same instruction text' if hit.exact_operands else 'same structure after register/number normalization'
        lines.append(f"- target +0x{a.offset:x}..+0x{b.offset:x} ↔ {row['name']} ({row['unit']}) "
                     f"+0x{c.offset:x}..+0x{d.offset:x}: {hit.count} instructions, {kind}; {state}.")
    lines.append('Offsets identify first/last instruction starts. Constants, stack offsets and symbol identities are retained. '
                 'Inspect donor C with get_function; a block is not automatically a standalone C helper.')
    return '\n'.join(lines)


def find_code_blocks(net, project, function, limit=5):
    note, facts = finder(net).find(project, function, limit)
    text = render(note, facts)
    best = next((row for _, row, _, _ in facts if row['status'] == 'matched'), None)
    if best is not None:
        saved = net.store.one('SELECT matched_source FROM functions WHERE id=?', best['id'])
        source = saved['matched_source'] if saved else None
        if source and len(source) <= 1800:
            text += f"\nRecorded matched definition of {best['name']} (whole function, not a mapped block):\n```c\n{source.strip()}\n```"
    return text


def hints(net, fn):
    if fn['n_insns'] < LARGE_FUNCTION or fn['status'] == 'matched':
        return ''
    try:
        proj = net.store.one('SELECT slug FROM projects WHERE id=?', fn['project_id'])
        note, facts = finder(net).find(proj['slug'], fn['name'], 3, automatic=True)
        return '\n\n### Similar code blocks\n' + render(note, facts, compact=True) if facts else ''
    except Exception:
        logging.getLogger('decompdecomp').debug('code-block hints unavailable for %s', fn['name'], exc_info=True)
        return ''  # advisory analysis must never prevent claiming or judging a function


def warm(net, proj, state):
    """Autopilot prepares advisory findings for a bounded group of large open functions, even without drafts."""
    from .function_patterns import hints as pattern_hints

    state['pattern_hints'] = 0
    rows = net.store.all('SELECT id,name,project_id,n_insns,status FROM functions WHERE project_id=? '
                         'AND status="open" AND n_insns>=? ORDER BY n_insns DESC,id LIMIT 16',
                         proj['id'], LARGE_FUNCTION)
    state['block_total'], state['block_scanned'], state['block_hints'] = len(rows), 0, 0
    for fn in rows:
        if state['stop'].is_set():
            break
        if hints(net, fn):
            state['block_hints'] += 1
        if pattern_hints(net, fn):
            state['pattern_hints'] += 1
        state['block_scanned'] += 1

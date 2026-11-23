"""Read-only project pattern library, shared by ROM/project tools and function briefs.

Uses the existing target mnemonic index; it never compiles a donor or changes saved work. Source observations are
read fresh for selected donors, so a changed/revoked match cannot leave stale advice in a cached library.
"""

import hashlib
import logging
import threading
from dataclasses import replace
from functools import lru_cache

from .. import patterns
from ..arch.patterns import families
from .code_blocks import finder
from .target_stamps import target_hashes

MAX_ENTRIES = 5000
MAX_TOTAL_OPS = 250000


@lru_cache(maxsize=4)
def library(net):
    return Library(net)


class Library:
    def __init__(self, net):
        self.net = net
        self.lock = threading.Lock()
        self.cached = None

    def corpus(self, proj):
        """Snapshot available target mnemonics; missing/stale units are excluded with visible coverage."""
        net = self.net
        rows = net.store.all('SELECT id,name,unit,meta,vram,size FROM functions WHERE project_id=? '
                             'AND status="matched" ORDER BY id LIMIT ?', proj['id'], MAX_ENTRIES+1)
        total = net.store.one('SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status="matched"', proj['id'])['n']
        if fork := net.fork_slug(proj['slug']):
            engine = net.forks
            _, fp, _ = engine.get(fork)
            with engine._ilock:
                indexed = dict(engine._index_of(proj, fp))
            units = {u.name: u for u in fp.units()}
            stamps = target_hashes(net, units.values())

            def ops(row):
                entry = indexed.get(row['unit'], {})
                if row['unit'] not in stamps or entry.get('sha256') != stamps[row['unit']]:
                    return ()
                symbol = engine._meta(row).get('symbol') or row['name']
                return entry.get('ops', {}).get(symbol, ())
        else:
            indexed = net._rom_ops(proj)

            def ops(row):
                return indexed.get((row['vram'], row['size']), ())
        entries, count = [], 0
        for row in rows[:MAX_ENTRIES]:
            sequence = tuple(op.lower() for op in ops(row)[:patterns.MAX_OPS])
            if len(sequence) < 4:
                continue
            if count+len(sequence) > MAX_TOTAL_OPS:
                continue
            entries.append(patterns.Entry(row['id'], row['name'], row['unit'] or '', sequence))
            count += len(sequence)
        key = (proj['id'], hashlib.sha256(repr(entries).encode()).digest())
        with self.lock:
            if not self.cached or self.cached[0] != key:
                self.cached = key, patterns.Index(entries)
            index = self.cached[1]
        return index, f'{len(entries)}/{total} recorded matched functions indexed ({count} instructions; missing/stale targets and limits excluded)'

    def source(self, entry):
        saved = self.net.store.one('SELECT substr(matched_source,1,?) AS source FROM functions '
                                   'WHERE id=? AND status="matched"', patterns.MAX_SOURCE+1, entry.id)
        if saved is None:
            return None
        source = saved['source'] or ''
        return replace(entry, source=source if len(source) <= patterns.MAX_SOURCE else '')

    def find(self, project, function, limit=5):
        net = self.net
        proj = net._project(project)
        fn = net._function(function, proj['slug'])
        # Read only the query target. Corpus indexing is reused, never triggers a full disassembly sweep.
        blocks = finder(net)
        if fork := net.fork_slug(proj['slug']):
            _, fp, _ = net.forks.get(fork)
            symbol = net.forks._meta(fn).get('symbol') or fn['name']
            code = blocks.unit_code(fp, fp.unit(fn['unit'])).get(symbol, [])
        else:
            code = blocks.rom_code(fn, proj)
        index, coverage = self.corpus(proj)
        hits = index.rank(tuple(ins.op for ins in code), exclude=fn['id'], limit=max(1, min(int(limit), 8)))
        # Revoked matches are dropped; source edits are read fresh rather than cached with the opcode index.
        hits = [(score, saved) for score, entry in hits if (saved := self.source(entry)) is not None]
        return dict(fn, pattern_arch=proj['arch']), coverage, hits


def render(fn, coverage, hits, compact=False):
    """Keep the default brief small; explicit queries expose exceptions and more evidence."""
    if not hits:
        return 'No sufficiently similar matched-function patterns. '+coverage+'.'
    lines = ['Matched-function patterns (advisory; retrieval scores are not match probabilities).', coverage]
    for score, entry in hits:
        scope = 'same unit' if entry.unit == (fn['unit'] or '') else 'different unit; compiler compatibility unverified'
        family = ', '.join(families(fn.get('pattern_arch', ''), entry.ops)) or 'instruction-sequence neighbor'
        lines.append(f'- {entry.name} [{entry.unit or "ROM"}]: similarity {score:.2f}, {len(entry.ops)} instructions; {scope}; {family}.')
    # Do not mix units into a supposed global convention. Cross-unit examples remain separate evidence groups.
    units = dict.fromkeys(entry.unit for _, entry in hits)
    for unit in units:
        group = [entry for _, entry in hits if entry.unit == unit]
        for trait, count, total, exceptions in patterns.evidence(group)[:2 if compact else 6]:
            strength = 'repeated observation' if count >= 2 else 'single example'
            line = f'  {unit or "ROM sample"}: {trait}: {count}/{total} readable donor definitions; {strength}.'
            if exceptions and not compact:
                line += ' Absent in: '+', '.join(exceptions)+'.'
            lines.append(line)
    lines.append('Donor matches are recorded, not reverified here. Inspect donor C with get_function and shared fragments with find_code_blocks. No inferred rule changes compiler settings or proves a struct layout; validate all reuse with the strict judge.')
    return '\n'.join(lines)


def function_patterns(net, project, function, limit=5):
    fn, coverage, hits = library(net).find(project, function, limit)
    return render(fn, coverage, hits)


def pattern_index_report(net, project, sample=10):
    """Evaluate bounded held-out retrieval locally; no source or target data leaves the project."""
    lib = library(net)
    index, coverage = lib.corpus(net._project(project))
    # Source budget bounds both memory and evaluation work; report the actual tested sample.
    entries, size = [], 0
    for entry in index.entries:
        saved = lib.source(entry)
        if saved is None:
            continue
        if size+len(saved.source) <= 2_000_000:
            size += len(saved.source)
            entries.append(saved)
        else:
            entries.append(entry)
    result = patterns.evaluate(entries, max(1, min(int(sample), 40)))
    return (f'Matched pattern index: {coverage}.\nHeld-out source-trait retrieval proxy: {result["tested"]} queries; '
            f'{result["retrieved"]} retrieved a non-clone donor; {result["trait_agreement"]} had a top-three donor '
            f'sharing at least one observed source trait (random top-three baseline: {result["random_trait_agreement"]}). Each query is excluded from training; identical opcode/source '
            'clones are excluded from results. Samples are deterministic, source reads are capped at 2 MB of retained text. '
            'This is not semantic-family accuracy or evidence of faster decompilation; generic traits can agree by chance.')


def hints(net, fn):
    if fn['status'] == 'matched' or (fn['n_insns'] or 0) < 8:
        return ''
    try:
        proj = net.store.one('SELECT slug FROM projects WHERE id=?', fn['project_id'])
        row, coverage, hits = library(net).find(proj['slug'], fn['name'], 2)
        hits = [(score, entry) for score, entry in hits if score >= .65]
        return '\n\n### Matched-function patterns\n'+render(row, coverage, hits, compact=True) if hits else ''
    except Exception:  # advisory hints must never prevent claims or judging
        logging.getLogger('decompdecomp').debug('pattern hints unavailable for %s', fn['name'], exc_info=True)
        return ''

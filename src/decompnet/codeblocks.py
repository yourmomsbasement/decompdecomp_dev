"""Bounded, operand-aware similarity inside functions. Findings are evidence, never matching verdicts.

Only straight-line fragments within basic-block boundaries are compared. We preserve instruction order, constants,
stack offsets and symbol names, and consistently rename general/vector registers. Interleaved or reordered inlines
are intentionally missed. Callers retain target offsets and donor provenance for an agent to inspect.
"""

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache

from .arch.blocks import control, implicit_registers, register

MIN_INSNS = 8
MAX_FRAGMENT = 96
MAX_INSNS = 12000
MAX_COMPARISONS = 12000
_TOKEN = re.compile(r'\{ref:[^}]*\}|<[^>]*>|[%$]?[A-Za-z_][\w.$@]*|0x[0-9a-fA-F]+|\d+|[^\s]')
NOISE = {'nop', 'nopl', 'nopw', 'push', 'pop', 'mov', 'movq', 'movl', 'lw', 'sw', 'ld', 'sd', 'mr', 'ldr', 'str'}


@dataclass(frozen=True)
class Instruction:
    offset: int
    text: str
    target: int | None = None
    relocation: str = ""

    @property
    def comparison(self):
        return self.text + (" {ref:" + self.relocation + "}" if self.relocation else "")

    @property
    def op(self):
        return self.text.split(None, 1)[0].lower() if self.text.strip() else ''


@dataclass(frozen=True)
class Hit:
    donor: str
    start: int
    donor_start: int
    count: int
    exact_operands: bool


def regions(code: list[Instruction], processor: str):
    """Half-open instruction ranges with no control transfer or internal incoming branch target.

    Exclude MIPS delay slots as well as the branch: a slot may be conditional (branch likely).
    """
    code = code[:MAX_INSNS]
    targets = {ins.target for ins in code if ins.target is not None}
    start, skip = 0, False
    for i, ins in enumerate(code):
        if ins.offset in targets and i > start:
            yield start, i
            start = i
        if skip:
            start, skip = i + 1, False
            continue
        if control(processor, ins.op) or (processor in {'arm', 'thumb'} and re.search(r'\bpc\b', ins.text)):
            if i > start:
                yield start, i
            start, skip = i + 1, processor == 'mips'
    if start < len(code):
        yield start, len(code)


def canonical(lines: tuple[str, ...], processor: str) -> tuple:
    names = {}
    out = []
    fixed = implicit_registers(processor, lines)
    for line in lines:
        parts = line.strip().split(None, 1)
        if not parts:
            out.append(())
            continue
        row = [parts[0].lower()]
        for token in _TOKEN.findall(parts[1] if len(parts) > 1 else ''):
            if not fixed and (reg := register(processor, token)):
                physical, width = reg
                row.append(('reg', names.setdefault(physical, len(names)), width))
            elif re.fullmatch(r'0x[0-9a-fA-F]+|\d+', token):
                row.append(('number', int(token, 16 if token.startswith('0x') else 10)))
            else:
                row.append(token)
        out.append(tuple(row))
    return tuple(out)


def grams(ops: tuple[str, ...]) -> frozenset:
    ops = ops[:MAX_INSNS]
    return _small_grams(ops) if len(ops) <= 512 else _grams(ops)


def _grams(ops):
    return frozenset(ops[i:i+MIN_INSNS] for i in range(max(0, len(ops)-MIN_INSNS+1)))


@lru_cache(maxsize=1024)
def _small_grams(ops):
    return _grams(ops)


def informative(lines: tuple[str, ...]) -> bool:
    ops = [line.split(None, 1)[0].lower() for line in lines if line.strip()]
    return len(set(ops) - NOISE) >= 3 and sum(op not in NOISE for op in ops) >= 4


def search(target: list[Instruction], donors: dict[str, list[Instruction]], processor: str,
           limit: int = 5) -> tuple[list[Hit], bool]:
    """Return non-overlapping strongest fragments and whether the comparison budget was exhausted."""
    seeds = defaultdict(list)
    for lo, hi in regions(target, processor):
        for i in range(lo, hi-MIN_INSNS+1):
            lines = tuple(ins.comparison for ins in target[i:i+MIN_INSNS])
            if informative(lines):
                seeds[canonical(lines, processor)].append((i, hi))
    hits, compared, exhausted = [], 0, False
    for name, code in donors.items():
        for lo, hi in regions(code, processor):
            for j in range(lo, hi-MIN_INSNS+1):
                compared += 1
                if compared > MAX_COMPARISONS:
                    exhausted = True
                    break
                lines = tuple(ins.comparison for ins in code[j:j+MIN_INSNS])
                matches = seeds.get(canonical(lines, processor), ())
                for i, end in matches[:16]:  # ubiquitous sequences cannot monopolize the search
                    if any(h.donor == name and h.start <= i < h.start+h.count
                           and h.donor_start <= j < h.donor_start+h.count for h in hits):
                        continue
                    count = MIN_INSNS
                    while count < min(MAX_FRAGMENT, end-i, hi-j):
                        a = tuple(ins.comparison for ins in target[i:i+count+1])
                        b = tuple(ins.comparison for ins in code[j:j+count+1])
                        if canonical(a, processor) != canonical(b, processor):
                            break
                        count += 1
                    same = [x.text for x in target[i:i+count]] == [x.text for x in code[j:j+count]]
                    hits.append(Hit(name, i, j, count, same))
                if exhausted:
                    break
            if exhausted:
                break
        if exhausted:
            break
    hits.sort(key=lambda h: (-h.count, not h.exact_operands, h.donor, h.start))
    kept = []
    for hit in hits:
        if any(max(hit.start, other.start) < min(hit.start+hit.count, other.start+other.count) for other in kept):
            continue
        kept.append(hit)
        if len(kept) >= max(1, min(limit, 10)):
            break
    return kept, exhausted

"""Bounded target CFG and local mismatch inspection; reads immutable targets and the last compile only.

Offsets are target instruction offsets, never source line numbers. These findings cannot approve a match.
Unsupported processors abstain because text heuristics cannot safely model delay slots or conditional execution.
"""

from dataclasses import dataclass, field, replace

from elftools.common.exceptions import ELFError

from ..arch.blocks import control
from ..arch.x86_flow import effects
from ..elfobj import extract_function
from ..network import NetworkError
from .code_blocks import finder

MAX_INSNS = 12000
MAX_BLOCKS = 40
MAX_ROWS = 160
MAX_OBJECT_BYTES = 32 * 1024 * 1024


@dataclass
class Block:
    start: int
    code: list
    successors: list = field(default_factory=list)
    predecessors: list = field(default_factory=list)
    unknown: bool = False


def analyze(code, processor, obj=None):
    """Build a bounded x86 CFG, retaining external destinations and unresolved indirect jumps explicitly."""
    if processor not in {'x86', 'x86_64'}:
        return [], {}, 'CFG/data flow unavailable for this processor (delay slots/conditional execution not modeled).'
    truncated = len(code) > MAX_INSNS
    code = code[:MAX_INSNS]
    if not code:
        return [], {}, 'No readable target instructions.'
    offsets = {ins.offset for ins in code}
    leaders = {code[0].offset} | {ins.target for ins in code if ins.target in offsets}
    for index, ins in enumerate(code[:-1]):
        if control(processor, ins.op):
            leaders.add(code[index+1].offset)
    blocks = []
    for ins in code:
        if ins.offset in leaders:
            blocks.append(Block(ins.offset, []))
        blocks[-1].code.append(ins)
    by_start = {block.start: block for block in blocks}
    for index, block in enumerate(blocks):
        last = block.code[-1]
        jump = last.op.startswith(('j', 'loop'))
        stop = last.op.startswith(('ret', 'iret')) or last.op in {'ud2', 'hlt', 'int3'}
        if jump:
            if last.target is None or last.relocation:
                block.unknown = True
            else:
                block.successors.append(last.target)
        if not stop and last.op not in {'jmp', 'ljmp'} and index+1 < len(blocks):
            block.successors.append(blocks[index+1].start)
        for successor in block.successors:
            if successor in by_start:
                by_start[successor].predecessors.append(block.start)
    note = 'Analysis capped at 12000 target instructions; final fallthrough may be incomplete.' if truncated else ''
    facts = effects(obj, MAX_INSNS) if obj and obj.arch in {'x86', 'x86_64'} else {}
    if not facts:
        note += ' Register/stack data flow unavailable: no raw x86 target bytes readable in this object format.'
    return blocks, facts, note.strip()


def localized_rows(code, rows):
    """Map diff rows by consumed target instruction, retaining candidate-only insertions at the adjacent target.

    Diff rows contain no addresses. Refuse mapping if target text differs, rather than silently localizing a stale
    compile against a changed target. This also refuses objdiff alignment rows that are not instructions.
    """
    consumed = [left.strip() for _, left, _ in rows if left.strip()]
    expected = [ins.text.strip() for ins in code]
    if consumed != expected:
        return {}, 'Last diff target rows differ from this target; localized mismatches unavailable.'
    out, cursor = {}, 0
    for row in rows:
        offset = code[min(cursor, len(code)-1)].offset if code else 0
        out.setdefault(offset, []).append(row)
        if row[1].strip():
            cursor += 1
    return out, ''


def _target(net, proj, fn):
    reader = finder(net)
    if fork := net.fork_slug(proj['slug']):
        engine = net.forks
        _, project, _ = engine.get(fork)
        unit = project.unit(fn['unit'])
        symbol = engine._meta(fn).get('symbol') or fn['name']
        code = reader.unit_code(project, unit).get(symbol, [])
        obj = None
        # Reuse the ELF reader; COFF still has CFG selection through the cached objdiff target reader.
        if unit.target and unit.target.is_file() and unit.target.stat().st_size <= MAX_OBJECT_BYTES:
            try:
                obj = extract_function(unit.target.read_bytes(), symbol)
            except (ELFError, ValueError, KeyError):
                pass
        return code, obj
    obj = net._target_obj(fn, proj)
    code = reader.rom_code(fn, proj)
    # An unlinked relocation's encoded displacement is a placeholder, never an internal CFG edge.
    for index, ins in enumerate(code):
        end = code[index+1].offset if index+1 < len(code) else len(obj.code)
        refs = [rel.symbol for rel in obj.relocs if ins.offset <= rel.offset < end]
        if refs:
            code[index] = replace(ins, relocation=', '.join(refs))
    return code, obj


def inspect_blocks(net, agent, slug, function, offset=None, start=0, limit=12, instruction_limit=80):
    """List target blocks or select one by offset, with bounded data-flow and last-diff evidence; never builds."""
    proj = net._project(slug)
    fn = net._function(function, proj['slug'])
    code, obj = _target(net, proj, fn)
    # Imported source trees may not have a project architecture; trust the decoded immutable object when present.
    processor = obj.arch if obj and obj.arch else proj['arch']
    blocks, facts, note = analyze(code, processor, obj)
    if not blocks:
        return note
    start, limit = max(0, int(start)), max(1, min(MAX_BLOCKS, int(limit)))
    instruction_limit = max(1, min(MAX_ROWS, int(instruction_limit)))
    lines = [f"Target blocks for {fn['name']} ({len(blocks)} blocks, {len(code)} decoded target instructions); advisory analysis, no new compile.", note]
    if offset is None:
        selected = blocks[start:start+limit]
    else:
        offset = int(offset, 0) if isinstance(offset, str) else int(offset)
        selected = [block for block in blocks if block.start <= offset <= block.code[-1].offset]
        if not selected:
            raise NetworkError('Choose a target instruction offset shown by inspect_blocks.')
    kept = net._last_diffs.get((agent.id, fn['id']))
    local, mismatch_note = localized_rows(code, kept[0]) if kept else ({}, 'No last compile diff for this agent.')
    lines.append(mismatch_note)
    for block in selected:
        fmt = lambda offsets: ', '.join(f'+0x{value:x}' for value in offsets) or 'none'
        lines.append(f'Block +0x{block.start:x}..+0x{block.code[-1].offset:x} '
                     f'({len(block.code)} instructions); predecessors {fmt(block.predecessors)}; '
                     f'successors {fmt(block.successors)}' + ('; unknown indirect/relocated edge' if block.unknown else ''))
        mismatches = [(ins.offset, row) for ins in block.code for row in local.get(ins.offset, []) if row[0] != ' ']
        lines.append(f'  Last diff: {len(mismatches)} differing rows.' if kept and not mismatch_note else '')
        if offset is None:
            continue
        incoming, defined = set(), set()
        for ins in block.code:
            fact = facts.get(ins.offset)
            if fact:
                incoming |= fact['use'] - defined
                defined |= fact['full_def']
                if fact['barrier']:
                    # A callee's unspecified reads/writes defeat stronger block-wide dependencies.
                    defined.clear()
        if facts:
            lines.append('  Registers used before a local definition: ' + ', '.join(sorted(incoming)))
        for ins in block.code[:instruction_limit]:
            lines.append(f'  +0x{ins.offset:x}: {ins.text}')
            fact = facts.get(ins.offset)
            if fact:
                lines.append('    use: ' + ', '.join(sorted(fact['use'])) + '; writes: ' + ', '.join(sorted(fact['def']))
                             + ('; flags use: ' + ','.join(sorted(fact['flag_use']))
                                + '; flags def: ' + ','.join(sorted(fact['flag_def']))
                                + '; flags undefined: ' + ','.join(sorted(fact['flag_undefined'])) if fact['flags'] else '')
                             + ('; partial/conditional/uncertain write: prior register value retained as dependency' if fact['partial'] else '')
                             + ('; CALL BARRIER: unknown callee register/memory effects' if fact['barrier'] else ''))
                lines.extend('    stack: ' + access for access in fact['stack'])
        if len(block.code) > instruction_limit:
            lines.append(f'  Instructions truncated to {instruction_limit}/{len(block.code)}.')
        for at, (mark, left, right) in mismatches[:instruction_limit]:
            lines.append(f'  diff +0x{at:x} {mark}: {left} | {right}')
        if len(mismatches) > instruction_limit:
            lines.append('  Mismatch rows truncated.')
    if offset is None:
        lines.append(f'Blocks {start}:{start+len(selected)} of {len(blocks)}; use start/limit to page, offset to inspect.')
    lines.append('Use/def is instruction-local, aliases are conservative, stack offsets are not global frame slots; '
                 'calls prevent proving liveness across blocks. No interprocedural or fixed-point liveness is inferred.')
    return '\n'.join(line for line in lines if line)

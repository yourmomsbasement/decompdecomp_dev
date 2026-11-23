"""Processor syntax used by advisory code-block search, never by the match judge."""

import re


def control(processor: str, mnemonic: str) -> bool:
    op = mnemonic.lower().split('.')[0]
    if processor in {'x86', 'x86_64'}:
        return op.startswith(('j', 'loop', 'ret', 'call', 'iret')) or op in {'syscall', 'sysenter', 'int', 'int3', 'into', 'hlt', 'ud0', 'ud1', 'ud2'}
    if processor == 'mips':
        return op.startswith(('b', 'j')) or op in {'eret', 'syscall', 'break'}
    if processor == 'ppc':
        return op.startswith('b') or op in {'sc', 'rfi'}
    if processor in {'arm', 'thumb'}:
        return bool(re.fullmatch(r'b(l|x|lx)?(eq|ne|cs|cc|mi|pl|vs|vc|hi|ls|ge|lt|gt|le|al)?', op)) or op in {
            'cbz', 'cbnz', 'tbb', 'tbh', 'svc', 'pop'}
    return True  # no useful regions for an unknown processor


def register(processor: str, token: str) -> tuple[str, str] | None:
    """Physical register identity and operand width/class; fixed ABI/special registers stay literal.

    Keeping width and alias relationships avoids treating an eax write followed by a rax read as two unrelated
    temporaries. Stack/frame pointers remain distinct; offsets are retained (no guesses about stack layouts).
    """
    t = token.lower().lstrip('$%')
    if processor in {'x86', 'x86_64'}:
        fixed = {'rsp', 'esp', 'sp', 'spl', 'rbp', 'ebp', 'bp', 'bpl', 'rip', 'eip', 'fs', 'gs'}
        if t in fixed:
            return None
        for stem in ('a', 'b', 'c', 'd'):
            aliases = {f'r{stem}x': '64', f'e{stem}x': '32', f'{stem}x': '16', f'{stem}l': '8l', f'{stem}h': '8h'}
            if t in aliases:
                return stem, aliases[t]
        for stem in ('si', 'di'):
            aliases = {'r'+stem: '64', 'e'+stem: '32', stem: '16', stem+'l': '8l'}
            if t in aliases:
                return stem, aliases[t]
        if m := re.fullmatch(r'r(\d+)(d|w|b)?', t):
            return 'r'+m[1], {None: '64', 'd': '32', 'w': '16', 'b': '8l'}[m[2]]
        if m := re.fullmatch(r'(xmm|ymm|zmm)(\d+)', t):
            return 'v'+m[2], {'xmm': '128', 'ymm': '256', 'zmm': '512'}[m[1]]
    elif processor == 'mips':
        if t in {'zero', '0', 'sp', 'fp', 's8', 'gp', 'ra', 'hi', 'lo', 'at'}:
            return None
        if re.fullmatch(r'(v[01]|a[0-7]|t[0-9]|s[0-7])', t):
            return t, 'gpr'
        if re.fullmatch(r'f\d+', t):
            return t, 'float'
    elif processor == 'ppc':
        if t in {'r0', 'r1', 'r2', 'r13'}:
            return None
        if re.fullmatch(r'r\d+', t):
            return t, 'gpr'
        if re.fullmatch(r'f\d+', t):
            return t, 'float'
    elif processor in {'arm', 'thumb'}:
        if re.fullmatch(r'r([0-9]|1[012])', t):
            return t, 'gpr'
        # s/d/q register overlap is intentionally left literal until modeled correctly.
    return None


def implicit_registers(processor: str, lines: tuple[str, ...]) -> bool:
    """Conservatively keep physical names for x86 instructions with implicit accumulator/string operands."""
    return processor in {'x86', 'x86_64'} and any(
        line.split(None, 1)[0].lower() in {'mul', 'imul', 'div', 'idiv', 'cwd', 'cdq', 'cqo', 'cbw', 'cwde', 'cdqe',
                                         'cpuid', 'rdtsc', 'xlat', 'lods', 'lodsb', 'lodsw', 'lodsd', 'lodsq',
                                         'stos', 'stosb', 'stosw', 'stosd', 'stosq', 'rep', 'repe', 'repne'}
        for line in lines if line.strip())

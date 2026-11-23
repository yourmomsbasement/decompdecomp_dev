"""Conservative x86 instruction effects for advisory block inspection, separate from the judge.

Capstone supplies implicit operands and flag effects. Calls are ABI barriers: its register list alone cannot
prove that an arbitrary callee preserves a value. Stack references stay relative to each instruction's SP/FP.
"""

import capstone
from capstone import x86_const

from .blocks import register


def family(name):
    # Absolute and index-only operands have no base register in Capstone.
    if not name:
        return name
    alias = register('x86_64', name)
    if alias:
        return alias[0]
    if name in {'rsp', 'esp', 'sp', 'spl'}:
        return 'sp'
    if name in {'rbp', 'ebp', 'bp', 'bpl'}:
        return 'fp'
    return name


def effects(obj, max_insns=12000):
    """Effects indexed by target offset, with partial writes retaining the previous full register value."""
    mode = capstone.CS_MODE_64 if obj.arch == 'x86_64' else capstone.CS_MODE_32
    decoder = capstone.Cs(capstone.CS_ARCH_X86, mode)
    decoder.detail = True
    out = {}
    for count, ins in enumerate(decoder.disasm(obj.code, 0)):
        if count >= max_insns:
            break
        reads, writes = ins.regs_access()
        use = {family(ins.reg_name(r)) for r in reads}
        define = {family(ins.reg_name(r)) for r in writes}
        partial = set()
        for reg in writes:
            name = ins.reg_name(reg)
            alias = register(obj.arch, name)
            # Scalar SSE and masked/conditional vector operations can preserve lanes not listed as reads.
            # shortcut: retain every vector destination until lane/predicate effects are modeled explicitly.
            if ((alias and alias[1] in {'16', '8l', '8h', '128', '256', '512'})
                    or ins.mnemonic.startswith('cmov')):
                partial.add(family(name))
        use |= partial
        stack = []
        for operand in ins.operands:
            if operand.type != x86_const.X86_OP_MEM:
                continue
            base, index = ins.reg_name(operand.mem.base), ins.reg_name(operand.mem.index)
            if family(base) in {'sp', 'fp'}:
                access = operand.access
                action = '/'.join(name for bit, name in ((capstone.CS_AC_READ, 'read'),
                                                         (capstone.CS_AC_WRITE, 'write')) if access & bit)
                stack.append(f'{action or "unknown access"} {operand.size} bytes [{base}{operand.mem.disp:+#x}'
                             + (f'+{index}*{operand.mem.scale}' if index else '') + ']')
        barrier = ins.group(capstone.CS_GRP_CALL)
        if ins.mnemonic.startswith(('push', 'pop', 'ret', 'call', 'enter', 'leave')):
            stack.append('implicit stack access; SP changes (frame-relative slot not inferred)')
        flag_use, flag_def, flag_undefined = set(), set(), set()
        for name in dir(x86_const):
            if not name.startswith('X86_EFLAGS_') or not ins.eflags & getattr(x86_const, name):
                continue
            action, flag = name.removeprefix('X86_EFLAGS_').split('_', 1)
            if action in {'TEST', 'PRIOR'}:
                flag_use.add(flag.lower())
            else:
                flag_def.add(flag.lower())
                if action == 'UNDEFINED':
                    flag_undefined.add(flag.lower())
        out[ins.address] = {'use': use, 'def': define, 'full_def': define - partial, 'stack': stack, 'partial': partial,
                           'flags': bool(ins.eflags), 'flag_use': flag_use, 'flag_def': flag_def,
                           'flag_undefined': flag_undefined, 'barrier': barrier}
    return out

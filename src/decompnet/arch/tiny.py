"""Recognize a deliberately small set of tiny target instruction shapes; the compiler judge decides matches."""

import json
import re


def shape(processor, code):
    """Return (kind, value), without treating database instruction counts as disassembly evidence."""
    lines = [' '.join(ins.text.lower().split()) for ins in code]
    ops = [ins.op for ins in code]
    if processor in {'x86', 'x86_64'}:
        if lines == ['ret']:
            return 'empty', None
        if lines == ['ud2']:
            return 'trap', None
        if len(lines) == 2 and lines[-1] == 'ret' and not code[0].relocation:
            if re.fullmatch(r'xor (eax|rax),\s*\1', lines[0]):
                return 'constant', 0
            m = re.fullmatch(r'mov (?:eax|rax),\s*(-?(?:0x[0-9a-f]+|[0-9]+))', lines[0])
            if m:
                return 'constant', int(m[1], 16 if '0x' in m[1] else 10)
        # Only the sole argument is mapped: register numbering for later arguments depends on ABI.
        if len(lines) == 2 and lines[-1] == 'ret' and not code[0].relocation:
            if re.fullmatch(r'mov eax,\s*(?:edi|ecx)', lines[0]) and processor == 'x86_64':
                return 'identity32', None
            if re.fullmatch(r'mov rax,\s*(?:rdi|rcx)', lines[0]) and processor == 'x86_64':
                return 'identity_pointer', None
            if re.fullmatch(r'mov eax,\s*(?:dword ptr )?\[(?:rdi|rcx)\]', lines[0]) and processor == 'x86_64':
                return 'load32', None
            if re.fullmatch(r'mov eax,\s*(?:dword ptr )?\[esp\s*\+\s*(?:4|0x4)\]', lines[0]) and processor == 'x86':
                return 'identity32', None
        if processor == 'x86' and len(lines) == 3 and lines[-1] == 'ret' and not any(i.relocation for i in code):
            if re.fullmatch(r'mov eax,\s*(?:dword ptr )?\[esp\s*\+\s*(?:4|0x4)\]', lines[0]) and re.fullmatch(r'mov eax,\s*(?:dword ptr )?\[eax\]', lines[1]):
                return 'load32', None
        if ops == ['jmp'] and code[0].relocation:
            try:
                reloc = json.loads(code[0].relocation)
                # objdiff writes the addend as text ("-4"): read as a number, or no tail jump ever matched this
                addend = None if reloc[1] is None else int(reloc[1])
                symbol = reloc[2]
                if addend not in (None, 0, -4) or not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z_]\w*', symbol):
                    return 'unsupported', None
                # A jump through a pointer in memory at a symbol, nothing added (an x86-64 PLT entry's jump through
                # its GOT slot): a tail call through that pointer. Never a register, an index or an offset.
                if processor == 'x86_64' and re.fullmatch(rf'jmp qword ptr \[{re.escape(symbol.lower())}(?:-0x4)?\]', lines[0]):
                    return 'pointer_tail', symbol
                # A plain function symbol only; never an adjusted address, indirect jump, or jump table.
                if '[' not in lines[0]:
                    return 'tail', symbol
            except (ValueError, IndexError, TypeError):
                pass
    if processor == 'ppc' and lines == ['blr']:
        return 'empty', None
    if processor in {'arm', 'thumb'} and lines == ['bx lr']:
        return 'empty', None
    if processor == 'mips' and lines in (['jr $ra', 'nop'], ['jr ra', 'nop']):
        return 'empty', None
    return 'unsupported', None

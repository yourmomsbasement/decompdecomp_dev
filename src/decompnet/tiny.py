"""Tiny-function C templates from explicit, simple signatures; never invent an ABI from a lone return/jump."""

import re
from dataclasses import dataclass

from .arch.tiny import shape
from .forks import csource

# Complex C++, attributes, macros, varargs, unnamed arguments, and K&R forms are left to agents.
_TYPE = r'(?:const\s+)?(?:void|char|short|int|long|signed|unsigned|float|double|s8|u8|s16|u16|s32|u32|s64|u64|int32_t|uint32_t|int64_t|uint64_t|const|struct\s+[A-Za-z_]\w*)(?:\s+(?:char|short|int|long|signed|unsigned|const))*\s*\**'


@dataclass(frozen=True)
class Signature:
    head: str
    result: str
    types: tuple
    args: tuple


def signature(text, name):
    """Accept a simple existing definition or prototype, rejecting conflicting declarations in this text."""
    if not re.fullmatch(r'[A-Za-z_]\w*', name):
        return None
    clean = csource._blank(text)
    found = []
    pattern = rf'(?m)^\s*((?:(?:extern|static)\s+)?({_TYPE})\s+{re.escape(name)}\s*\(([^()]*)\))\s*(?:;|\{{)'
    for match in re.finditer(pattern, clean):
        result = ' '.join(match[2].split())
        params = match[3].strip()
        if params != 'void' and not params:
            continue  # f() is an unspecified argument list in C
        types, args = [], []
        for param in ([] if params == 'void' else params.split(',')):
            p = re.fullmatch(rf'\s*({_TYPE})\s*\b([A-Za-z_]\w*)\s*', param)
            if p is None or p[2] in {'void', 'int', 'long', 'short', 'unsigned', 'signed', 'char', 'const'}:
                break
            types.append(' '.join(p[1].split()))
            args.append(p[2])
        else:
            head = text[match.start(1):match.end(1)].strip()
            found.append(Signature(head, result, tuple(types), tuple(args)))
    if not found or any((s.result, s.types) != (found[0].result, found[0].types) for s in found):
        return None
    return found[0]


def candidate(text, name, processor, code):
    """One bounded template per recognized shape; return a reason when evidence is insufficient."""
    kind, value = shape(processor, code)
    if kind == 'unsupported':
        return None, 'unsupported instructions or padding'
    if kind == 'pointer_tail':
        return _pointer_tail(text, name, value)
    sig = signature(text, name)
    if sig is None:
        return None, 'missing or complex signature'
    if kind == 'empty':
        if sig.result != 'void':
            return None, 'return value unspecified by bare return'
        body = ''
    elif kind in {'identity32', 'identity_pointer', 'load32'}:
        integers = {'int', 'unsigned int', 'signed int', 'unsigned', 'signed', 's32', 'u32', 'int32_t', 'uint32_t'}
        if len(sig.args) != 1:
            return None, 'argument template requires exactly one explicit argument'
        if kind == 'identity32' and sig.result in integers and sig.types[0] == sig.result:
            body = f'return {sig.args[0]};'
        elif kind == 'identity_pointer' and '*' in sig.result and sig.result == sig.types[0]:
            body = f'return {sig.args[0]};'
        elif kind == 'load32' and sig.result in integers and re.fullmatch(re.escape(sig.result)+r'\s*\*', sig.types[0].replace('const ', '').strip()):
            body = f'return *{sig.args[0]};'
        else:
            return None, 'argument template requires compatible explicit scalar types'
    elif kind == 'trap':
        body = '__builtin_trap();'
    elif kind == 'constant':
        if '*' in sig.result or not re.fullmatch(r'(?:unsigned |signed )?(?:int|long|long long)|[su](?:32|64)|u?int(?:32|64)_t', sig.result):
            return None, 'constant return requires an explicit integer signature'
        if not -(2**63) <= value < 2**64:
            return None, 'constant outside supported integer range'
        body = f'return {value if value < 2**63 else str(value)+"ULL"};'
    else:
        other = signature(text, value)
        if value == name or other is None or (sig.result, sig.types) != (other.result, other.types):
            return None, 'tail destination signature missing or incompatible'
        body = ('return ' if sig.result != 'void' else '') + f'{value}({", ".join(sig.args)});'
    return sig.head+' { '+body+' }', kind


def _pointer_tail(text, name, slot):
    """`jmp [slot]`: a tail call through the function pointer at `slot` (an x86-64 PLT entry's jump through its GOT
    slot; four matched on a PS4 game written so). The jump forwards every register and the stack untouched, so any
    signature compiles to the same instruction: with none in the file, `void name(void)` claims no arguments either way
    (no ABI is guessed); with a simple one, its arguments are passed on. The slot is declared as an array of pointers,
    hidden, so the code reads it RIP-relative as the target does; a slot the file declares otherwise is left alone."""
    clean = csource._blank(text)
    if not re.fullmatch(r'[A-Za-z_]\w*', name):
        return None, 'missing or complex signature'
    own = rf'extern\s+void\s*\(\s*\*\s*{re.escape(slot)}\s*\[\s*\]\s*\)\s*\(\s*void\s*\)'
    declared = re.search(rf'(?<![\w]){re.escape(slot)}(?![\w])', clean)
    if declared and not re.search(own, clean):
        return None, 'pointer slot declared with another type'
    decl = '' if declared else f'extern void (*{slot}[])(void) __attribute__((visibility("hidden")));\n'
    sig = signature(text, name)
    if sig is None:
        if re.search(rf'(?<![\w]){re.escape(name)}(?![\w])', clean):
            return None, 'missing or complex signature'  # named in the file, but not as a simple declaration
        return decl + f'void {name}(void) {{ {slot}[0](); }}', 'pointer_tail'
    if sig.result == 'void' and not sig.args:
        body = f'{slot}[0]();'
    else:
        cast = f'({sig.result} (*)({", ".join(sig.types) or "void"}))'
        body = ('return ' if sig.result != 'void' else '') + f'({cast}{slot}[0])({", ".join(sig.args)});'
    return decl + sig.head + ' { ' + body + ' }', 'pointer_tail'

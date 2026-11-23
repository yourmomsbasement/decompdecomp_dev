"""Canonical x86-64 TLSGD-to-IE linker relaxation, gated by explicit slot mappings and target evidence.

Only the 16-byte ABI resolver sequence is replaced, at equal length. Compiler register allocation, function
layout, guards and all other relocations stay intact. Never infer slot identities from draft names or scores.
"""
import struct
from pathlib import Path

import capstone

_SHDR = struct.Struct('<IIQQQQIIQQ')
_SYM = struct.Struct('<IBBHQQ')
_RELA = struct.Struct('<QQq')

def _sections(data: bytes) -> dict:
    (shoff,) = struct.unpack_from('<Q', data, 40)
    (shentsize, shnum, shstrndx) = struct.unpack_from('<HHH', data, 58)
    secs = []
    for i in range(shnum):
        (name, kind, _flags, addr, off, size, link, info, _align, entsize) = _SHDR.unpack_from(data, shoff + i * shentsize)
        secs.append({'i': i, 'name': name, 'type': kind, 'addr': addr, 'off': off, 'size': size, 'link': link, 'info': info, 'entsize': entsize})
    return {'data': data, 'secs': secs, 'shoff': shoff, 'shentsize': shentsize, 'shstrndx': shstrndx}

def _symbols(tab: dict) -> list[tuple[str, int, int, int, int]]:
    """(name, type, section index, value, size)."""
    (data, secs) = (tab['data'], tab['secs'])
    symtab = next((s for s in secs if s['type'] == 2), None)
    if symtab is None:
        return []
    strtab = secs[symtab['link']]
    out = []
    for i in range(symtab['size'] // _SYM.size):
        (name_off, info, _other, shndx, value, size) = _SYM.unpack_from(data, symtab['off'] + i * _SYM.size)
        start = strtab['off'] + name_off
        end = data.index(b'\x00', start)
        out.append((data[start:end].decode('utf-8', 'replace'), info & 15, shndx, value, size))
    return out

def _supported(blob):
    return len(blob) >= 64 and blob[:6] == b'\x7fELF\x02\x01' and (struct.unpack_from('<HH', blob, 16) == (1, 62))

def _slot_evidence(target):
    blob = target.read_bytes()
    if not _supported(blob):
        return {}
    tab = _sections(blob)
    syms = _symbols(tab)
    byfield = {}
    for sec in tab['secs']:
        if sec['type'] != 4:
            continue
        for at in range(sec['off'], sec['off'] + sec['size'], _RELA.size):
            (off, info, addend) = _RELA.unpack_from(tab['data'], at)
            if info & 4294967295 == 2 and addend == -4:
                byfield[sec['info'], off] = syms[info >> 32][0]
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoder.detail = True
    found = {}
    for (name, kind, section, value, size) in syms:
        if kind != 2 or not 0 < section < len(tab['secs']):
            continue
        sec = tab['secs'][section]
        blob = tab['data'][sec['off'] + value:sec['off'] + value + size]
        instructions = list(decoder.disasm(blob, value))
        for (i, ins) in enumerate(instructions):
            ops = ins.operands
            if ins.mnemonic != 'mov' or len(ops) != 2 or ops[0].type != capstone.x86.X86_OP_REG or (ops[1].type != capstone.x86.X86_OP_MEM):
                continue
            mem = ops[1].mem
            if mem.segment != capstone.x86.X86_REG_FS or mem.base or mem.index or mem.disp:
                continue
            reg = ops[0].reg
            for follow in instructions[i + 1:i + 9]:
                q = follow.operands
                if follow.mnemonic == 'add' and len(q) == 2 and (q[0].type == capstone.x86.X86_OP_REG) and (q[0].reg == reg) and (q[1].type == capstone.x86.X86_OP_MEM) and (q[1].mem.base == capstone.x86.X86_REG_RIP):
                    slot = byfield.get((section, follow.address + follow.disp_offset))
                    if slot:
                        found.setdefault(name, set()).add(slot)
                    break
                if reg in follow.regs_access()[1]:
                    break
    return found

def relax_tls_gd_to_ie(obj: Path, target: Path | None, mapping: dict[str, str]) -> int:
    """Apply only canonical resolver pairs whose explicit slot is corroborated in the same target function."""
    if not mapping or target is None or (not target.exists()):
        return 0
    data = bytearray(obj.read_bytes())
    if not _supported(data) or not _supported(target.read_bytes()):
        return 0
    tab = _sections(bytes(data))
    syms = _symbols(tab)
    approved = _slot_evidence(target)
    symtab = next((s for s in tab['secs'] if s['type'] == 2))
    strtab = tab['secs'][symtab['link']]
    strings = bytearray(data[strtab['off']:strtab['off'] + strtab['size']])
    symbols = bytearray(data[symtab['off']:symtab['off'] + symtab['size']])
    indexes = {s[0]: i for (i, s) in enumerate(syms)}
    changed = 0
    for sec in tab['secs']:
        if sec['type'] != 4:
            continue
        rels = [list(_RELA.unpack_from(data, at)) for at in range(sec['off'], sec['off'] + sec['size'], _RELA.size)]
        byoff = {r[0]: i for (i, r) in enumerate(rels)}
        drop = set()
        body = tab['secs'][sec['info']]
        for (i, (off, info, addend)) in enumerate(rels):
            if info & 4294967295 != 19 or addend != -4 or off < 4:
                continue
            index = info >> 32
            (name, kind, shndx, _, _) = syms[index]
            slot = mapping.get(name)
            if not slot or kind != 6 or shndx != 0 or (name == slot):
                continue
            owner = next((s for s in syms if s[1] == 2 and s[2] == sec['info'] and (s[3] <= off - 4) and (off + 12 <= s[3] + s[4])), None)
            if not owner or slot not in approved.get(owner[0], set()):
                continue
            j = byoff.get(off + 8)
            if j is None:
                continue
            call = rels[j]
            if call[1] & 4294967295 != 4 or call[2] != -4 or syms[call[1] >> 32][0] != '__tls_get_addr':
                continue
            start = body['off'] + off - 4
            if data[start:start + 16] != bytes.fromhex('66488d3d00000000666648e800000000'):
                continue
            if slot in indexes:
                slot_index = indexes[slot]
                if slot_index < len(syms) and (syms[slot_index][1] not in {0, 1} or syms[slot_index][2] != 0):
                    continue
            else:
                slot_index = len(symbols) // _SYM.size
                indexes[slot] = slot_index
                symbols.extend(_SYM.pack(len(strings), 16, 0, 0, 0, 0))
                strings.extend(slot.encode() + b'\x00')
            data[start:start + 16] = bytes.fromhex('64488b04250000000048030500000000')
            rels[j] = [off + 8, slot_index << 32 | 2, -4]
            drop.add(i)
            changed += 1
        if drop:
            kept = b''.join((_RELA.pack(*r) for (i, r) in enumerate(rels) if i not in drop))
            data[sec['off']:sec['off'] + len(kept)] = kept
            struct.pack_into('<Q', data, tab['shoff'] + sec['i'] * tab['shentsize'] + 32, len(kept))
    if changed:
        for (sec, content) in [(strtab, strings), (symtab, symbols)]:
            data.extend(b'\x00' * (-len(data) % 8))
            struct.pack_into('<QQ', data, tab['shoff'] + sec['i'] * tab['shentsize'] + 24, len(data), len(content))
            data.extend(content)
        obj.write_bytes(data)
    return changed

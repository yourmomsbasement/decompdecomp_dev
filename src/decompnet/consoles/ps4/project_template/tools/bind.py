"""Bind the stack guard to the address the game fixed at link time.

clang emits the guardian as a PIC global: `mov r14, [rip + GOTPCREL __stack_chk_guard]` — a four-byte
displacement of zero and a relocation for the linker. The game's code has it resolved, because its GOT slot sits at
a fixed address (0xd9de00: 210 of 210 sampled canary functions load their guard pointer from it) in a binary that
is never relocated. A relocation on one side against a literal on the other can never compare equal, so this
writes the game's own displacement into the object and drops the relocation — what its linker would have done.

The displacement is relative to the instruction, so the instruction's address in the *game* is what matters: it is
taken from the delinked target object (which lays every function out at its real offset) through the symbol names
both objects share.

Usage: bind.py <object.o> [--target target.o] [--guard 0xd9de00]
"""

import argparse
import struct
import sys
from pathlib import Path

GUARD_SYMBOL = "__stack_chk_guard"
GUARD_ADDRESS = 0xD9DE00  # the GOT slot every canary prologue in the game reads its guard pointer from
GOTPCREL, GOTPCRELX, REX_GOTPCRELX, PC32 = 9, 41, 42, 2

SHDR = struct.Struct("<IIQQQQIIQQ")
SYM = struct.Struct("<IBBHQQ")
RELA = struct.Struct("<QQq")


def section_table(data: bytes) -> dict:
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
    secs = []
    for i in range(shnum):
        name, kind, _flags, addr, off, size, link, info, _align, entsize = SHDR.unpack_from(
            data, shoff + i * shentsize)
        secs.append({"i": i, "name": name, "type": kind, "addr": addr, "off": off, "size": size,
                     "link": link, "info": info, "entsize": entsize})
    return {"data": data, "secs": secs, "shoff": shoff, "shentsize": shentsize, "shstrndx": shstrndx}


def symbol_table(tab: dict) -> list[tuple[str, int, int, int, int]]:
    """(name, type, section index, value, size)."""
    data, secs = tab["data"], tab["secs"]
    symtab = next((s for s in secs if s["type"] == 2), None)
    if symtab is None:
        return []
    strtab = secs[symtab["link"]]
    out = []
    for i in range(symtab["size"] // SYM.size):
        name_off, info, _other, shndx, value, size = SYM.unpack_from(data, symtab["off"] + i * SYM.size)
        start = strtab["off"] + name_off
        end = data.index(b"\0", start)
        out.append((data[start:end].decode("utf-8", "replace"), info & 0xF, shndx, value, size))
    return out


def unit_vaddr(obj: Path) -> int | None:
    """A unit object is named after the game address it starts at: text_005C08A0 -> 0x5c08a0."""
    try:
        return int(obj.stem.rsplit("_", 1)[1], 16)
    except (IndexError, ValueError):
        return None


def game_addresses(target: Path) -> dict[str, int]:
    """name -> the address the game has that symbol at (from the delinked object, laid out at real offsets)."""
    base = unit_vaddr(target)
    if base is None:
        return {}
    tab = section_table(target.read_bytes())
    return {name: base + value for name, _t, shndx, value, _s in symbol_table(tab) if shndx != 0 and name}


def bind(obj: Path, target: Path | None, guard: int) -> int:
    data = bytearray(obj.read_bytes())
    if data[:4] != b"\x7fELF":
        return 0
    tab = section_table(bytes(data))
    secs, shoff, shentsize = tab["secs"], tab["shoff"], tab["shentsize"]
    syms = symbol_table(tab)
    addr_of = game_addresses(target) if target is not None else {}
    if not addr_of:
        return 0
    patched = 0
    for sec in secs:
        if sec["type"] != 4:  # SHT_RELA
            continue
        owner = next((s for s in syms if s[2] == sec["info"] and s[1] == 2), None)  # the section's STT_FUNC symbol
        if owner is None or owner[0] not in addr_of:
            continue
        func_addr = addr_of[owner[0]]
        count = sec["size"] // RELA.size
        kept = []
        for i in range(count):
            r_offset, r_info, _addend = RELA.unpack_from(data, sec["off"] + i * RELA.size)
            sym_index, r_type = r_info >> 32, r_info & 0xFFFFFFFF
            raw = bytes(data[sec["off"] + i * RELA.size: sec["off"] + (i + 1) * RELA.size])
            name = syms[sym_index][0] if sym_index < len(syms) else ""
            if r_type in (GOTPCREL, GOTPCRELX, REX_GOTPCRELX) and name == GUARD_SYMBOL:
                field = secs[sec["info"]]["off"] + r_offset
                insn_end = func_addr + (r_offset - owner[3]) + 4
                struct.pack_into("<I", data, field, (guard - insn_end) & 0xFFFFFFFF)
                patched += 1
                continue
            kept.append(raw)
        if len(kept) != count:
            body = b"".join(kept)
            data[sec["off"]:sec["off"] + len(body)] = body
            struct.pack_into("<Q", data, shoff + sec["i"] * shentsize + 32, len(body))  # sh_size
    if patched:
        obj.write_bytes(bytes(data))
    return patched


def relax_got(obj: Path, target: Path | None) -> int:
    """The linker's GOTPCREL relaxation, done here because nothing links these objects.

    `mov reg,[rip+GOT]` against a symbol the binary defines is relaxed by the linker into `lea reg,[rip+sym]`
    (GOTPCRELX), which is why the game's code reads `lea rax,[D]; load [rax+off]` while our object holds the
    pre-link `mov rax,[rip+GOT]`. Those are the same 7 bytes with 8B where the lea has 8D, so this rewrites the
    opcode and turns the relocation into R_X86_64_PC32 with the addend kept (-4) - exactly what `ld` leaves behind.

    Only symbols this binary defines: `D_`/`func_` names the delinked target names, i.e. the game's own, which are
    non-preemptible and therefore relaxable. An imported symbol (`__stack_chk_guard`, libc data) keeps its GOT load.
    """
    data = bytearray(obj.read_bytes())
    if data[:4] != b"\x7fELF" or target is None or not target.exists():
        return 0
    tab = section_table(bytes(data))
    secs, syms = tab["secs"], symbol_table(tab)
    known = {name for name, _t, shndx, _v, _s in symbol_table(section_table(target.read_bytes()))
             if name and (shndx != 0 or name.startswith(("D_", "func_")))}
    if not known:
        return 0
    relaxed = 0
    for sec in secs:
        if sec["type"] != 4:  # SHT_RELA
            continue
        base = secs[sec["info"]]["off"]  # sh_info: the section these relocations apply to
        for i in range(sec["size"] // RELA.size):
            at = sec["off"] + i * RELA.size
            r_offset, r_info, r_addend = RELA.unpack_from(data, at)
            sym_index, r_type = r_info >> 32, r_info & 0xFFFFFFFF
            if r_type not in (GOTPCREL, GOTPCRELX, REX_GOTPCRELX):
                continue
            name = syms[sym_index][0] if sym_index < len(syms) else ""
            if name not in known:
                continue
            field = base + r_offset  # the disp32
            start = field - 3  # REX 8B modrm disp32, or 8B modrm disp32
            if start >= base and data[start] in (0x48, 0x49, 0x4C, 0x4D) and data[start + 1] == 0x8B \
                    and (data[start + 2] & 0xC7) == 0x05:
                data[start + 1] = 0x8D
            elif field - 2 >= base and data[field - 2] == 0x8B and (data[field - 1] & 0xC7) == 0x05:
                data[field - 2] = 0x8D
            else:
                continue  # not the load-of-address shape: leave the GOT access alone
            struct.pack_into("<Q", data, at + 8, (sym_index << 32) | PC32)
            relaxed += 1
    if relaxed:
        obj.write_bytes(bytes(data))
    return relaxed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("object")
    parser.add_argument("--target", help="the delinked object the addresses come from (default: beside build/)")
    parser.add_argument("--guard", type=lambda s: int(s, 0), default=GUARD_ADDRESS)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    obj = Path(args.object)
    target = Path(args.target) if args.target else obj.parent.parent.parent / "target" / obj.name
    exists = target.exists()
    n = bind(obj, target if exists else None, args.guard)
    relaxed = relax_got(obj, target if exists else None)
    if not args.quiet:
        print(f"{obj}: bound {n} guard load(s) to 0x{args.guard:x}, relaxed {relaxed} GOT load(s)"
              + ("" if exists else f" (no target object at {target})"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

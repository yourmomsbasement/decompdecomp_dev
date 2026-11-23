"""Bind a switch's jump table to the game's, where the two tables are the same.

clang compiles a switch into `lea reg,[rip+table]; movsxd idx,[reg+i*4]; add idx,reg; jmp idx`, and puts the table
in the object's own read-only data. In the game the linker placed that table among the game's read-only data, so the
delinked target loads it from a game symbol (`D_009889E8`), and our object's load names its own section: the same
instruction, with relocations that never compare equal. On a PS4 game 391 open functions dispatched through such a
table, and none had matched.

This does what the game's linker did, only where the tables are provably the same: for each table load of a function,
the target's table (read from the game's image at the symbol's address) and ours (from its relocations) must send every
case to the same offset in the function. Then our load names the game's symbol, as the target's does. A table with
any case elsewhere is left alone, and the judge says so; nothing about the instructions is changed.

Usage: jump_tables.py <object.o> --config config/<version>/functions.json
"""

import argparse
import json
import struct
import sys
from pathlib import Path

from bind import PC32, RELA, SYM, game_addresses, section_table, symbol_table

LEA = 0x8D
MOVSXD = 0x63
MAX_CASES = 4096


def image_reader(elf: Path):
    """address -> bytes of the game's image (its loaded segments), or None outside them."""
    data = elf.read_bytes()
    phoff, = struct.unpack_from("<Q", data, 0x20)
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    segments = []
    for i in range(phnum):
        _type, _flags, offset, vaddr, _paddr, filesz, _memsz, _align = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + i * phentsize)
        if filesz:
            segments.append((vaddr, filesz, offset))

    def read(address: int, length: int) -> bytes | None:
        for vaddr, filesz, offset in segments:
            if vaddr <= address and address + length <= vaddr + filesz:
                return data[offset + address - vaddr: offset + address - vaddr + length]
        return None
    return read


def _relocations(tab: dict) -> dict[int, list[tuple[int, int, int, int]]]:
    """section index -> [(r_offset, symbol index, type, addend)] of the relocations that apply to it."""
    out = {}
    data = tab["data"]
    for sec in tab["secs"]:
        if sec["type"] != 4:  # SHT_RELA
            continue
        rows = []
        for at in range(sec["off"], sec["off"] + sec["size"], RELA.size):
            offset, info, addend = RELA.unpack_from(data, at)
            rows.append((offset, info >> 32, info & 0xFFFFFFFF, addend))
        out.setdefault(sec["info"], []).extend(rows)
    return out


HOISTED = 4096  # bytes after a table's lea where its `movsxd` may be: a loop's dispatch, its lea in the prologue


def _scaled_load(code: bytes, nxt: int, base: int | None = None) -> bool:
    """Whether `movsxd reg,[base+reg*4]` starts at `nxt` (REX 63 ModRM SIB: a memory operand through a SIB byte, rm
    100, whose index is scaled by 4), through `base` (a register number, 0-15) when it's given. A base of rbp or r13
    is encoded with a zero 8-bit displacement (mod 01), since mod 00 with that base means none."""
    if not (nxt + 4 <= len(code) and code[nxt] & 0xF0 == 0x40 and code[nxt + 1] == MOVSXD
            and code[nxt + 2] & 0x07 == 0x04 and code[nxt + 3] >> 6 == 2):
        return False
    mod, low = code[nxt + 2] >> 6, code[nxt + 3] & 7
    if not (mod == 0 and low != 5 or mod == 1 and low == 5 and nxt + 5 <= len(code) and code[nxt + 4] == 0):
        return False
    return base is None or ((code[nxt] & 1) << 3 | low) == base


def _table_load(code: bytes, at: int, end: int | None = None) -> bool:
    """Whether the displacement at `at` is a `lea reg,[rip+disp]` whose register a `movsxd reg,[reg+reg*4]` reads: right
    after it, or (`end`: where the function ends) later in the function. clang hoists a loop's table address out of
    the loop: one of a PS4 game's tables is loaded into r10 in the prologue, and dispatched through 0x40 bytes on.
    The later load is found by its bytes, not by following every instruction: the tables' contents decide what's bound
    (every case must land where the game's does), so a load that only looks like one binds nothing."""
    start = at - 3
    if start < 0 or code[start] & 0xF0 != 0x40 or code[start + 1] != LEA or code[start + 2] & 0xC7 != 0x05:
        return False
    if _scaled_load(code, at + 4):
        return True
    if end is None:
        return False
    reg = (code[start] >> 2 & 1) << 3 | code[start + 2] >> 3 & 7
    if reg == 4:  # (rsp: never a table's)
        return False
    return any(_scaled_load(code, nxt, reg) for nxt in range(at + 4, min(end, at + 4 + HOISTED, len(code)) - 3))


def target_tables(target: Path) -> dict[tuple[str, int], tuple[str, int, int]]:
    """(function, offset of the load's displacement in it) -> (the game symbol, the reloc's addend, the function's
    game address), for each table load of the delinked target."""
    tab = section_table(target.read_bytes())
    syms, relocs = symbol_table(tab), _relocations(tab)
    addresses = game_addresses(target)
    out = {}
    for shndx, rows in relocs.items():
        code = tab["data"][tab["secs"][shndx]["off"]:][: tab["secs"][shndx]["size"]]
        funcs = [s for s in syms if s[1] == 2 and s[2] == shndx and s[4]]
        for offset, sym, kind, addend in rows:
            name = syms[sym][0] if sym < len(syms) else ""
            owner = next((f for f in funcs if f[3] <= offset < f[3] + f[4]), None)
            if (kind != PC32 or not name.startswith("D_") or owner is None
                    or not _table_load(code, offset, owner[3] + owner[4])):
                continue
            if owner[0] in addresses:
                out[(owner[0], offset - owner[3])] = (name, addend, addresses[owner[0]])
    return out


def bind_tables(obj: Path, target: Path | None, read) -> int:
    """Our table loads named after the game's tables where both send every case to the same offset; how many."""
    if target is None or not target.exists():
        return 0
    wanted = target_tables(target)
    if not wanted:
        return 0
    data = bytearray(obj.read_bytes())
    if data[:4] != b"\x7fELF":
        return 0
    tab = section_table(bytes(data))
    secs, syms, relocs = tab["secs"], symbol_table(tab), _relocations(tab)
    reloc_at = {}  # (section, r_offset) -> where that relocation's entry is in the file
    for sec in secs:
        if sec["type"] == 4:
            for k, at in enumerate(range(sec["off"], sec["off"] + sec["size"], RELA.size)):
                reloc_at[(sec["info"], RELA.unpack_from(data, at)[0])] = at
    symtab = next(s for s in secs if s["type"] == 2)
    strtab = secs[symtab["link"]]
    strings = bytearray(data[strtab["off"]:strtab["off"] + strtab["size"]])
    symbols = bytearray(data[symtab["off"]:symtab["off"] + symtab["size"]])
    indexes = {s[0]: i for i, s in enumerate(syms) if s[0]}
    # where each table starts, per section: a function with two switches can keep both tables in one section, back
    # to back, and the first ends where the second begins
    starts: dict[int, set[int]] = {}
    for shndx, rows in relocs.items():
        code = bytes(data[secs[shndx]["off"]:secs[shndx]["off"] + secs[shndx]["size"]])
        funcs = [s for s in syms if s[1] == 2 and s[2] == shndx and s[4]]
        for offset, sym, kind, addend in rows:
            owner = next((f for f in funcs if f[3] <= offset < f[3] + f[4]), None)
            if (kind == PC32 and sym < len(syms) and syms[sym][1] == 3 and owner is not None
                    and _table_load(code, offset, owner[3] + owner[4])):
                starts.setdefault(syms[sym][2], set()).add(addend + 4)
    bound = 0
    for shndx, rows in relocs.items():
        code = bytes(data[secs[shndx]["off"]:secs[shndx]["off"] + secs[shndx]["size"]])
        funcs = [s for s in syms if s[1] == 2 and s[2] == shndx and s[4]]
        for offset, sym, kind, addend in rows:
            owner = next((f for f in funcs if f[3] <= offset < f[3] + f[4]), None)
            if owner is None or kind != PC32 or not _table_load(code, offset, owner[3] + owner[4]):
                continue
            game = wanted.get((owner[0], offset - owner[3]))
            table_sec = syms[sym][2] if sym < len(syms) else 0
            if game is None or syms[sym][1] != 3 or not 0 < table_sec < len(secs):  # (ours: a section symbol)
                continue
            name, game_addend, func_addr = game
            start = addend + 4  # the table's offset in its section: rip (the load's end) + addend + 4 - field
            ours = []
            entries = {o: (s, a) for o, s, t, a in relocs.get(table_sec, []) if t == PC32}
            for k in range(MAX_CASES):  # each entry: case label - table start, a PC32 into the function's section
                if k and start + 4 * k in starts.get(table_sec, ()):
                    break  # the next table
                entry = entries.get(start + 4 * k)
                if entry is None or syms[entry[0]][2] != shndx:
                    break
                ours.append(syms[entry[0]][3] + entry[1] - 4 * k - owner[3])  # the case's offset in the function
            try:
                table_addr = int(name[2:], 16)
            except ValueError:
                continue
            raw = read(table_addr, 4 * len(ours)) if len(ours) >= 2 else None
            if raw is None:
                continue
            theirs = [table_addr + e - func_addr for e in struct.unpack(f"<{len(ours)}i", raw)]
            if theirs != ours:
                continue  # some case goes elsewhere: not the game's table, and the judge will say so
            if name not in indexes:
                indexes[name] = len(symbols) // SYM.size
                symbols.extend(SYM.pack(len(strings), 0x10, 0, 0, 0, 0))  # global, undefined: the game's data
                strings.extend(name.encode() + b"\0")
            at = reloc_at[(shndx, offset)]
            struct.pack_into("<Qq", data, at + 8, (indexes[name] << 32) | PC32, game_addend)
            bound += 1
    if bound:
        for sec, content in ((strtab, strings), (symtab, symbols)):
            data.extend(b"\0" * (-len(data) % 8))
            struct.pack_into("<QQ", data, tab["shoff"] + sec["i"] * tab["shentsize"] + 24, len(data), len(content))
            data.extend(content)
        obj.write_bytes(bytes(data))
    return bound


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("object")
    parser.add_argument("--config", required=True)
    parser.add_argument("--target", help="the delinked object (default: beside build/, as bind.py finds it)")
    args = parser.parse_args()
    obj = Path(args.object)
    target = Path(args.target) if args.target else obj.parent.parent.parent / "target" / obj.name
    config = json.loads(Path(args.config).read_text())
    elf = Path(config["elf"])
    if not elf.exists() or not target.exists():
        return 0
    bind_tables(obj, target, image_reader(elf))
    return 0


if __name__ == "__main__":
    sys.exit(main())

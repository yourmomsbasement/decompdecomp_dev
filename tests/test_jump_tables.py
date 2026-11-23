"""A switch's jump table, named as the game names it where the two tables are the same (the PS4 template's
tools/jump_tables.py): on a PS4 game 391 open functions dispatched through one, and none had matched."""

import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from decompnet import toolchains
from decompnet.consoles.ps4 import units

TOOLS = Path(units.__file__).with_name("project_template") / "tools"


def load():
    sys.path.insert(0, str(TOOLS))
    try:
        spec = importlib.util.spec_from_file_location("ps4_jump_tables", TOOLS / "jump_tables.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(TOOLS))


def test_a_table_load_is_lea_then_a_scaled_movsxd():
    jt = load()
    code = bytes.fromhex("488d0d00000000" "48630481")  # lea rcx,[rip+T]; movsxd rax,[rcx+rax*4]
    assert jt._table_load(code, 3)
    assert not jt._table_load(bytes.fromhex("488d0d00000000" "8b0481"), 3)  # a plain load after it
    assert not jt._table_load(bytes.fromhex("488d0d00000000" "48630441"), 3)  # scaled by 2: not 32-bit entries
    assert not jt._table_load(bytes.fromhex("488b0d00000000" "48630481"), 3)  # mov, not lea


def fake_game(path: Path, vaddr: int, payload: bytes) -> Path:
    """An ELF64 with one loaded segment holding `payload` at `vaddr`: what image_reader needs of the game."""
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    struct.pack_into("<Q", header, 0x20, 64)  # e_phoff
    struct.pack_into("<HH", header, 0x36, 56, 1)  # e_phentsize, e_phnum
    phdr = struct.pack("<IIQQQQQQ", 1, 4, 64 + 56, vaddr, vaddr, len(payload), len(payload), 1)
    path.write_bytes(bytes(header) + phdr + payload)
    return path


def test_the_game_image_is_read_by_address(tmp_path):
    jt = load()
    read = jt.image_reader(fake_game(tmp_path / "eboot.elf", 0x1000, b"abcdefgh"))
    assert read(0x1002, 3) == b"cde" and read(0x0FFF, 2) is None and read(0x1006, 4) is None


def clang():
    for tid in ("clang-3.9.1", "clang-3.8.1"):
        if toolchains.installed(tid):
            return toolchains.binary(tid)
    for p in ("/opt/llvm-5.0.2-globalguard/bin/clang",):
        if Path(p).exists():
            return Path(p)
    return None


SWITCH = """int g(int);
int f(int x) {
    switch (x) { case 0: return g(1); case 1: return g(7); case 2: return g(3); case 3: return g(9); case 4: return g(5); }
    return 0;
}
"""


@pytest.mark.skipif(clang() is None, reason="needs a clang")
@pytest.mark.parametrize("same", [True, False])
def test_a_table_the_game_has_is_bound_and_one_it_doesnt_isnt(tmp_path, same):
    sys.path.insert(0, str(TOOLS))
    try:
        jt = load()
        from bind import section_table, symbol_table

        spec = importlib.util.spec_from_file_location("ps4_delink", TOOLS / "delink.py")
        delink = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(delink)
    finally:
        sys.path.remove(str(TOOLS))
    src = tmp_path / "text_00002000.c"
    src.write_text(SWITCH)
    built = tmp_path / "build/v/src/text_00002000.o"
    built.parent.mkdir(parents=True)
    subprocess.run([str(clang()), "-O2", "-fPIE", "-ffunction-sections", "-c", str(src), "-o", str(built)], check=True)
    tab = section_table(built.read_bytes())
    syms, relocs = symbol_table(tab), jt._relocations(tab)
    f = next(s for s in syms if s[0] == "f")
    code = tab["data"][tab["secs"][f[2]]["off"]:][:f[4]]
    load_at = next(o for o, s, t, a in relocs[f[2]] if syms[s][1] == 3 and jt._table_load(code, o))
    table_sec = next(syms[s][2] for o, s, t, a in relocs[f[2]] if o == load_at)
    cases = [syms[s][3] + a - 4 * k for k, (o, s, t, a) in enumerate(sorted(relocs[table_sec]))]
    assert len(cases) == 5
    # the target: f's code at the unit's start (0x2000), its table load naming the game's table at 0x1000
    names = [("D_00001000" if o == load_at else syms[s][0] or "g", t, a) for o, s, t, a in relocs[f[2]]]
    target = tmp_path / "build/v/target/text_00002000.o"
    target.parent.mkdir(parents=True)
    target.write_bytes(delink.elf64("text_00002000", [{"name": "f", "addr": 0x2000, "size": f[4], "relocs": [
        (o, t, n, a) for (o, _, _, _), (n, t, a) in zip(relocs[f[2]], names)]}], code, 0x2000))
    entries = [0x2000 + c - 0x1000 for c in cases]
    if not same:
        entries[2], entries[3] = entries[3], entries[2]  # two cases swapped: not the game's table
    game = fake_game(tmp_path / "eboot.elf", 0x1000, struct.pack("<5i", *entries))
    before = built.read_bytes()
    assert jt.bind_tables(built, target, jt.image_reader(game)) == (1 if same else 0)
    if not same:
        assert built.read_bytes() == before
        return
    tab = section_table(built.read_bytes())
    syms = symbol_table(tab)
    rel = next(r for r in jt._relocations(tab)[next(s for s in syms if s[0] == "f")[2]] if r[0] == load_at)
    assert syms[rel[1]][0] == "D_00001000" and rel[3] == -4 and syms[rel[1]][2] == 0  # undefined: the game's data


def test_target_table_in_a_later_function_keeps_its_original_address(tmp_path):
    jt = load()
    spec = importlib.util.spec_from_file_location("per_function_delink", TOOLS / "delink.py")
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    code = bytes.fromhex("488d0d0000000048630481c3")
    functions = [{"name": "first", "addr": 0x1000, "size": 1},
                 {"name": "later", "addr": 0x1020, "size": len(code), "relocs": [(3, 2, "D_00004000", -4)]}]
    target = tmp_path / "arbitrary_unit.o"
    target.write_bytes(writer.elf64("unit", functions, b"\xc3" + b"\x90" * 31 + code, 0x1000))
    assert jt.target_tables(target) == {("later", 3): ("D_00004000", -4, 0x1020)}

"""An x86-64 ELF program's PLT is the linker's: on a PS4 game 844 of the 989 open functions of 1-3 instructions were
PLT entries' halves. Its header and the lazy-binding steps are set aside; the entries' `jmp [rip+GOT slot]` isn't, as
C makes it (`D_00D9E160[0]();` matched four on that game)."""

from decompnet import arch
from decompnet.app import twins

from .test_ps4_targets import delinker

HEADER = bytes.fromhex("ff3500000000" "ff2500000000" "0f1f4000")  # push [rip+GOT+8]; jmp [rip+GOT+16]; nop
JUMP = bytes.fromhex("ff2500000000")  # jmp [rip+its GOT slot]
STEP = bytes.fromhex("6800000000" "e900000000")  # push <index>; jmp <the header>
RET1 = bytes.fromhex("b801000000c3")  # mov eax, 1; ret


def test_the_plts_header_and_steps_are_the_linkers():
    reason = arch.get("x86_64").asm_reason
    assert "lazy-binding step" in reason(STEP)
    assert "PLT's header" in reason(HEADER) and "PLT's header" in reason(HEADER[:12])
    assert reason(JUMP) is None  # a tail call through the GOT slot: C makes it
    assert reason(RET1) is None and reason(STEP + b"\xc3") is None


def test_a_programs_plt_is_set_aside_in_its_object_and_c_code_isnt():
    pieces = [("plt0", HEADER), ("jump0", JUMP), ("step0", STEP), ("jump1", JUMP), ("step1", STEP), ("ret1", RET1)]
    functions, blob = [], b""
    for name, code in pieces:
        functions.append({"name": name, "addr": len(blob), "size": len(code), "relocs": []})
        blob += code
    prints = twins.object_prints(delinker().elf64("text_plt", functions, blob, 0))
    assert {n for n, p in prints.items() if p.asm} == {"plt0", "step0", "step1"}
    assert "the linker writes it" in prints["step1"].asm


def test_a_function_of_nothing_but_nops_is_padding():
    """The function map's padding-only entries (`nop; nop dword ptr [rax]`, between two real functions) are no C."""
    reason = arch.get("x86_64").asm_reason
    assert "alignment padding" in reason(bytes.fromhex("90" "0f1f4000"))
    assert "alignment padding" in reason(bytes.fromhex("90" "66" "0f1f440000"))
    assert reason(bytes.fromhex("90" "c3")) is None  # a ret: a function
    assert reason(b"\x90" * 40) is None  # longer than any alignment gap

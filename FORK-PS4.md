# PS4 (Orbis) support — this fork

Adds a `ps4` console to decompdecomp: it imports an `eboot.elf` (Sony ELF64, `e_type 0xfe10`, no section headers),
makes a project for it, and judges functions with objdiff like any other PROJECT console.

Status: **beta, import and judge working on inFAMOUS Second Son** — 27,602 functions cut, measured, one function
matched byte for byte from the GUI (`/api/try` → "MATCH! func_00000320 is 100% identical").

## What was added

    src/decompnet/consoles/ps4/__init__.py            the console: sniff, facts, the parts the app calls
    src/decompnet/consoles/ps4/scratch.py             ELF64 reading, function analysis, project creation
    src/decompnet/consoles/ps4/project_template/tools/  configure.py, delink.py (ELF64 objects), compile.py
    src/decompnet/consoles/ps4/guides/ps4.md          what agents read first: the compiler pin and the deltas
    src/decompnet/consoles/__init__.py                registered in _registry()

The compiler is pinned by measurement: **clang 5.0.2** with
`-O2 -mavx -fno-omit-frame-pointer -fPIE -mllvm -x86-use-vzeroupper=0 -fno-stack-protector
-fno-asynchronous-unwind-tables`. On 20 hand-transcribed functions of inFAMOUS Second Son it reproduces 14 byte for
byte and 18 as the same instruction set; the flag evidence (99.7% rbp prologues, 21,093/27,602 VEX functions, exactly
one `vzeroupper`) is in `guides/ps4.md`.

Where clang lives: `DECOMP_PS4_CLANG`, else `/opt/clang-5.0.2/bin/clang`, `clang-5.0` on PATH, or a
`~/.hermes/cache/scratch/clangs/clang+llvm-5.0.2*/bin/clang` checkout.

## How an import goes

1. `sniff` accepts ELF64 x86-64 with Sony's `e_type` (0xfe10 exec, 0xfe04 dynexec).
2. The functions come from a `functions.jsonl` beside the game (or a subdirectory of it) — decompdecomp's own map
   format, `{"a": offset hex, "s": size, "n": name}` — else from Ghidra via `forks.ghidra.functions`.
3. `analyse` reads every function with capstone: calls and jumps become `R_X86_64_PC32` relocations, rip-relative
   memory operands become `R_X86_64_PC32` relocations, and the function's bytes are otherwise kept.
4. `units` groups ~4 KiB runs of neighbouring functions into source files (`src/auto/text_<addr>.c`).
5. The project is built once: `tools/delink.py` writes one ELF64 relocatable object per unit with the game's bytes at
   their original offsets (so intra-unit distances stay right), a global `func_<addr>` symbol per function, and a
   relocation per external reference; `tools/configure.py` writes `build.ninja` + `objdiff.json`; `ninja prepare`
   compiles the (initially empty) sources with the pinned clang.

## Known gaps

- **Absolute-address immediates are not relocated.** objdiff's x86 reader refuses a relocation that falls on an
  instruction's first byte (`Unsupported inline x86 relocation`), and its decoder disagrees with capstone about
  boundaries often enough that one unit in a few hundred trips it, failing the whole report. A function that loads an
  absolute address therefore shows one argument mismatch where a relocation would have masked it.
- **No demo game.** `tests/test_consoles.py -k ps4` passes (the console is well-formed), but the console can't be
  recommended until a real game rebuilds byte for byte.
- **No symbol renaming, no rebuild check, no per-file compiler choice** — the `rename_symbol`, `check_rebuild`,
  `file_compiler` parts are the defaults, so the GUI says so.
- **Sony NIDs are not resolved**: imports appear as `func_<stub address>` until a NID table names them.
- The delinker keeps each unit's original byte range, so gaps between functions are part of the target object; objdiff
  counts them as section data.

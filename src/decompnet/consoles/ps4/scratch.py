"""Start a PlayStation 4 decompilation from scratch: a project made for the user's own eboot.elf, judged by objdiff.

An eboot is an ELF64 with Sony's own e_type (0xFE10) and no section headers at all: code and data are only reachable
through the program headers, so this reads the image the way the loader does. Everything is linked at 0, which is
what makes the addresses in the code comparable with a relocatable object's.

- `read_elf` / `ELF`: the file's PT_LOAD segments, entry point, address→offset.
- `map_functions`: the functions of the game, from a `functions.jsonl` map next to the file (what the reverse
  engineering of inFAMOUS Second Son produced: {"a": text offset hex, "s": size, "n": name, "i": import NIDs}),
  else from Ghidra's analysis.
- `analyse`: each function's extent, symbol (`func_00000320`, data `D_0009866D0`) and relocations, read with
  capstone: calls and jumps to other functions, rip-relative data addresses, absolute immediates in the image.
- `units`: source files, runs of about 4 KiB of neighbouring functions.
- `create`: the project itself (project_template/), built once with the pinned clang.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import capstone
from capstone import x86

from decompnet.hostenv import NO_WINDOW

from ...forks.project import ForkError

PT_LOAD = 1
PF_X = 0x1
EM_X86_64 = 62
HEAD_BYTES = 64

# ELF64 relocation types this writes into the target objects (objdiff masks the bytes they fill, so a function's
# absolute address in the game and the C's symbol both compare as "relocated")
R_X86_64_64, R_X86_64_PC32, R_X86_64_PLT32, R_X86_64_32, R_X86_64_32S = 1, 2, 4, 10, 11

UNIT_BYTES = 0x1000
TEMPLATE = Path(__file__).with_name("project_template")
PAD = (0x90, 0xCC, 0x00)
READ_DEFAULT_BYTES, READ_MIN_BYTES, READ_MAX_BYTES = 64, 4, 512
HEX_ROW_BYTES = 16
POINTERS_SHOWN, FLOATS_SHOWN = 24, 8
PLAUSIBLE_FLOAT = (1e-6, 1e7)
MIN_STRING_CHARS = 3
PRINTABLE = range(32, 127)
TOOL_SECONDS = 3600
ERROR_TAIL = 1500

TYPES_H = """/* Basic types for this decompilation (x86-64, clang 5.0.2, written in C). Agents add structs and prototypes here
 * or in new headers next to it. The game's functions are func_<address> and its data D_<address>: write them with
 * those names, as C, and the build gives them the original's symbols. Data: `extern int D_9866D0;`. */
#ifndef COMMON_H
#define COMMON_H

typedef signed char s8;
typedef unsigned char u8;
typedef signed short s16;
typedef unsigned short u16;
typedef signed int s32;
typedef unsigned int u32;
typedef signed long long s64;
typedef unsigned long long u64;
typedef float f32;
typedef double f64;
typedef int BOOL;
#define TRUE 1
#define FALSE 0
#ifndef NULL
#define NULL 0
#endif

#endif
"""

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class Segment:
    va: int
    offset: int
    filesz: int
    memsz: int
    flags: int

    @property
    def end(self) -> int:
        return self.va + self.memsz


@dataclass
class ELF:
    data: bytes
    entry: int
    e_type: int
    segments: list[Segment]

    @property
    def text(self) -> Segment:
        """The executable segment: the game's code, and the constants the code reads in place."""
        for s in self.segments:
            if s.flags & PF_X:
                return s
        raise ForkError("this ELF has no executable segment")

    def segment_at(self, addr: int) -> Segment | None:
        return next((s for s in self.segments if s.va <= addr < s.va + max(s.filesz, s.memsz)), None)

    def offset(self, addr: int) -> int | None:
        s = self.segment_at(addr)
        return None if s is None or addr - s.va >= s.filesz else s.offset + addr - s.va

    def read(self, addr: int, n: int) -> bytes:
        off = self.offset(addr)
        return b"" if off is None else self.data[off:off + n]

    def contains(self, addr: int) -> bool:
        s = self.segment_at(addr)
        return s is not None and addr - s.va < s.filesz


def is_elf64(head: bytes) -> bool:
    return len(head) >= HEAD_BYTES and head[:4] == b"\x7fELF" and head[4] == 2 and head[5] == 1 \
        and int.from_bytes(head[18:20], "little") == EM_X86_64


def read_elf(data: bytes) -> ELF:
    if not is_elf64(data[:HEAD_BYTES]):
        raise ForkError("this isn't an x86-64 ELF: PS4 games start from their eboot.elf")
    e_type = struct.unpack_from("<H", data, 16)[0]
    entry, phoff = struct.unpack_from("<QQ", data, 24)
    phentsize, phnum = struct.unpack_from("<HH", data, 54)
    if not phoff or not phnum or phoff + phentsize * phnum > len(data):
        raise ForkError("this ELF's program headers are missing or past the end of the file")
    segments = []
    for i in range(phnum):
        at = phoff + phentsize * i
        p_type, p_flags, p_offset, p_vaddr, _, p_filesz, p_memsz, _ = struct.unpack_from("<IIQQQQQQ", data, at)
        if p_type != PT_LOAD:
            continue
        segments.append(Segment(p_vaddr, p_offset, p_filesz, p_memsz, p_flags))
    if not segments:
        raise ForkError("this ELF has no PT_LOAD segments")
    return ELF(data, entry, e_type, segments)


def clang_path() -> Path | None:
    """The pinned clang 5.0.2 this project builds with: DECOMP_PS4_CLANG, or one of the places it is installed."""
    for candidate in (os.environ.get("DECOMP_PS4_CLANG"), "/opt/llvm-5.0.2-globalguard/bin/clang",
                      "/opt/clang-5.0.2/bin/clang", shutil.which("clang-5.0"), shutil.which("clang-5.0.2")):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    for pattern in ("*/.hermes/cache/scratch/clangs/clang+llvm-5.0.2*/bin/clang",
                    "*/dev/clangs/clang+llvm-5.0.2*/bin/clang"):
        hits = sorted(Path("/home").glob(pattern))
        if hits:
            return hits[0]
    return None


def write_atomic(path: Path, text: str) -> None:
    """Write beside the file and rename: the app and agents can act on the same project at once."""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def game_info(path: Path) -> dict:
    data = path.read_bytes()
    elf = read_elf(data)
    stem = re.sub(r"[^a-z0-9]+", "", path.stem.lower()) or "game"
    title = path.stem if path.stem.lower() != "eboot" else (path.parent.name or path.stem)
    return {"platform": "ps4", "id": stem, "title": title, "region": "PS4",
            "sha1": hashlib.sha1(data).hexdigest(), "entry": elf.entry, "e_type": elf.e_type,
            "segments": len(elf.segments),
            "compiler": "clang-5.0.2", "compiler_why": "the game's code is clang's, pinned by fingerprint"}


# ---------------------------------------------------------------------------------------------------- functions


@dataclass
class Function:
    addr: int
    size: int
    name: str
    relocs: list[tuple[int, int, str | None, int]] = field(default_factory=list)  # (offset, type, symbol, addend)


_NOP_BYTES = frozenset({0x90, 0xCC})  # one-byte nop and int3: what alignment padding is made of
_NOP_PARTS = frozenset({0x90, 0xCC, 0x66, 0x2E, 0x0F, 0x1F, 0x84, 0x00})  # the 1..11-byte nop encodings clang emits


def is_padding(blob: bytes) -> bool:
    """A run that is nothing but alignment nops. Function maps list some of these as functions because a scan saw
    a call or a boundary there; no C can be them, since even an empty function is a `ret`."""
    if not blob or len(blob) > 64:
        return False
    if all(b in _NOP_BYTES for b in blob):
        return True
    return b"\x0f\x1f" in blob and all(b in _NOP_PARTS for b in blob)


def map_functions(game: Path, text: Segment, data: bytes | None = None) -> list[dict] | None:
    """The game's functions from a functions.jsonl beside it (offsets in the executable segment), else None.
    With `data`, entries that are only alignment padding are dropped: they are not functions and no C is them."""
    candidates = [game.parent / "functions.jsonl", game.parent / "functions" / "functions.jsonl",
                  game.with_suffix(".functions.jsonl"), *sorted(game.parent.glob("*/functions.jsonl")),
                  *sorted(game.parent.glob("*/*/functions.jsonl"))]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        out = []
        padding = 0
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                start = int(row["a"], 16)
                size = int(row["s"])
            except (ValueError, KeyError, TypeError):
                continue
            if size <= 0:
                continue
            if data is not None and is_padding(data[text.offset + start: text.offset + start + size]):
                padding += 1
                continue
            out.append({"entry": text.va + start, "end": text.va + start + size,
                        "name": row.get("n") or "", "source": "map", "imports": row.get("i") or []})
        if padding:
            log(f"the map lists {padding} entries that are only alignment padding: dropped, they are not functions")
        if out:
            return out
    return None


def _symbol_for(addr: int, names: dict[int, str]) -> str:
    return names.get(addr) or f"D_{addr:08X}"


def analyse(elf: ELF, found: list[dict]) -> list[Function]:
    """Every function's extent, name and relocations, from the map's (or Ghidra's) function list."""
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    text = elf.text
    rows: dict[int, dict] = {}
    for r in found:
        start = int(r["entry"])
        if r.get("thunk") or not elf.contains(start) or elf.offset(start) is None:
            continue
        rows[start] = r
    starts = sorted(rows)
    names = {s: f"func_{s:08X}" for s in starts}
    out = []
    for i, start in enumerate(starts):
        limit = min(starts[i + 1] if i + 1 < len(starts) else text.end, text.end)
        end = max(start + 1, min(int(rows[start].get("end") or limit), limit))
        code = elf.read(start, end - start)
        fn = Function(start, end - start, names[start])

        def symbol(addr: int, start: int = start, end: int = end) -> tuple[str, int]:
            if start <= addr < end:
                return "", addr - start  # inside this function: a label, not a relocation
            return _symbol_for(addr, names), 0

        insn_starts: set[int] = set()
        for ins in md.disasm(code, start):
            insn_starts.add(ins.address - start)
        for ins in md.disasm(code, start):
            at = ins.address - start
            ops = ins.operands
            if (x86.X86_GRP_CALL in ins.groups or x86.X86_GRP_JUMP in ins.groups) and ops \
                    and ops[0].type == x86.X86_OP_IMM:
                target = ops[0].imm
                if not start <= target < end and elf.contains(target):
                    name, _ = symbol(target)
                    # PC32, not PLT32: objdiff's x86 reader rejects PLT32 as an inline relocation
                    fn.relocs.append((at + ins.size - 4, R_X86_64_PC32, name, 0))
                continue
            for op in ops:
                if op.type == x86.X86_OP_MEM and op.mem.base == x86.X86_REG_RIP and ins.disp_size == 4:
                    target = ins.address + ins.size + op.mem.disp
                    if elf.contains(target):
                        name, addend = symbol(target)
                        fn.relocs.append((at + ins.disp_offset, R_X86_64_PC32, name, addend))
                elif op.type == x86.X86_OP_IMM:
                    # a 64-bit immediate (movabs) that names a function or lands in the data segment is an address:
                    # relocate it (its field is 2 bytes into the instruction, never at a boundary, which is what
                    # objdiff refuses). 32-bit immediates stay literal: their field can coincide with a boundary the
                    # way objdiff decodes a unit, and one such relocation fails the whole report.
                    seg = elf.segment_at(op.imm)
                    if ins.imm_size == 8 and op.imm >= 0x1000 and \
                            (op.imm in names or (seg is not None and not seg.flags & PF_X)):
                        name, addend = symbol(op.imm)
                        fn.relocs.append((at + ins.imm_offset, R_X86_64_64, name, addend))
        # a relocation at an instruction's first byte reads as inline data to objdiff's x86 reader (it refuses
        # those), and those offsets are the reader's, not a field: leave the bytes as they are
        fn.relocs = [r for r in fn.relocs if r[0] not in insn_starts]
        fn.relocs.sort()
        out.append(fn)
    return out



def prune_unit_relocs(elf: ELF, functions: list[Function]) -> None:
    """Drop relocations that land on an instruction's first byte, read as objdiff reads the unit: it disassembles a
    whole source file's section in one stream, so an offset that looks like a field inside one function can be the
    first byte of an instruction once the file's bytes are read from its start. objdiff refuses those ("unsupported
    inline x86 relocation"), and a field there would zero a byte the instruction needs."""
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    start = functions[0].addr
    end = max(fn.addr + fn.size for fn in functions)
    starts = {i.address - start for i in md.disasm(elf.read(start, end - start), 0)}
    for fn in functions:
        base = fn.addr - start
        fn.relocs = [r for r in fn.relocs if base + r[0] not in starts]


GAP_BYTES = 0x400  # a hole this big between functions means another region of code, not padding


def units(functions: list[Function]) -> dict[str, list[Function]]:
    """Source files: runs of neighbouring functions of about UNIT_BYTES each."""
    out: dict[str, list[Function]] = {}
    cur: list[Function] = []
    for fn in functions:
        if cur and (fn.addr + fn.size - cur[0].addr > UNIT_BYTES or fn.addr - (cur[-1].addr + cur[-1].size) > GAP_BYTES):
            out[f"text_{cur[0].addr:08X}"] = cur
            cur = []
        cur.append(fn)
    if cur:
        out[f"text_{cur[0].addr:08X}"] = cur
    return out


# ---------------------------------------------------------------------------------------------------- data


def _program(root: Path, manifest: dict) -> ELF:
    path = root / manifest["game"]["path"]
    return read_elf(path.read_bytes())


def data_bytes(root: Path, manifest: dict, addr: int, length: int) -> bytes | None:
    try:
        elf = _program(root, manifest)
    except Exception:  # no copy, or one that doesn't parse: the diff goes without values
        return None
    if not elf.contains(addr):
        return None
    data = elf.read(addr, length)
    return data if data and len(data) == length else None


def read_data(root: Path, manifest: dict, name: str, length: int = READ_DEFAULT_BYTES) -> str:
    """The bytes at a D_<address> of the user's copy, the addresses its words point to, and a string or floats."""
    m = re.fullmatch(r"_?D_([0-9A-Fa-f]{6,8})|(?:0x)?([0-9A-Fa-f]{6,8})", name)
    if m is None:
        raise ForkError(f"`{name}` isn't data by address: PS4 data is named D_<address> (D_9866D0)")
    addr = int(m.group(1) or m.group(2), 16)
    elf = _program(root, manifest)
    seg = elf.segment_at(addr)
    if seg is None:
        raise ForkError(f"0x{addr:08X} isn't in the game's program")
    n = max(READ_MIN_BYTES, min(int(length or READ_DEFAULT_BYTES), READ_MAX_BYTES))
    data = elf.read(addr, n)
    out = [f"D_{addr:08X} (+0x{addr - seg.va:X} of the segment at 0x{seg.va:08X}, {n} bytes)"]
    if not data:
        return out[0] + ": uninitialized data, zeros at start-up."
    for i in range(0, len(data), HEX_ROW_BYTES):
        chunk = data[i:i + HEX_ROW_BYTES]
        out.append(f"  +0x{i:03X}: {chunk.hex(' '):<47}  {''.join(chr(c) if c in PRINTABLE else '.' for c in chunk)}")
    words = [w for (w,) in struct.iter_unpack("<Q", data[:len(data) // 8 * 8])]
    ptrs = [(8 * i, w) for i, w in enumerate(words) if elf.contains(w)]
    if ptrs:
        out.append("pointers in it: " + ", ".join(f"+0x{at:X} -> 0x{w:08X}" for at, w in ptrs[:POINTERS_SHOWN]))
    floats = [struct.unpack("<f", struct.pack("<I", w & 0xFFFFFFFF))[0] for w in words[:FLOATS_SHOWN]]
    smallest, biggest = PLAUSIBLE_FLOAT
    if words and any(smallest < abs(x) < biggest for x in floats):
        out.append("as f32: " + ", ".join(f"{x:g}" for x in floats))
    text = data.split(b"\0", 1)[0]
    if len(text) >= MIN_STRING_CHARS and all(c in PRINTABLE or c in b"\t\n" for c in text):
        out.append(f"as a string: {json.dumps(text.decode('latin-1'))}")
    return "\n".join(out)


def rename(fp, version: str, old: str, new: str) -> dict:
    """Rename one of the game's functions in the project's config, where the delinker takes every target symbol from
    (`func_<addr>` -> a real name). The engine renames the C and checks that every match still matches. Returns the
    files changed, with the text they had (ForkEngine puts them back if the rename doesn't hold)."""
    path = Path(fp.root) / "config" / version / "functions.json"
    text = path.read_text(encoding="utf-8")
    cfg = json.loads(text)
    hit = False
    for functions in cfg.get("units", {}).values():
        for fn in functions:
            if fn.get("name") == old:
                fn["name"] = new
                hit = True
    if not hit:
        raise ForkError(f"{old} isn't a function in config/{version}/functions.json: only the game's functions "
                        f"(func_<address>) can be renamed for now, not its data")
    write_atomic(path, json.dumps(cfg, indent=1))
    return {path: text}


# ---------------------------------------------------------------------------------------------------- the project


def _run(cmd: list[str], root: Path, what: str, log) -> None:
    from ...forks.project import ForkProject

    ok, tail = ForkProject(root).run(cmd, log=log, timeout=TOOL_SECONDS)
    if not ok:
        raise ForkError(f"{what} failed:\n{tail[-ERROR_TAIL:]}")


def create(root: Path, game: Path, *, compiler: str | None = None, log=print) -> dict:
    """Make the project in `root` (which must not exist yet) and build it once. Returns facts about the game."""
    from ...forks import ghidra

    clang = clang_path()
    if clang is None:
        raise ForkError("PS4 projects build with clang 5.0.2, pinned by fingerprint: point DECOMP_PS4_CLANG at it, "
                        "then try again")
    info = game_info(game)
    version = info["id"]
    data = game.read_bytes()
    elf = read_elf(data)
    text = elf.text
    log(f"reading {game.name}: {len(elf.segments)} segments, code at 0x{text.va:X} ({text.filesz} bytes)")

    found = map_functions(game, text, data)
    if found:
        log(f"using the function map beside the game: {len(found)} functions")
    else:
        if not ghidra.available():
            raise ForkError("no functions.jsonl map beside the game and no Ghidra to find them: put one there "
                            "({\"a\": offset hex, \"s\": size, \"n\": name} per line) or install Ghidra in Setup")
        log(f"analysing {game.name} with Ghidra (finds every function; a big game takes a few minutes)")
        found = ghidra.functions(game)
        if not found:
            raise ForkError("neither a function map nor Ghidra found functions in this program")
    functions = analyse(elf, found)
    if not functions:
        raise ForkError("the function map gave no function in this program's code")
    groups = units(functions)
    for fns in groups.values():
        prune_unit_relocs(elf, fns)
    log(f"{len(functions)} functions in {len(groups)} source files")

    shutil.copytree(TEMPLATE, root)
    (root / "orig").mkdir()
    eboot = root / "orig" / f"{version}.elf"
    try:
        eboot.hardlink_to(game)
    except OSError:
        shutil.copyfile(game, eboot)
    (root / "include").mkdir()
    (root / "include" / "common.h").write_text(TYPES_H, encoding="utf-8")
    conf = root / "config" / version
    conf.mkdir(parents=True)
    (conf / "functions.json").write_text(json.dumps({
        "elf": f"orig/{version}.elf", "sha1": info["sha1"], "image_base": 0, "entry": elf.entry,
        "text": {"va": text.va, "offset": text.offset, "size": text.filesz},
        "units": {u: [{"name": f.name, "addr": f.addr, "offset": elf.offset(f.addr), "size": f.size,
                       "relocs": [list(r) for r in f.relocs]} for f in fns] for u, fns in groups.items()},
    }, indent=1), encoding="utf-8")
    for u, fns in groups.items():
        src = root / "src" / "auto" / f"{u}.c"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(f"/* {len(fns)} function{'s' if len(fns) != 1 else ''} from 0x{fns[0].addr:08X} to "
                       f"0x{fns[-1].addr + fns[-1].size:08X}, from the game's function map. */\n"
                       "#include \"common.h\"\n", encoding="utf-8")
    log(f"{len(groups)} source files for agents to write into")

    args = ["--clang", str(clang)]
    for cmd in ([sys.executable, "tools/configure.py", version, *args], ["ninja", "prepare"]):
        _run(cmd, root, "the first build", log)
    if not (root / "objdiff.json").exists():
        raise ForkError("the first build finished without writing objdiff.json")

    (root / ".decomp.wtf.json").write_text(json.dumps({
        "created": "from scratch", "platform": "ps4", "version": version, "compiler": "clang-5.0.2",
        "game": {"what": "your own copy of the game's eboot.elf", "path": f"orig/{version}.elf", "sha1": info["sha1"]},
        "setup": [["python", "tools/configure.py", version, *args], ["ninja", "prepare"]],
    }, indent=2), encoding="utf-8")
    (root / ".gitignore").write_text("# made from your copy of the game, or by the build\norig/\nbuild/\nbuild.ninja\n"
                                     ".ninja_*\nobjdiff.json\n__pycache__/\n.decomp-wtf/\n", encoding="utf-8")
    (root / "README.md").write_text(f"""# {info['title']} decompilation

Started from scratch with [decompdecomp](https://github.com/n-delic/decompdecomp): the game's functions are cut out of
your own `eboot.elf` into ELF64 objects and compared, function by function, with what `src/` compiles to, with objdiff.

1. Your copy of the game's program is here as `orig/{version}.elf` (never commit it).
2. `python tools/configure.py {version} --clang <clang 5.0.2>`, then `ninja`.
3. Decompiled code goes in `src/`; `objdiff.json` shows each function's progress.

Compiler: clang 5.0.2 with `-O2 -mavx -fno-omit-frame-pointer -fPIE -mllvm -x86-use-vzeroupper=0`, which is what the
game was built with as far as measurement can tell (14/20 hand-transcribed functions byte-identical, 18/20 the same
instruction set). `config/{version}/functions.json` holds every function's address, size, symbol and relocations.
""", encoding="utf-8")
    if shutil.which("git"):
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                    ["git", "-c", "user.name=decompdecomp", "-c", "user.email=noreply@decompdecomp.invalid", "commit",
                     "-q", "-m", f"Start the {info['title']} decompilation (decompdecomp, PS4)"]):
            subprocess.run(cmd, cwd=root, capture_output=True, creationflags=NO_WINDOW)
    return {"platform": "ps4", "id": version, "title": info["title"], "compiler": "clang-5.0.2",
            "sources": len(groups), "sha1": info["sha1"]}

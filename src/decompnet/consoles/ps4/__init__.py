"""PlayStation 4 (Orbis) games: x86-64 ELF programs, PROJECT mode, judged by objdiff.

- `elf64.py`: the game's program (an eboot, ET_SCE_EXEC) as program headers, addresses and bytes. The file has no
  section headers, so everything is read through PT_LOAD segments.
- `scratch.py`: a project for the user's eboot: the functions it holds (from a `functions.jsonl` map beside the game,
  else Ghidra), each function's bytes cut into ELF64 relocatable objects at build time (tools/delink.py), and src/
  compiled with the pinned clang.
- `project_template/`: what that project starts as (tools/configure.py writes its ninja build and objdiff.json).
- `units.py`: each unit's own compiler and flags, why, and docs/build-settings.md; `tls.py`: units whose code reads
  thread-locals as shared-object code does, built with -fPIC.
- `guides/ps4.md`: what agents read first — the compiler pin, the flags, and the three deltas that are known.

The compiler is pinned by measurement, not by the SDK's name: clang 5.0.2 with
`-O2 -mavx -fno-omit-frame-pointer -fPIE -mllvm -x86-use-vzeroupper=0` reproduced 14 of 20 hand-transcribed
functions of inFAMOUS Second Son byte for byte, and 18 of 20 as the same instruction set (see guides/ps4.md).
"""

from __future__ import annotations

import re
from pathlib import Path

from ...rom import RomError
from .. import BETA, PROJECT, Compiler, Console

# the EBoot's e_type: Sony's own ELF types, outside the standard ones
ET_SCE_EXEC, ET_SCE_DYNEXEC = 0xFE10, 0xFE04
EM_X86_64 = 62


class PS4(Console):
    def diagnostic_recipe(self, project, unit):
        from ...app.compiler_diagnostics import clang_recipe

        return clang_recipe(project, unit)

    def original_load_types(self, elf) -> tuple:
        # Only genuine original executables use the console's read-only mapping extension. TLS, dynamic and
        # metadata headers are not load segments, even when they describe virtual addresses.
        if elf['e_machine'] == 'EM_X86_64' and elf['e_type'] in (ET_SCE_EXEC, ET_SCE_DYNEXEC):
            return ('PT_LOAD', 0x61000010)  # PT_SCE_RELRO, also recognized by the original-image importer
        return super().original_load_types(elf)

    id = "ps4"
    name = "PlayStation 4"
    short = "PS4"
    aliases = ("orbis",)
    status = BETA
    arch = "x86_64"
    big_endian = False
    mode = PROJECT
    layout = "Ghidra + objdiff"
    compilers = (Compiler("clang-5.0.2", "Clang 5.0.2 (Orbis era)",
                          "inFAMOUS Second Son, by fingerprint"),
                 # upstream releases some units match with that 5.0.2 doesn't (units.py); each only where it's shown
                 Compiler("clang-3.8.1", "Clang 3.8.1 (upstream)", "some units of a PS4 game, by their functions"),
                 Compiler("clang-3.9.1", "Clang 3.9.1 (upstream)", "some units of a PS4 game, by their functions"))
    compiler_evidence = 2  # one function matching only with an older clang could be luck: two show the unit's
    packs = (("clang-5.0.2", "public compiler for PS4-era ELF projects (Linux x86-64)"),
             ("ghidra", "finds functions and provides machine drafts (needs Java 21+)"))
    guides = ("ps4",)
    compiler_guide = "ps4"
    m2c = ""  # m2c reads MIPS only
    ram = (0x0, 0x40000000)  # the image is linked at 0 and loaded wherever; words this low read as addresses
    demo = "demo-ps4"
    demo_tools = ("clang-5.0.2", "objdiff-cli")
    unit_settings_files = ("tools/compiler_units.json", "tools/compilers.json")  # units.UNITS, units.COMPILERS

    @property
    def import_error(self):
        from .scratch import host_error
        return host_error()

    def sniff(self, head: bytes, file) -> bool:
        """An x86-64 ELF, of one of Sony's executable types (an eboot.elf, not a library)."""
        return (len(head) >= 64 and head[:4] == b"\x7fELF" and head[4] == 2 and head[5] == 1
                and int.from_bytes(head[16:18], "little") in (ET_SCE_EXEC, ET_SCE_DYNEXEC)
                and int.from_bytes(head[18:20], "little") == EM_X86_64)

    def upload_name(self, data: bytes) -> str:
        return "eboot.elf"

    def create_project(self, root: Path, game: Path, *, compiler: str | None = None, log=print) -> dict:
        from . import scratch

        return scratch.create(root, game, compiler=compiler, log=log)

    def game_info(self, game: Path) -> dict:
        from . import scratch

        return scratch.game_info(game)

    def tool_args(self) -> list[str]:
        from . import scratch

        clang = scratch.clang_path()
        if clang is None:
            raise RomError("no clang 5.0.2 for PS4 matching: point DECOMP_PS4_CLANG at one, or install the pinned "
                           "build, then try again")
        return ["--clang", str(clang)]

    # each unit's compiler and flags, why, and the page that says so (units.py); for a project whose compile.py
    # reads them (decompdecomp's template since unit settings: on_start brings older projects' up to date)

    _tools: dict[str, tuple[tuple, bool]] = {}  # compile.py -> (its mtime and size, whether it reads unit settings)

    @classmethod
    def _per_unit(cls, fp) -> bool:
        """Whether the project's compile.py reads a compiler per unit: read once per change of the file (the overview
        asks it for each source file)."""
        tool = fp.root / "tools" / "compile.py"
        try:
            st = tool.stat()
        except OSError:
            return False
        stamp = (st.st_mtime_ns, st.st_size)
        hit = cls._tools.get(str(tool))
        if hit is None or hit[0] != stamp:
            hit = (stamp, "def unit_settings" in tool.read_text(encoding="utf-8", errors="replace"))
            cls._tools[str(tool)] = hit
        return hit[1]

    def file_compiler(self, fp, source: str) -> str | None:
        from . import units

        return units.file_compiler(fp.root, source) if self._per_unit(fp) else None

    def set_file_compiler(self, fp, source: str, compiler: str, why: str = "", evidence: list | None = None) -> str:
        from ...forks.project import ForkError
        from . import units

        if not self._per_unit(fp):
            raise ForkError("this project's build has no compiler per unit yet: Check again brings it up to date")
        if compiler not in {c.id for c in self.compilers}:
            raise ForkError(f"{compiler!r} isn't a PS4 compiler decompdecomp knows: {', '.join(c.id for c in self.compilers)}")
        units.write_compilers(fp.root)
        units.set_compiler(fp.root, source, compiler, why, evidence)
        return units.UNITS

    def file_settings(self, fp) -> dict[str, dict]:
        from . import units

        if not self._per_unit(fp):
            return {}
        out = {}
        for stem, s in units.read(fp.root).items():
            if s["compiler"] != units.DEFAULT or s["flags"] or s["why"]:
                out[f"src/auto/{stem}.c"] = s
        return out

    def settings_doc(self, fp) -> str | None:
        from . import units

        units.write_doc(fp.root)
        return units.DOC

    def build_unit_with(self, fp, unit, compiler: str | None, out: Path, text: str | None = None) -> Path | None:
        from . import units

        return (units.build_with(fp.root, unit.source, compiler, out, text) if self._per_unit(fp) and unit.source
                else None)

    def on_start(self, net, slug: str, log=print) -> None:
        """A project made before units had compilers of their own gets the build tool that reads them, kept only if
        every function that matched still does (units.upgrade); every project gets its compilers' paths and its page
        of build settings written again; and units whose code reads thread-locals as shared-object code does are built
        that way (tls.give_pic)."""
        from . import tls, units

        if net.fork_slug(slug):
            units.upgrade(net, slug, log=log)
            given = tls.give_pic(net, slug, log=log)
            if given:
                log(f"PS4 TLS: {len(given)} units are built with -fPIC, as their thread-local access shows")

    def adapt_draft(self, text: str) -> str:
        """Ghidra's C in this project's type names. It writes `undefined8` and `uint` where common.h has `u64` and
        `u32`, and an agent handed those spends a turn translating instead of matching; its warnings go too. The
        project's own tools/drafts.py found these spellings enough for 249 drafts to match as they are."""
        for theirs, ours in (("undefined8", "u64"), ("undefined4", "u32"), ("undefined2", "u16"),
                             ("undefined1", "u8"), ("undefined", "u8"), ("byte", "u8"), ("ushort", "u16"),
                             ("uint", "u32"), ("ulong", "u64")):
            text = re.sub(rf"\b{theirs}\b", ours, text)
        text = re.sub(r"\bcode \*", "void *", text)
        return re.sub(r"/\* WARNING: .*?\*/\s*", "", text, flags=re.S).strip()

    def rename_symbol(self, fp, version: str, old: str, new: str) -> dict:
        """A function's real name, in the config the delinker names the target object's symbols from."""
        from . import scratch

        return scratch.rename(fp, version, old, new)

    splits_sources = True

    def split_source(self, fp, version: str, source: str, at: str, new: str) -> dict:
        from . import scratch

        return scratch.split_source(fp, version, source, at, new)

    def function_addresses(self, fp) -> dict[tuple[str, str], int]:
        """Keep standard relocatable targets; original addresses live in the import config (#119)."""
        import json

        version = (fp.manifest or {}).get("version")
        if not version:
            return {}
        path = fp.root / "config" / version / "functions.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return {(unit, fn["name"]): fn["addr"] for unit, functions in cfg["units"].items() for fn in functions}

    def read_data(self, fp, name: str, length: int) -> str | None:
        from . import scratch

        return scratch.read_data(fp.root, fp.manifest or {}, name, length)

    def data_bytes(self, fp, addr: int, length: int) -> bytes | None:
        from . import scratch

        return scratch.data_bytes(fp.root, fp.manifest or {}, addr, length)


PS4_CONSOLE = PS4()

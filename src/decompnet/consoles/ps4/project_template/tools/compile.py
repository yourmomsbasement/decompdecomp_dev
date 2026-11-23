"""Compile one PS4 unit with the pinned clang, without a shell parsing paths or flags.

A unit can have settings of its own in tools/compiler_units.json, each with its reason (docs/build-settings.md lists
every one, decompdecomp writes it):

    "text_00123450": ["-march=btver2"]                    flags only (the form from before reasons were kept)
    "text_00123450": {"compiler": "clang-3.8.1", "flags": ["-march=btver2"],
                      "why": "...", "evidence": [{"function": "...", ...}]}

A compiler other than the build's own is looked up in tools/compilers.json, which decompdecomp writes: each pinned
compiler's path and the flags it needs before the build's (an older clang: the PS4 target, which reads the stack guard
from memory as the game does, and the system's headers that target doesn't search).
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

TOOL_VERSION = 2  # decompdecomp brings a project's copy up to date when the template's is newer (consoles/ps4/units.py)
COMPILE_SECONDS = 300
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HERE = Path(__file__).resolve().parent


def unit_settings(stem: str) -> tuple[str | None, list[str]]:
    """(the unit's own compiler or None, its own flags) from compiler_units.json."""
    path = HERE / "compiler_units.json"
    entry = json.loads(path.read_text()).get(stem, []) if path.exists() else []
    compiler, flags = (entry.get("compiler"), entry.get("flags", [])) if isinstance(entry, dict) else (None, entry)
    if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
        raise ValueError(f"{stem}'s flags in compiler_units.json must be a list of strings")
    return compiler, list(flags)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compiler", required=True)
    parser.add_argument("--include", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source", required=True)
    # another compiler than the unit's own, for decompdecomp's survey (consoles/ps4/units.py): the same build otherwise
    parser.add_argument("--unit-compiler", default="")
    parser.add_argument("flags", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    flags = args.flags[1:] if args.flags[:1] == ["--"] else args.flags
    clang = args.compiler
    own, extra = unit_settings(Path(args.source).stem)
    own = args.unit_compiler or own
    if own:
        known = json.loads((HERE / "compilers.json").read_text()) if (HERE / "compilers.json").exists() else {}
        spec = known.get(own)
        if spec is None:
            print(f"{args.source} is built with {own}, which tools/compilers.json doesn't name: install it in "
                  "decompdecomp (Compilers & tools), which writes that file", file=sys.stderr)
            return 2
        if spec.get("path"):  # (none: the build's own compiler, named in compiler_units.json for its reason)
            clang = spec["path"]
            flags = [*spec.get("flags", []), *flags]
            # the PS4 target's own CPU is btver2: a unit that doesn't ask for one keeps the build's (x86-64)
            if spec.get("default_march") and not any(f.startswith("-march=") for f in [*flags, *extra]):
                extra.append(f"-march={spec['default_march']}")
    # -fPIE and -fPIC together stay PIE (clang keeps the PIE codegen), so a unit that asks for -fPIC has to lose the
    # base -fPIE: without this, the flag changed nothing and the TLS access stayed GOTTPOFF instead of TLSGD.
    if "-fPIC" in extra:
        flags = [f for f in flags if f != "-fPIE"]
    flags = [*flags, *extra]
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    command = [clang, *flags, f"-I{args.include}", "-I.", "-c", args.source, "-o", args.output]
    code = subprocess.run(command, timeout=COMPILE_SECONDS, creationflags=NO_WINDOW).returncode
    if code == 0:  # the stack guard's GOT slot is at a fixed address in the game: bind it, as its linker did
        subprocess.run([sys.executable, str(HERE / "bind.py"), args.output, "--quiet"],
                       timeout=COMPILE_SECONDS, creationflags=NO_WINDOW)
        if (HERE / "jump_tables.py").exists():  # a switch's table, where it's the game's own: named as the game names it
            subprocess.run([sys.executable, str(HERE / "jump_tables.py"), args.output, "--config", args.config],
                           timeout=COMPILE_SECONDS, creationflags=NO_WINDOW)
    return code


if __name__ == "__main__":
    sys.exit(main())

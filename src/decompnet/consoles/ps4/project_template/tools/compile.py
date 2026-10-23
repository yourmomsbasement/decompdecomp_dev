"""Compile one PS4 unit with the pinned clang, without a shell parsing paths or flags."""

import argparse
import subprocess
import sys
from pathlib import Path

COMPILE_SECONDS = 300
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compiler", required=True)
    parser.add_argument("--include", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("flags", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    flags = args.flags[1:] if args.flags[:1] == ["--"] else args.flags
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    command = [args.compiler, *flags, f"-I{args.include}", "-I.", "-c", args.source, "-o", args.output]
    code = subprocess.run(command, timeout=COMPILE_SECONDS, creationflags=NO_WINDOW).returncode
    if code == 0:  # the stack guard's GOT slot is at a fixed address in the game: bind it, as its linker did
        subprocess.run([sys.executable, str(Path(__file__).with_name("bind.py")), args.output, "--quiet"],
                       timeout=COMPILE_SECONDS, creationflags=NO_WINDOW)
    return code


if __name__ == "__main__":
    sys.exit(main())

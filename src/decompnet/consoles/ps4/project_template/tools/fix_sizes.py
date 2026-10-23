"""Repair a function size that stops inside its last instruction.

The function map's sizes are heuristic — the game's ELF carries no symbol table — and for some functions a size
lands a byte or two short of the end of the final instruction. The delinked target object then ends mid-encoding,
objdiff shows a bad row at the tail, and no C can ever match that function.

This walks each function from its own address to the next function's, and where the declared size cuts the last
instruction it raises the size to that instruction's end. It never shrinks a size and never crosses into the next
function.

Usage: fix_sizes.py [config/eboot/functions.json] [--dry-run]
"""

import argparse
import json
import os
import sys
from pathlib import Path

TEXT = Path("/home/Trist/Desktop/REA Infamous Second Son/modding/functions/text.bin")


def walk(md, blob: bytes, start: int, limit: int) -> int:
    """The end of the last whole instruction in [start, limit)."""
    end = start
    for insn in md.disasm(blob[start:limit], start):
        if insn.address + insn.size > limit:
            break
        end = insn.address + insn.size
    return end


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", default="config/eboot/functions.json")
    parser.add_argument("--text", default=str(TEXT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        import capstone
    except ImportError:  # decompdecomp's own python has it; a bare system python may not
        print("needs capstone: python -m pip install capstone", file=sys.stderr)
        return 2
    path = Path(args.config)
    cfg = json.loads(path.read_text())
    blob = Path(args.text).read_bytes()
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    starts = sorted(fn["addr"] for fns in cfg["units"].values() for fn in fns)
    upper = dict(zip(starts, starts[1:] + [starts[-1] + 16]))
    grown = []
    for fns in cfg["units"].values():
        for fn in fns:
            start, size = fn["addr"], fn["size"]
            limit = min(upper[start], start + 0x10000)
            # only the instruction the size lands inside: a truncated tail is 1-15 bytes, a bigger extension means
            # the map is wrong about something else, and swallowing the next region would compare the wrong bytes
            end = size
            for insn in md.disasm(blob[start:limit], start):
                if insn.address + insn.size <= start + size:
                    continue
                if insn.address < start + size and insn.size <= 15:  # starts inside, ends past: a cut tail
                    end = insn.address + insn.size - start
                break
            if end > size:
                fn["size"] = end
                grown.append((fn["name"], size, fn["size"]))
    print(f"functions whose size cut an instruction: {len(grown)}")
    for name, before, after in grown[:10]:
        print(f"  {name}: {before} -> {after}")
    if grown and not args.dry_run:
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(cfg, indent=1))
        os.replace(tmp, path)
        print(f"written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

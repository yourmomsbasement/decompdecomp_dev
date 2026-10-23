"""Deterministic recipes: rewrite the C shapes we have measured, and keep only what the judge confirms.

No model, no tokens. Each recipe is a rule whose trigger is visible in the target's own assembly or in the
declaration the code uses, and whose rewrite is the spelling an agent found by hand:

  float_ne    target compares with ucomiss/vucomiss and takes the flag straight into a branch or setne, while our C
              writes `a != b` (fcmp UNE, which clang expands to setp+or)  ->  __builtin_islessgreater(a, b)
  open_hidden the target takes a data symbol's address with `lea reg,[rip+D]` while this unit declares it hidden
              (which folds the pair into one operand)                    ->  drop the attribute on that symbol

Every variant is compiled and judged; only a strictly better score is written back. The judge is the same one the
agents face, so a rewrite that looks right and scores worse costs nothing.

Usage: recipes.py <function> [--apply] [--project DIR]
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

CLI = "/home/Trist/.local/share/decompnet/toolchains/objdiff-cli/objdiff-cli"
SHDR_RE = re.compile(r"\s*\[\s*\d+\]\s+(\S+)\s+(\S+)")
FLOAT_NE = re.compile(r"\b([A-Za-z_][\w.\[\]\->]*)\s*!=\s*([A-Za-z_][\w.\[\]\->]*)")
DECL_HIDDEN = re.compile(r'(extern[^;]*?\b(D_[0-9A-Fa-f]+)\b[^;]*?)\s*__attribute__\(\(visibility\("hidden"\)\)\)')
DEF = r"^(?:static\s+|inline\s+)*[\w \t\*]+?\b{name}\s*\([^;{{]*\)\s*\{{"


def function_text(source: str, name: str) -> tuple[int, int] | None:
    """The span of the function's definition in the unit's source, braces balanced."""
    m = re.search(DEF.format(name=re.escape(name)), source, re.M)
    if m is None:
        return None
    depth, i = 0, m.end() - 1
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return m.start(), i + 1
        i += 1
    return None


def target_asm(project: Path, unit: str, symbol: str) -> str:
    obj = project / f"build/eboot/target/{unit}.o"
    if not obj.exists():
        return ""
    out = subprocess.run(["objdump", "-d", "--no-show-raw-insn", str(obj)], capture_output=True, text=True).stdout
    i = out.find(f"<{symbol}>:")
    if i < 0:
        return ""
    j = out.find("\n\n", i)
    return out[i:j if j > 0 else len(out)]


def addressed_symbols(project: Path, unit: str) -> set[str]:
    """D_ symbols this unit's target takes the address of with a lea (rather than folding into the operand)."""
    obj = project / f"build/eboot/target/{unit}.o"
    if not obj.exists():
        return set()
    out = subprocess.run(["objdump", "-dr", "--no-show-raw-insn", str(obj)], capture_output=True, text=True).stdout
    found = set()
    lines = out.splitlines()
    for n, line in enumerate(lines):
        m = re.search(r"\bR_X86_64_PC32\s+(D_[0-9A-Fa-f]+)-0x4", line)
        if m is None or n == 0:
            continue
        prev = lines[n - 1]
        if re.search(r"\blea\s+", prev):
            found.add(m.group(1))
    return found


def variants(project: Path, unit: str, name: str, source: str) -> list[tuple[str, str, str]]:
    """(label, whole-file text, note) candidates for this function."""
    out: list[tuple[str, str, str]] = []
    asm = target_asm(project, unit, name)
    span = function_text(source, name)
    body = source[span[0]:span[1]] if span else ""
    if span is not None and "ucomiss" in asm and "!=" in body:
        # one variant per `!=` in the function: which comparison is the float one is a question the judge answers
        # (a pointer `!=` written as the builtin does not even compile), and a compile costs 40 ms
        for n, m in enumerate(FLOAT_NE.finditer(body)):
            rewritten = body[:m.start()] + f"__builtin_islessgreater({m.group(1)}, {m.group(2)})" + body[m.end():]
            out.append((f"float_ne_{n}", source[:span[0]] + rewritten + source[span[1]:],
                        f"'!=' #{n} ({m.group(0).strip()}) -> islessgreater"))
    addressed = addressed_symbols(project, unit)
    if addressed:
        def open_decl(m):
            return m.group(1) if m.group(2) in addressed else m.group(0)
        opened = DECL_HIDDEN.sub(open_decl, source)
        if opened != source:
            out.append(("open_hidden", opened, "hidden -> default on " + ", ".join(sorted(addressed)[:3])))
    return out


def judge(project: Path, unit: str, name: str, text: str, workdir: Path, label: str) -> float | None:
    """Compile this whole-file variant and score the function, as the agents' judge would."""
    src = workdir / f"{unit}.{label}.c"
    src.write_text(text)
    obj = workdir / f"{unit}.{label}.o"
    cfg = project / "config/eboot/functions.json"
    r = subprocess.run([sys.executable, "tools/compile.py", "--compiler",
                        "/opt/llvm-5.0.2-globalguard/bin/clang", "--include", "include", "--config", str(cfg),
                        "--output", str(obj), "--source", str(src), "--", "-O2", "-mavx", "-fno-omit-frame-pointer",
                        "-fPIE", "-mllvm", "-x86-use-vzeroupper=0", "-fstack-protector-strong",
                        "-fno-asynchronous-unwind-tables", "-ffunction-sections", "-mllvm",
                        "-no-x86-call-frame-opt", "-mbmi", "-mllvm", "-unroll-runtime=false", "-w",
                        f"-I{project / 'include'}", f"-I{project}"], cwd=project, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    probe = workdir / f"p-{label}"
    probe.mkdir(exist_ok=True)
    (probe / "objdiff.json").write_text(json.dumps({"units": [{
        "name": unit, "target_path": str((project / f"build/eboot/target/{unit}.o").resolve()),
        "base_path": str(obj.resolve())}]}))
    subprocess.run([CLI, "--no-color", "report", "generate", "-p", str(probe), "-o", str(probe / "r.json")],
                   cwd=project, capture_output=True, text=True)
    try:
        rep = json.loads((probe / "r.json").read_text())
    except (OSError, ValueError):
        return None
    for unit_rep in rep.get("units", []):
        for f in unit_rep.get("functions", []):
            if f["name"] == name:
                return f.get("fuzzy_match_percent")
    return None


def apply(project: Path, unit: str, name: str, dry_run: bool = True) -> dict:
    """Try every recipe on one function; write the best one back when it beats the current build."""
    src_path = project / f"src/auto/{unit}.c"
    source = src_path.read_text(errors="replace")
    current = judge(project, unit, name, source, Path(tempfile.mkdtemp(prefix="recipe-")), "current")
    tried, best, best_label = [], current, None
    for label, text, note in variants(project, unit, name, source):
        work = Path(tempfile.mkdtemp(prefix=f"recipe-{label}-"))
        score = judge(project, unit, name, text, work, label)
        tried.append({"recipe": label, "note": note, "score": score})
        if score is not None and (best is None or score > best):
            best, best_label = score, label
    if best_label is not None and not dry_run:
        for label, text, _note in variants(project, unit, name, source):
            if label == best_label:
                src_path.write_text(text)
                break
    return {"function": name, "unit": unit, "current": current, "tried": tried, "best": best,
            "applied": best_label if (best_label and not dry_run) else None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("function")
    parser.add_argument("--project", default="/home/Trist/Decomp-dev/rea-infamous-second-son-eboot")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    project = Path(args.project)
    cfg = json.loads((project / "config/eboot/functions.json").read_text())
    for unit, fns in cfg["units"].items():
        for fn in fns:
            if fn["name"] == args.function:
                print(json.dumps(apply(project, unit, args.function, dry_run=not args.apply), indent=1))
                return 0
    print(f"{args.function} is not in this project's config")
    return 1


if __name__ == "__main__":
    sys.exit(main())

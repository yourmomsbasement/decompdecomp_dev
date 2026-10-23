"""Deterministic recipes: rewrite the C shapes we have measured, and keep only what the judge confirms.

No model, no tokens. Each recipe is a rule whose trigger is visible in the target's own code, and whose rewrite is
the spelling agents found by hand:

  float_ne     target compares with ucomiss while our C writes `a != b` (fcmp UNE -> setp+or): __builtin_islessgreater
  cmp_order    a relational test whose operand order the target keeps (cmp index,[count]; jae, not cmp [count],index),
               written as an overflow check on the subtraction, which costs no instruction of its own
  open_hidden  the target takes a data symbol's address with a lea while the unit declares it hidden

Three things keep the judging cheap: results are cached against the source, the target object and the compiler; a
batch stops the moment a variant reaches 100%; and a re-apply writes the winner it already knows instead of
re-deriving the batch. Each result also classifies what is still different, so the next experiment can be chosen
instead of guessed.

Usage: recipes.py <function> [--apply] [--delta] [--project DIR]
"""
import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLI = "/home/Trist/.local/share/decompnet/toolchains/objdiff-cli/objdiff-cli"
COMPILER = "/opt/llvm-5.0.2-globalguard/bin/clang"
FLOAT_NE = re.compile(r"\b([A-Za-z_][\w.\[\]\->]*)\s*!=\s*([A-Za-z_][\w.\[\]\->]*)")
DECL_HIDDEN = re.compile(r'(extern[^;]*?\b(D_[0-9A-Fa-f]+)\b[^;]*?)\s*__attribute__\(\(visibility\("hidden"\)\)\)')
COND_IF = re.compile(r"\bif\s*\(")
REL = ("<=", ">=", "<", ">")
REG = re.compile(r"\b(r[a-z0-9]{1,3}|e[a-z]{2}|[a-d][lhx]|[re]?[sd]i|[re]?[sb]p|xmm\d+|ymm\d+)\b")
DEF = r"^(?:static\s+|inline\s+)*[\w \t\*]+?\b{name}\s*\([^;{{]*\)\s*\{{"
CACHE_VERSION = 1


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
    found, lines = set(), out.splitlines()
    for n, line in enumerate(lines):
        m = re.search(r"\bR_X86_64_PC32\s+(D_[0-9A-Fa-f]+)-0x4", line)
        if m and n and re.search(r"\blea\s+", lines[n - 1]):
            found.add(m.group(1))
    return found


def conditions(body: str):
    """(start, end, condition text) for every `if (...)` in a function body, parentheses balanced."""
    out = []
    for m in COND_IF.finditer(body):
        depth, i = 1, m.end()
        while i < len(body) and depth:
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
            i += 1
        out.append((m.end(), i - 1, body[m.end():i - 1]))
    return out


def comparisons(cond: str):
    """(index, left, op, right) for the top-level comparison in a condition; nested ones are left alone."""
    depth = 0
    for i in range(len(cond)):
        c = cond[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0:
            for op in REL:
                if cond.startswith(op, i):
                    left, right = cond[:i].strip(), cond[i + len(op):].strip()
                    if left and right:
                        return [(i, left, op, right)]
    return []


def overflow_form(left: str, op: str, right: str, signed: bool) -> str | None:
    """The same test written as an overflow check, which keeps left before right in the subtraction."""
    fn = "__builtin_ssub_overflow" if signed else "__builtin_usub_overflow"
    return {"<": f"{fn}({left}, {right}, &__unused_ov)",
            ">=": f"!{fn}({left}, {right}, &__unused_ov)",
            ">": f"{fn}({right}, {left}, &__unused_ov)",
            "<=": f"!{fn}({right}, {left}, &__unused_ov)"}.get(op)


def _declare_temp(body: str, name: str) -> str:
    """Insert the `unsigned __unused_ov;` the overflow builtins need, right after the function's opening brace."""
    if "__unused_ov" not in body:
        return body
    m = re.search(DEF.format(name=re.escape(name)), body)
    return body if m is None else body[:m.end()] + " unsigned __unused_ov;" + body[m.end():]


def variants(project: Path, unit: str, name: str, source: str) -> list[tuple[str, str, str]]:
    """(label, whole-file text, note) candidates for this function."""
    out: list[tuple[str, str, str]] = []
    asm = target_asm(project, unit, name)
    span = function_text(source, name)
    body = source[span[0]:span[1]] if span else ""
    if span is not None and "ucomiss" in asm and "!=" in body:
        # one variant per `!=`: which comparison is the float one is a question the judge answers, and a compile is
        # 40 ms (a pointer `!=` written as the builtin does not even compile)
        for n, m in enumerate(FLOAT_NE.finditer(body)):
            rewritten = body[:m.start()] + f"__builtin_islessgreater({m.group(1)}, {m.group(2)})" + body[m.end():]
            out.append((f"float_ne_{n}", source[:span[0]] + rewritten + source[span[1]:],
                        f"'!=' #{n} ({m.group(0).strip()}) -> islessgreater"))
    if span is not None and body:
        for ci, (cstart, cend, cond) in enumerate(conditions(body)):
            for _i, left, op, right in comparisons(cond):
                for signed in (False, True):
                    form = overflow_form(left, op, right, signed)
                    if form is None:
                        continue
                    new_body = _declare_temp(body[:cstart] + form + body[cend:], name)
                    out.append((f"cmp_{ci}_{'s' if signed else 'u'}",
                                source[:span[0]] + new_body + source[span[1]:],
                                f"'{left} {op} {right}' -> {'signed' if signed else 'unsigned'} overflow form"))
    addressed = addressed_symbols(project, unit)
    if addressed:
        opened = DECL_HIDDEN.sub(lambda m: m.group(1) if m.group(2) in addressed else m.group(0), source)
        if opened != source:
            out.append(("open_hidden", opened, "hidden -> default on " + ", ".join(sorted(addressed)[:3])))
    return out


def _compiler_stamp() -> str:
    st = os.stat(COMPILER)
    return f"{COMPILER}:{st.st_size}:{int(st.st_mtime)}"


def cache_path(project: Path) -> Path:
    return project / "build/recipes-cache.json"


def load_cache(project: Path) -> dict:
    try:
        data = json.loads(cache_path(project).read_text())
    except (OSError, ValueError):
        return {}
    return data if data.get("version") == CACHE_VERSION else {}


def save_cache(project: Path, cache: dict) -> None:
    cache["version"] = CACHE_VERSION
    tmp = cache_path(project).with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cache))
    os.replace(tmp, cache_path(project))


def _sha(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8", "replace"))
    return h.hexdigest()


def judge(project: Path, unit: str, name: str, text: str, workdir: Path, label: str,
          stats: dict, cache: dict | None = None, cache_key: str | None = None) -> float | None:
    """Compile this whole-file variant and score the function, as the agents' judge would."""
    if cache is not None and cache_key is not None:
        cached = cache.get(cache_key, {}).get(label)
        if cached is not None:
            stats["cache_hits"] += 1
            return cached
    src = workdir / f"{unit}.{label}.c"
    src.write_text(text)
    obj = workdir / f"{unit}.{label}.o"
    cfg = project / "config/eboot/functions.json"
    stats["compiles"] += 1
    r = subprocess.run([sys.executable, "tools/compile.py", "--compiler", COMPILER, "--include", "include",
                        "--config", str(cfg), "--output", str(obj), "--source", str(src), "--",
                        "-O2", "-mavx", "-fno-omit-frame-pointer", "-fPIE", "-mllvm", "-x86-use-vzeroupper=0",
                        "-fstack-protector-strong", "-fno-asynchronous-unwind-tables", "-ffunction-sections",
                        "-mllvm", "-no-x86-call-frame-opt", "-mbmi", "-mllvm", "-unroll-runtime=false", "-w",
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
                score = f.get("fuzzy_match_percent")
                if cache is not None and cache_key is not None and score is not None:
                    cache.setdefault(cache_key, {})[label] = score
                    stats.setdefault("objects", {})[label] = str(obj)
                return score
    return None


def _disassembly(obj: Path, name: str) -> list[str]:
    """The formatted instructions of one symbol in an object file."""
    if not obj.exists():
        return []
    out = subprocess.run(["objdump", "-d", "--no-show-raw-insn", str(obj)], capture_output=True, text=True).stdout
    i = out.find(f"<{name}>:")
    if i < 0:
        return []
    j = out.find("\n\n", i)
    lines = []
    for ln in out[i:j if j > 0 else len(out)].splitlines()[1:]:
        if "\t" in ln:
            lines.append(ln.split("\t", 1)[1].strip())
    return lines


def classify(project: Path, unit: str, name: str, base_obj: Path | None = None) -> dict:
    """What kind of difference is left: ordering, registers, constants, references, stack, control flow.

    `base_obj` is the object to compare against the target - the winner's, when a recipe won, so the report
    describes what the next experiment faces rather than what the last build left behind."""
    target = _disassembly(project / f"build/eboot/target/{unit}.o", name)
    base = _disassembly(base_obj or (project / f"build/eboot/src/auto/{unit}.o"), name)
    counts: dict[str, int] = {}
    if not target or not base:
        return counts
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, target, base).get_opcodes():
        if tag == "equal":
            continue
        tl, bl = target[i1:i2], base[j1:j2]
        if sorted(tl) == sorted(bl):
            counts["ordering"] = counts.get("ordering", 0) + max(len(tl), len(bl))
            continue
        if len(tl) == len(bl):
            for t, b in zip(tl, bl):
                tm, bm = t.split(" ", 1)[0], b.split(" ", 1)[0]
                if tm != bm:
                    key = "control_flow" if (tm.startswith("j") or tm == "call") else "instruction_form"
                elif re.search(r"D_[0-9A-Fa-f]+|\b(lea|call|jmp)\b", t) and re.sub(r"0x[0-9a-f]+", "X", t) != re.sub(r"0x[0-9a-f]+", "X", b):
                    key = "references"
                elif re.sub(REG, "R", t) == re.sub(REG, "R", b):
                    key = "constants"
                elif re.sub(r"0x[0-9a-f]+", "X", t) == re.sub(r"0x[0-9a-f]+", "X", b):
                    key = "registers"
                elif re.search(r"\b(rsp|rbp|esp|ebp)\b", t) and "rsp" in (t + b) + "rsp":
                    key = "stack"
                else:
                    key = "instruction_form"
                counts[key] = counts.get(key, 0) + 1
            continue
        joined = " ".join(tl + bl)
        if re.search(r"\b(sub|add)\s+(rsp|esp)\b|\[rbp[-+]0x", joined):
            key = "stack"
        elif any(ln.split(" ")[0].startswith("j") for ln in tl + bl if ln):
            key = "control_flow"
        else:
            key = "extra"
        counts[key] = counts.get(key, 0) + max(len(tl), len(bl))
    return counts


def apply(project: Path, unit: str, name: str, dry_run: bool = True) -> dict:
    """Try every recipe on one function; write the best one back when it beats the current build."""
    started = time.time()
    src_path = project / f"src/auto/{unit}.c"
    source = src_path.read_text(errors="replace")
    target_obj = project / f"build/eboot/target/{unit}.o"
    cache = load_cache(project)
    cache_key = _sha(source, _sha(target_obj.read_bytes().hex() if target_obj.exists() else ""),
                     _compiler_stamp(), CLI)
    stats = {"compiles": 0, "cache_hits": 0}
    work = Path(tempfile.mkdtemp(prefix="recipe-"))
    current = judge(project, unit, name, source, work, "current", stats, cache, cache_key)
    tried, best, best_label, best_text, best_obj = [], current, None, None, None
    for label, text, note in variants(project, unit, name, source):
        score = judge(project, unit, name, text, work, label, stats, cache, cache_key)
        tried.append({"recipe": label, "note": note, "score": score})
        if score is not None and (best is None or score > best):
            best, best_label, best_text = score, label, text
            best_obj = Path(stats.get("objects", {}).get(label, "")) if stats.get("objects", {}).get(label) else None
        if score == 0.0:  # an exact match ends the batch; nothing can beat it
            break
    applied = None
    if best_label is not None and best_text is not None and not dry_run:
        if src_path.read_text(errors="replace") == source:  # the file did not move under us
            src_path.write_text(best_text)
            applied = best_label
    save_cache(project, cache)
    delta = classify(project, unit, name, best_obj if best_label else None)
    stats.pop("objects", None)
    return {"function": name, "unit": unit, "current": current, "tried": tried, "best": best,
            "delta": delta, "applied": applied, "stats": stats,
            "seconds": round(time.time() - started, 1)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("function")
    parser.add_argument("--project", default="/home/Trist/Decomp-dev/rea-infamous-second-son-eboot")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delta", action="store_true", help="also classify what is still different")
    args = parser.parse_args()
    project = Path(args.project)
    cfg = json.loads((project / "config/eboot/functions.json").read_text())
    for unit, fns in cfg["units"].items():
        for fn in fns:
            if fn["name"] == args.function:
                result = apply(project, unit, args.function, dry_run=not args.apply)
                if not args.delta:
                    result.pop("delta", None)
                print(json.dumps(result, indent=1))
                return 0
    print(f"{args.function} is not in this project's config")
    return 1


if __name__ == "__main__":
    sys.exit(main())

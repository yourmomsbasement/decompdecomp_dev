"""Polish the matched corpus: give the symbols that are not really functions (and the ones that only wrap a named
import) names that say what they are, then prove every match still matches.

Two passes, both mechanical and both verified by the project's own build and objdiff report:

  padding   a "function" whose body is empty because the target is alignment bytes: named pad_<address>, so nobody
            spends a compile or an agent turn on it again
  imports   a function whose only import is one named library call (not the stack guard): named <Api>_call

Nothing is kept unless the rebuild still matches every function it matched before: the config and every source are
restored from a backup otherwise.

Usage: polish.py [--project DIR] [--apply] [--limit N]
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/Trist/Desktop/REA Infamous Second Son")
CLANG = "/opt/llvm-5.0.2-globalguard/bin/clang"
CLI = "/home/Trist/.local/share/decompnet/toolchains/objdiff-cli/objdiff-cli"
NOISE = {"__stack_chk_fail", "__stack_chk_guard"}


def matched_now(project: Path) -> dict[str, float]:
    """function -> percent, from the project's own report (the number everything else is judged by)."""
    out = project / "build/polish-report.json"
    subprocess.run([CLI, "--no-color", "report", "generate", "-p", str(project), "-o", str(out)],
                   cwd=project, capture_output=True, text=True)
    rep = json.loads(out.read_text())
    return {f["name"]: (f.get("fuzzy_match_percent") or 0)
            for u in rep.get("units", []) for f in u.get("functions", [])}


def rebuild(project: Path) -> bool:
    """The project's own setup: configure, then ninja."""
    ok = subprocess.run([sys.executable, "tools/configure.py", "eboot", "--clang", CLANG],
                        cwd=project, capture_output=True, text=True)
    if ok.returncode != 0:
        print(ok.stderr[-1500:])
        return False
    built = subprocess.run(["ninja"], cwd=project, capture_output=True, text=True)  # build.ninja sits at the root
    if built.returncode != 0:
        print((built.stdout or built.stderr)[-1500:])
        return False
    return True


def restore(project: Path, backup: Path) -> None:
    """Put the config and every source back as they were, and rebuild what that makes."""
    shutil.copy2(backup / "functions.json", project / "config/eboot/functions.json")
    for folder in ("src", "include"):
        if (backup / folder).is_dir():
            shutil.rmtree(project / folder, ignore_errors=True)
            shutil.copytree(backup / folder, project / folder)
    # copy2 keeps the backup's mtimes, and ninja then leaves objects built from the *renamed* sources in place:
    # an object newer than its source is up to date, so every match looks lost
    for folder in ("src", "include"):
        if (project / folder).is_dir():
            subprocess.run(["find", str(project / folder), "-type", "f", "-exec", "touch", "{}", "+"], check=False)
    rebuild(project)


def definitions(project: Path, cfg: dict) -> dict[str, tuple[str, str, str]]:
    """name -> (unit, definition text, body)."""
    out = {}
    for unit in cfg["units"]:
        path = project / f"src/auto/{unit}.c"
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        for fn in cfg["units"][unit]:
            m = re.search(rf"^[^\n;]*\b{re.escape(fn['name'])}\s*\([^;]*?\)\s*$|"
                          rf"^[^\n;]*\b{re.escape(fn['name'])}\s*\([^;]*?\)\s*\{{", text, re.M)
            if m is None:
                continue
            i = text.find("{", m.start(), m.end() + 2)
            if i < 0:
                i = text.find("{", m.end())
            if i < 0:
                continue
            depth, j = 0, i
            while j < len(text):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out[fn["name"]] = (unit, text[m.start():j + 1], text[i + 1:j])
    return out


def proposals(project: Path, cfg: dict, names: dict[str, str], limit: int,
              passes: set[str] = frozenset({"pad", "imports"})) -> dict[str, str]:
    """old -> new for every symbol a pass knows how to name, in a form that links (unique, identifier-safe)."""
    taken = {fn["name"] for fns in cfg["units"].values() for fn in fns}
    defs = definitions(project, cfg)
    want: dict[str, str] = {}

    for name, (_unit, _text, body) in defs.items() if "pad" in passes else ():
        if not re.fullmatch(r"/\*.*?\*/\s*", body, flags=re.S) and body.strip():
            continue
        addr = name.split("_")[-1]
        want[name] = f"pad_{addr}"

    for name, (_unit, text, body) in defs.items() if "imports" in passes else ():
        if name in want:
            continue
        # unresolved imports keep their NID as a name; naming a function after one of those says nothing
        imports = [n for n in (IMPORTS.get(name) or [])
                   if n not in NOISE and re.fullmatch(r"[A-Za-z_]\w*", n)
                   and not (len(n) == 11 and not n.startswith(("sce", "_")))]
        if len(imports) == 1 and len(re.findall(r"[;\n]", body)) <= 3:  # a body that is that call, not a user of it
            want[name] = re.sub(r"\W", "_", imports[0]) + "_call"

    out = {}
    for old, base in want.items():
        new, n = base, 1
        while new in taken and new != old:
            n += 1
            new = f"{base}{n}"
        taken.add(new)
        out[old] = new
        if limit and len(out) >= limit:
            break
    return out


IMPORTS: dict[str, list[str]] = {}


def load_imports() -> dict[str, str]:
    """function name -> the NIDs it imports, and NID -> the library name the surface resolved."""
    surface = json.loads((ROOT / "windows-translation-test-v2/translation/surface/eboot-surface.json").read_text())
    nid_name = {i["nid"]: i["name"] for i in surface["imports"] if i.get("name")}
    for line in (ROOT / "modding/functions/functions.jsonl").open():
        o = json.loads(line)
        IMPORTS[f"func_{int(o['a'], 16):08X}"] = [nid_name.get(n, n) for n in (o.get("i") or [])]
    return nid_name


def apply_renames(project: Path, cfg: dict, mapping: dict[str, str]) -> None:
    """Whole-word, in the config (where the target's symbols come from) and in every source and header."""
    config_path = project / "config/eboot/functions.json"
    data = json.loads(config_path.read_text())
    for fns in data["units"].values():
        for fn in fns:
            if fn["name"] in mapping:
                fn["name"] = mapping[fn["name"]]
    config_path.write_text(json.dumps(data, indent=1))
    for folder in ("src", "include"):
        for path in (project / folder).rglob("*"):
            if path.is_file() and path.suffix in (".c", ".h", ".cpp", ".hpp"):
                text = path.read_text(errors="replace")
                new = text
                for old, fresh in mapping.items():
                    new = re.sub(rf"\b{re.escape(old)}\b", fresh, new)
                if new != text:
                    path.write_text(new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="/home/Trist/Decomp-dev/rea-infamous-second-son-eboot")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--passes", default="pad,imports", help="pad, imports, or both")
    args = ap.parse_args()
    project = Path(args.project)
    names = load_imports()
    cfg = json.loads((project / "config/eboot/functions.json").read_text())

    before = matched_now(project)
    print(f"matched before: {sum(1 for p in before.values() if p >= 100)}")
    mapping = proposals(project, cfg, names, args.limit, set(args.passes.split(",")))
    print(f"proposals: {len(mapping)}")
    for old, new in list(mapping.items())[:12]:
        print(f"  {old} -> {new}")
    if not args.apply:
        print("dry run: nothing changed")
        return 0

    backup = project / "build/polish-backup"
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True)
    shutil.copy2(project / "config/eboot/functions.json", backup / "functions.json")
    for folder in ("src", "include"):
        if (project / folder).is_dir():
            shutil.copytree(project / folder, backup / folder)

    apply_renames(project, cfg, mapping)
    if not rebuild(project):
        print("the project did not build with the new names: restoring")
        restore(project, backup)
        return 1
    after = matched_now(project)
    kept = sum(1 for p in after.values() if p >= 100)
    lost = sorted(n for n, p in before.items() if p >= 100
                  and after.get(mapping.get(n, n), 0) < 100)
    print(f"matched after: {kept}")
    if lost:
        print(f"LOST {len(lost)}: {lost[:6]} - restoring")
        restore(project, backup)
        return 1
    print(f"kept {len(mapping)} renames; every match still matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())

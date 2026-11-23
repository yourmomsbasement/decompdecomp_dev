"""Each PS4 unit's own compiler and flags, why it has them, and the page that says so.

A PS4 game's units aren't all built the same way: on one game, 820 units match only with `-march=btver2` (in long
address runs), and some match with an older upstream clang that clang 5.0.2 doesn't reproduce. A unit's settings live
in tools/compiler_units.json, which the project's tools/compile.py reads:

    "text_00123450": ["-march=btver2"]                      flags only (the form from before reasons were kept)
    "text_00123450": {"compiler": "clang-3.8.1", "flags": [...], "why": "...", "evidence": [...]}

Every unit with settings of its own is listed in docs/build-settings.md (`write_doc`), with why and the functions
that show it, rewritten and committed with every change, so anyone rebuilding the game knows which unit uses what and
on what evidence. tools/compilers.json says where each pinned compiler is and what it needs (`write_compilers`).
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ... import toolchains
from ...hostenv import NO_WINDOW

DEFAULT = "clang-5.0.2"  # the build's own (configure.py's --clang)
UNITS = "tools/compiler_units.json"
COMPILERS = "tools/compilers.json"
DOC = "docs/build-settings.md"
# what an older upstream clang needs to build a PS4 game's C as clang 5.0.2 (patched) does: the PS4 target, which reads
# the stack guard from memory (__stack_chk_guard@GOTPCREL, as the game; the Linux target reads fs:0x28) and makes btver2
# the CPU, so a unit that asks for none keeps the build's x86-64; and the system's headers, which that target doesn't
# search (505 of 2,568 units of a PS4 game didn't build without them)
PS4_TARGET = ["--target=x86_64-scei-ps4", "-idirafter", "/usr/include", "-idirafter", "/usr/include/x86_64-linux-gnu"]
OLDER = ("clang-3.8.1", "clang-3.9.1")  # pinned in toolchains.toml
TEMPLATE_TOOL = Path(__file__).with_name("project_template") / "tools" / "compile.py"
# the build tools decompdecomp keeps up to date in a project (compile.py and bind.py run the others): bind.py stays
# the project's, which projects have extended themselves
TEMPLATE_TOOLS = ("compile.py", "jump_tables.py", "tls_relax.py")
# what each per-unit flag does, as the PS4 guide established it: for a unit whose own reason wasn't recorded when it
# was set, its page still says why such a unit has the flag at all
FLAG_REASONS = {
    "-march=btver2": "builds for the PS4's CPU (Jaguar): its instruction order and register choice, which clang's "
                     "generic x86-64 doesn't reproduce; the game's own code matches with it in long address runs",
    "-fPIC": "the unit's target keeps the three-instruction TLS sequence of code built for shared objects (the TLSGD "
             "pair tools/tls_relax.py relaxes); compile.py drops the base -fPIE for it, which alone changes nothing",
    "-enable-shrink-wrap=false": "keeps the stack frame set up at the function's entry, as the target does, where clang "
                                 "would sink it into the one branch that calls (guide: entry frames with a tail-call exit)",
    "-mno-sse": "copies large structs passed by value with integer moves, as the target does, not SSE ones",
}
IGNORED = "tools/compilers.json"  # this computer's paths: rewritten at every start, kept out of the project's git


_READ: dict[str, tuple[tuple, dict]] = {}  # file -> (its mtime and size, what read made of it)


def read(root: Path, copy: bool = True) -> dict[str, dict]:
    """stem -> {"compiler", "flags", "why", "evidence", "set"}, every entry in the dict form. Parsed once per change
    of the file: the project's overview asks each of its 2,567 source files' compiler, and parsing the file (373 KB)
    for each took 6.5 s, on every second's poll of the GUI and every helper's project_info (claims waited 180 s)."""
    path = root / UNITS
    try:
        st = path.stat()
    except OSError:
        return {}
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _READ.get(str(path))
    if hit and hit[0] == stamp:
        return _copy(hit[1]) if copy else hit[1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for stem, entry in raw.items():
        if isinstance(entry, list):
            entry = {"flags": entry}
        out[stem] = {"compiler": entry.get("compiler") or DEFAULT, "flags": list(entry.get("flags", [])),
                     "why": entry.get("why", ""), "evidence": list(entry.get("evidence", [])), "set": entry.get("set", "")}
    _READ[str(path)] = (stamp, out)
    return _copy(out) if copy else out


def _copy(settings: dict[str, dict]) -> dict[str, dict]:
    return {k: dict(v, flags=list(v["flags"]), evidence=list(v["evidence"])) for k, v in settings.items()}


def write(root: Path, settings: dict[str, dict]) -> None:
    """compiler_units.json from `settings`: an entry only for a unit with settings of its own; the list form for one
    with flags and nothing else (as before), so a project's file only grows where there's something to say."""
    raw = {}
    for stem in sorted(settings):
        s = settings[stem]
        own = s["compiler"] != DEFAULT
        if not (own or s["flags"] or s["why"]):
            continue
        if not (own or s["why"] or s["evidence"]):
            raw[stem] = s["flags"]
            continue
        raw[stem] = {k: v for k, v in (("compiler", s["compiler"] if own else None), ("flags", s["flags"]),
                                       ("why", s["why"]), ("evidence", s["evidence"]), ("set", s["set"])) if v}
    _write_atomic(root / UNITS, json.dumps(raw, indent=1) + "\n")


def _write_atomic(path: Path, text: str) -> None:
    """Written whole, then put in place: the app reads the file while the survey writes it (a reader that saw it
    half-written failed a helper's project_info)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def stem_of(source: str) -> str:
    return Path(source).stem


def file_compiler(root: Path, source: str) -> str:
    return read(root, copy=False).get(stem_of(source), {}).get("compiler", DEFAULT)


def set_compiler(root: Path, source: str, compiler: str, why: str = "", evidence: list | None = None) -> None:
    """A unit built with `compiler` from now on, saying why (back to the default: its reason and evidence go)."""
    settings = read(root)
    s = settings.setdefault(stem_of(source), {"compiler": DEFAULT, "flags": [], "why": "", "evidence": [], "set": ""})
    s["compiler"] = compiler
    if compiler == DEFAULT:
        s["why"], s["evidence"], s["set"] = "", [], ""
    else:
        s["why"], s["evidence"], s["set"] = why or s["why"], list(evidence or s["evidence"]), time.strftime("%Y-%m-%d")
    write(root, settings)


def explain(root: Path, stem: str, why: str, evidence: list | None = None) -> None:
    """Record why a unit has the settings it has (flags set before reasons were kept: the btver2 ranges)."""
    settings = read(root)
    if stem in settings:
        settings[stem]["why"] = why
        if evidence is not None:
            settings[stem]["evidence"] = list(evidence)
        settings[stem]["set"] = settings[stem]["set"] or time.strftime("%Y-%m-%d")
        write(root, settings)


def write_compilers(root: Path) -> Path:
    """tools/compilers.json: where each pinned compiler is installed and what it needs (compile.py reads it). One that
    isn't installed is left out: a unit that names it says so when it builds."""
    known = {DEFAULT: {"name": "Clang 5.0.2 (the build's own: configure.py --clang)", "path": ""}}
    for tid in OLDER:
        if toolchains.installed(tid) is not None:
            known[tid] = {"name": toolchains.get(tid).name, "path": str(toolchains.binary(tid)), "flags": PS4_TARGET,
                          "default_march": "x86-64"}
    path = root / COMPILERS
    _write_atomic(path, json.dumps(known, indent=1) + "\n")
    return path


def base_flags(root: Path) -> list[str]:
    """The flags every unit gets (configure.py's CL_FLAGS)."""
    try:
        tree = ast.parse((root / "tools/configure.py").read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "CL_FLAGS" for t in node.targets):
            try:
                return list(ast.literal_eval(node.value))
            except ValueError:
                return []
    return []


def write_doc(root: Path, order: list[str] | None = None) -> Path:
    """docs/build-settings.md: the build's defaults, then every unit with settings of its own, with why and on what
    evidence. Units that share settings and a reason are one entry, their units in address runs (`order`: every unit
    of the project, in address order, to tell runs apart)."""
    settings = {k: v for k, v in read(root).items() if v["compiler"] != DEFAULT or v["flags"] or v["why"]}
    position = {stem: i for i, stem in enumerate(order or sorted(settings, key=_address))}
    history = _history(root)
    groups: dict[tuple, list[str]] = {}
    for stem, s in settings.items():
        groups.setdefault((s["compiler"], tuple(s["flags"]), s["why"]), []).append(stem)
    lines = ["# Build settings", "",
             f"Written by decompdecomp from `{UNITS}`, whenever a unit's settings change. Don't edit it by hand: the next",
             "change writes it again. What it says is what the build does (`tools/compile.py` reads the same file).", "",
             "## Every unit", "",
             f"Built with **{DEFAULT}** and `{' '.join(base_flags(root))}`, unless it's listed below.", "",
             f"## Units with settings of their own ({len(settings)})", ""]
    if not settings:
        lines.append("None.")
    for (compiler, flags, why), stems in sorted(groups.items(), key=lambda g: (-len(g[1]), g[0])):
        stems.sort(key=lambda s: position.get(s, 1 << 30))
        what = ([f"compiler **{compiler}**"] if compiler != DEFAULT else []) + ([f"flags `{' '.join(flags)}`"] if flags else [])
        lines += [f"### {len(stems)} unit{'s' * (len(stems) != 1)}: {', '.join(what) or 'the defaults, with a note'}", ""]
        if why:
            lines += [f"Why: {why}", ""]
        else:
            lines += ["Why: not recorded when it was set (before decompdecomp kept a reason for each unit)."]
            general = [f"`{f}` {FLAG_REASONS[k]}" for f in flags for k in FLAG_REASONS if f.endswith(k)]
            lines += [f"What {'these flags do' if len(flags) > 1 else 'this flag does'}: " + "; ".join(general) + "."
                      ] if general else []
            set_in = sorted({history[s] for s in stems if s in history})
            lines += [f"Set in {', '.join(set_in[:3])}{', ...' if len(set_in) > 3 else ''}."] if set_in else []
            lines.append("")
        lines += ["Units: " + _grouped(stems, position), ""]
        shown = [(s, settings[s]) for s in stems if settings[s]["evidence"]]
        if shown:
            lines += ["Evidence (the functions that match only with these settings, and when it was found):", ""]
            for stem, s in shown:
                found = "; ".join(_evidence_text(e) for e in s["evidence"])
                lines.append(f"- `{stem}` ({s['set'] or 'date not recorded'}): {found}")
            lines.append("")
    used = {s["compiler"] for s in settings.values()}
    lines += ["## Compilers", "", "| Compiler | Where it comes from | Units |", "| :-- | :-- | --: |",
              f"| {DEFAULT} | the build's own (`configure.py --clang`) | every other unit |"]
    for tid in OLDER:
        if tid in used:
            tc = toolchains.get(tid)
            lines.append(f"| {tid} | {tc.source}, sha256 pinned in decompdecomp's toolchains.toml | "
                         f"{sum(s['compiler'] == tid for s in settings.values())} |")
    path = root / DOC
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _history(root: Path) -> dict[str, str]:
    """stem -> the commit that first gave it settings ("commit cc8db789 of 2026-10-24"), from the project's git."""
    try:
        done = subprocess.run(["git", "log", "--reverse", "--format=@@%h %ad", "--date=short", "-p", "--", UNITS],
                              cwd=root, capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return {}
    out, commit = {}, ""
    for line in done.stdout.splitlines():
        if line.startswith("@@") and not line.startswith("@@ "):
            sha, date = (line[2:].split() + [""])[:2]
            commit = f"commit {sha} of {date}"
        elif line.startswith("+") and (m := re.match(r'\+\s*"([^"]+)"\s*:', line)):
            out.setdefault(m.group(1), commit)
    return out


def _address(stem: str) -> int:
    m = re.search(r"([0-9A-Fa-f]{6,})$", stem)
    return int(m.group(1), 16) if m else 0


def _grouped(stems: list[str], position: dict[str, int]) -> str:
    """Units in runs of the project's order: `text_A` to `text_B` (n), one by one when alone."""
    out, run = [], []
    for stem in stems:
        if run and position.get(stem, -9) != position.get(run[-1], -99) + 1:
            out.append(run)
            run = []
        run.append(stem)
    if run:
        out.append(run)
    return ", ".join(f"`{r[0]}`" if len(r) == 1 else f"`{r[0]}` to `{r[-1]}` ({len(r)})" for r in out)


def _evidence_text(e: dict) -> str:
    parts = [f"`{e.get('function', '?')}`"]
    if e.get("before") is not None:
        parts.append(f"{e['before']:.1f}% with {DEFAULT}, 100% with it")
    if e.get("by"):
        parts.append(f"by {e['by']}")
    return " ".join(parts)


def build_with(root: Path, source: str, compiler: str | None, out: Path, text: str | None = None) -> Path | None:
    """A unit's object built outside the project's build (`out`: a folder of the same layout, build/<version>/src/...,
    whose target folder is the project's), with the unit's flags and the build's: with `compiler` in place of its own
    (the compiler survey), and/or from `text` in place of its source file (the permuter's variants: nothing in the
    project's tree is written or rebuilt). None: it doesn't build."""
    ninja = _ninja_command(root, source)
    if ninja is None:
        return None
    args, obj_rel = ninja
    obj = out / obj_rel
    obj.parent.mkdir(parents=True, exist_ok=True)
    target_dir = obj.parent.parent.parent / "target"
    if not target_dir.exists():
        target_dir.symlink_to(root / Path(obj_rel).parent.parent.parent / "target", target_is_directory=True)
    cmd = [*args[: args.index("--output") + 1], str(obj), *args[args.index("--output") + 2:]]
    if compiler:
        cmd = [*cmd[: cmd.index("--")], "--unit-compiler", compiler, *cmd[cmd.index("--"):]]
    if text is not None:  # the same name (compile.py finds the unit's settings by it), its own folder's headers too
        copy = out / source
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(text, encoding="utf-8")
        cmd[cmd.index("--source") + 1] = str(copy)
        cmd += ["-iquote", str((root / source).parent)]
    done = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=600, creationflags=NO_WINDOW)
    return obj if done.returncode == 0 and obj.exists() else None


_COMMANDS: dict[tuple, tuple[list[str], str] | None] = {}  # (root, source, build.ninja's mtime) -> _ninja_command's


def _ninja_command(root: Path, source: str) -> tuple[list[str], str] | None:
    """(compile.py's command for `source`'s object, as build.ninja has it, the object's path) or None. Asked once per
    unit while build.ninja is the same: the permuter builds one unit's variants back to back."""
    try:
        key = (str(root), source, (root / "build.ninja").stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _COMMANDS:
        _COMMANDS[key] = _read_ninja_command(root, source)
    hit = _COMMANDS[key]
    return (list(hit[0]), hit[1]) if hit else None


def _read_ninja_command(root: Path, source: str) -> tuple[list[str], str] | None:
    import shlex

    from ...forks.project import _tool

    ninja = _tool("ninja")
    if not ninja:
        return None
    rel = Path(source).with_suffix(".o")
    for build in sorted((root / "build").glob("*/" + str(rel))):
        obj_rel = build.relative_to(root).as_posix()
        break
    else:
        versions = sorted(p.name for p in (root / "build").iterdir() if p.is_dir()) if (root / "build").exists() else []
        obj_rel = f"build/{versions[0]}/{rel.as_posix()}" if versions else ""
    if not obj_rel:
        return None
    done = subprocess.run([ninja, "-t", "commands", obj_rel], cwd=root, capture_output=True, text=True, timeout=30,
                          creationflags=NO_WINDOW)
    for line in reversed(done.stdout.splitlines()):
        args = shlex.split(line)
        if len(args) > 3 and args[1] == "tools/compile.py" and "--output" in args and "--" in args:
            return [sys.executable if Path(args[0]).name.startswith("python") and not Path(args[0]).exists() else args[0],
                    *args[1:]], obj_rel
    return None


def _version(text: str) -> int:
    """A compile.py's TOOL_VERSION: 1 for the first that read unit settings, 0 for one before."""
    m = re.search(r"^TOOL_VERSION = (\d+)", text, re.M)
    return int(m.group(1)) if m else 1 if "def unit_settings" in text else 0


def _matched(fp) -> set[str]:
    """unit:function of everything objdiff's report says matches, from the objects as they are."""
    rep = fp.report()
    return {f"{u['name']}:{f['name']}" for u in rep.get("units", []) for f in u.get("functions", [])
            if (f.get("fuzzy_match_percent") or 0) >= 100}


def upgrade(net, slug: str, log=print) -> str | None:
    """Bring a PS4 project's build tools up to decompdecomp's (tools/compile.py, which reads each unit's compiler and
    reasons, and jump_tables.py), kept only when the rebuilt project still matches every function it matched; and
    every start, tools/compilers.json (this computer's paths) and docs/build-settings.md. Says what changed, as a
    sentence; None: nothing did."""
    engine = net.forks
    proj, fp, cfg = engine.get(slug)
    tool = fp.root / "tools" / "compile.py"
    if cfg.get("origin") != "scratch" or not tool.is_file():
        return None
    said, commit = [], []
    ignore = fp.root / ".gitignore"
    if ignore.is_file() and IGNORED not in ignore.read_text(encoding="utf-8").split():
        ignore.write_text(ignore.read_text(encoding="utf-8").rstrip("\n") + f"\n{IGNORED}\n", encoding="utf-8")
        commit.append(".gitignore")
    write_compilers(fp.root)
    if _outdated(fp.root):
        with engine._tree(proj).exclusive():
            # decided again with the build held: the app's start and a Check again both run this, and the one that
            # waited rebuilt the whole game a second time for nothing (33 minutes of the build held, on a PS4 game)
            if _outdated(fp.root):
                said += _install_tools(net, engine, slug, proj, fp, commit, log)
    doc = fp.root / DOC
    was = doc.read_text(encoding="utf-8") if doc.exists() else None
    write_doc(fp.root)
    if doc.read_text(encoding="utf-8") != was:
        commit.append(DOC)
        said.append(f"{DOC} lists each unit's own compiler and flags, and why")
    if commit:
        engine._commit_paths(proj, fp, commit, "Build settings per unit, documented\n\n" + "; ".join(said or
                             ["the project's ignore list leaves this computer's compiler paths out"]) + ".\n\nBy decompdecomp.")
    if said:
        with net.store.tx() as db:
            net._event(db, proj["id"], None, "analysis", "; ".join(said))
        log("; ".join(said))
    return "; ".join(said) or None


def _outdated(root: Path) -> bool:
    """Whether a project's build tools are older than decompdecomp's (compile.py's TOOL_VERSION), or one is missing."""
    tool = root / "tools" / "compile.py"
    return (_version(tool.read_text(encoding="utf-8")) < _version(TEMPLATE_TOOL.read_text(encoding="utf-8"))
            or not all((root / "tools" / name).exists() for name in TEMPLATE_TOOLS))


def _install_tools(net, engine, slug: str, proj, fp, commit: list, log) -> list[str]:
    """The template's build tools in the project (the build held by the caller), kept only if the rebuilt project
    still matches every function it matched: what it says, the files to commit added to `commit`."""
    olds = {name: (fp.root / "tools" / name).read_text(encoding="utf-8") if (fp.root / "tools" / name).exists() else None
            for name in TEMPLATE_TOOLS}
    try:
        before = _matched(fp)
    except Exception as e:  # nothing to compare with: leave the build as it is
        log(f"PS4 build tools not brought up to date: the project couldn't be measured ({e})")
        return []
    for name in TEMPLATE_TOOLS:
        shutil.copy2(TEMPLATE_TOOL.with_name(name), fp.root / "tools" / name)
    ok, _ = engine._build_all(proj, fp)
    try:
        now = _matched(fp) if ok else set()
        lost = before - now
    except Exception:
        ok, lost, now = False, before, set()
    if not ok or lost:
        for name, text in olds.items():
            path = fp.root / "tools" / name
            path.unlink(missing_ok=True) if text is None else path.write_text(text, encoding="utf-8")
        engine._build_all(proj, fp)
        why = "didn't build the project" if not ok else f"lost {len(lost)} match(es)"
        text = f"its build tools were kept as they were: decompdecomp's newest {why}"
        with net.store.tx() as db:
            net._event(db, proj["id"], None, "analysis", text)
        log(text)
        return []
    commit += [f"tools/{name}" for name in TEMPLATE_TOOLS]
    if now - before:
        engine.sync(slug, log=lambda *a: None)  # (the new matches, recorded)
    return [f"its build tools are decompdecomp's newest (every match kept, {len(now - before)} more)"]

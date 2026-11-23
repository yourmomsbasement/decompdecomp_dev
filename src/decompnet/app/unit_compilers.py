"""A source file's compiler, switched only on evidence, and the survey that finds where another one fits.

Matching every byte is the judge's job and stays it; which compiler a file is built with is a claim about how the
game was made, and one function matching with another compiler can be luck. So a file switches only when
Console.compiler_evidence of its functions match only with the other compiler and none of its matches is lost
(`decide`). A single match short of that is kept as evidence (the compiler_evidence table: the function and its C),
so the next function of the file to show the same completes it. On a PS4 game's 400 sampled units the rule found 30
whose older clang gains matches without losing any; one such match alone moves no file.

Each switch is written with its reason and evidence where the console keeps a file's settings, and the project's
page of build settings is written again and committed with it (Console.settings_doc): which file uses which compiler
and flags, why, and which functions show it.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

from .. import consoles
from ..forks import objdiff
from ..network import NetworkError

log = logging.getLogger("decompdecomp")


def decide(before: dict, after: dict, needed: int) -> tuple[bool, list[str], list[str]]:
    """(switch?, functions matched only after, functions matched only before) for a file's scores ({function:
    percent}) with its compiler and with another."""
    was = {n for n, p in before.items() if (p or 0) >= 100}
    now = {n for n, p in after.items() if (p or 0) >= 100}
    gained, lost = sorted(now - was), sorted(was - now)
    return (not lost and len(gained) >= needed), gained, lost


def needed(console, base: dict) -> int:
    """How many of a file's functions must match only with another compiler: the console's number, or every function
    of a file with fewer (a one-function file of a PS4 game could never show two)."""
    return max(1, min(console.compiler_evidence, len(base) or console.compiler_evidence))


def reason(compiler: str, own: str, evidence: list[dict]) -> str:
    names = [e["function"] for e in evidence]
    return (f"{len(names)} of its functions match byte for byte only with {compiler} ({', '.join(names[:6])}"
            f"{', ...' if len(names) > 6 else ''}), and every function that matched with {own} still does")


def stored(net, proj, source: str, compiler: str) -> list[dict]:
    """The evidence kept for a file and a compiler: functions that matched only with it, still open."""
    rows = net.store.all("SELECT e.*, f.name, f.status FROM compiler_evidence e JOIN functions f ON f.id=e.function_id "
                         "WHERE e.project_id=? AND e.source=? AND e.compiler=?", proj["id"], source, compiler)
    return [dict(r) for r in rows if r["status"] == "open"]


def keep(net, proj, source: str, compiler: str, fn, code: str, before: float | None, agent) -> int:
    """Keep one function's match with `compiler` as evidence for its file; how many functions the file has so far."""
    with net.store.tx() as db:
        db.execute("INSERT OR REPLACE INTO compiler_evidence VALUES(?,?,?,?,?,?,?,?)",
                   (proj["id"], source, compiler, fn["id"], code, before, agent.id if agent else None, time.time()))
    return len(stored(net, proj, source, compiler))


def forget(net, proj, source: str) -> None:
    with net.store.tx() as db:
        db.execute("DELETE FROM compiler_evidence WHERE project_id=? AND source=?", (proj["id"], source))


def scores(fp, unit, obj: Path) -> dict:
    """{function: percent} of a unit's object against its target, as the judge's report measures it."""
    return objdiff.diff(unit.target, obj, None, cwd=fp.root, options=fp.diff_options(unit)).unit_scores


def survey(net, agent, slug: str, apply: bool = False, limit: int = 0, log=log.info) -> str:
    """Each unit with functions left to match, built (outside the project's build) with each other compiler of its
    console: where one passes `decide`, it's reported, and with `apply` the unit switches to it, is rebuilt in the
    project and checked again (taken back unless it still passes), and its new matches are recorded. The project's
    page of build settings says which unit uses what and why."""
    engine = net.forks
    proj, fp, cfg = engine.get(slug)
    console = consoles.get(cfg.get("platform"))
    known = [c.id for c in console.compilers]
    units = [u for u in fp.units() if u.source and u.target and u.target.exists() and u.base and u.base.exists()]
    if len(known) < 2 or not units or console.file_compiler(fp, units[0].source) is None:
        raise NetworkError(f"this {console.name} project's build has no compiler per source file to survey")
    found, examined, failed = [], 0, 0
    with tempfile.TemporaryDirectory(prefix="decompdecomp-survey-") as tmp:
        for unit in units:
            if limit and examined >= limit:
                break
            own = console.file_compiler(fp, unit.source)
            try:
                base = scores(fp, unit, unit.base)
            except Exception:
                failed += 1
                continue
            if all((p or 0) >= 100 for p in base.values()):
                continue  # nothing left to gain in it
            examined += 1
            best = None
            for compiler in (c for c in known if c != own):
                obj = console.build_unit_with(fp, unit, compiler, Path(tmp) / compiler)
                if obj is None:
                    continue
                try:
                    ok, gained, _ = decide(base, scores(fp, unit, obj), needed(console, base))
                except Exception:
                    continue
                # (a tie goes to the later compiler of the console's list: the nearer to the build's own)
                if ok and (best is None or len(gained) >= len(best[1])):
                    best = (compiler, gained)
            if best:
                found.append((unit, own, best[0], best[1], base))
                log(f"compiler survey: {unit.name}: {len(best[1])} functions match only with {best[0]}")
    applied = 0
    if apply and found:
        for unit, own, compiler, gained, base in found:
            applied += switch(net, agent, slug, unit, own, compiler, gained, base)
        if applied:
            doc = console.settings_doc(fp)
            engine._commit_paths(proj, fp, [p for p in ("tools/compiler_units.json", doc) if p],
                                 f"Build {applied} units with the compilers their functions show\n\nEach is in "
                                 f"{doc or 'the settings'}, with the functions that match only with it.\n\n"
                                 f"Found by decompdecomp's compiler survey.")
    gains = sum(len(g) for _, _, _, g, _ in found)
    lines = [f"Compiler survey of {examined} units with functions left to match"
             + (f" ({failed} couldn't be measured)" if failed else "") + f": {len(found)} would switch, "
             f"{gains} functions matching only with their new compiler, no match lost in any"
             + (f"; {applied} switched and checked in the project" if apply else " (report only; apply=true switches them)")]
    lines += [f"- {u.name}: {own} -> {c}: {', '.join(g[:6])}{', ...' if len(g) > 6 else ''}" for u, own, c, g, _ in found[:40]]
    return "\n".join(lines)


def switch(net, agent, slug: str, unit, own: str, compiler: str, gained: list[str], base: dict) -> bool:
    """One unit to `compiler`, with its reason and evidence, rebuilt and checked in the project: kept only if it still
    passes `decide` there; its new matches recorded. Whether it was kept."""
    engine = net.forks
    proj, fp, cfg = engine.get(slug)
    console = consoles.get(cfg.get("platform"))
    evidence = [{"function": n, "before": round(base.get(n) or 0.0, 2), "by": getattr(agent, "name", "")} for n in gained]
    with engine._tree(proj).exclusive(), engine._unit_lock(proj, unit.name):
        console.set_file_compiler(fp, unit.source, compiler, reason(compiler, own, evidence), evidence)
        ok, _ = engine._build_unit(proj, fp, unit)
        try:
            after = scores(fp, unit, unit.base) if ok else {}
        except Exception:
            after = {}
        good, _, lost = decide(base, after, needed(console, base))
        if not good:
            console.set_file_compiler(fp, unit.source, own)
            engine._build_unit(proj, fp, unit)
            log.info("compiler survey: %s taken back (%s)", unit.name, f"lost {', '.join(lost[:4])}" if lost else
                     "fewer matches in the project's own build")
            return False
        text = (fp.root / unit.source).read_text(errors="replace", encoding="utf-8")
        engine._apply_scores(proj, unit.name, after, agent=agent, text=text, via=f"the compiler survey ({compiler})")
    forget(net, proj, unit.source)
    return True

"""Units whose code reads thread-locals the way code built for shared objects does, built that way.

The game's TLS access is `mov rax,fs:[0]; add rax,[D_slot]; mov rax,[rax]`: the TLSGD resolver pair of code built with
-fPIC, relaxed by the game's linker. clang builds a unit with the base -fPIE into two instructions (GOTTPOFF) that no
C turns into the three, so only a unit built with -fPIC, whose pair tools/tls_relax.py relaxes as the linker did, can
match such a function. On a PS4 game 352 units' targets had the sequence, 344 of them built with -fPIE: their 715
open functions with it had never matched, and agents reported the same boundary unit after unit, unable to change a
build's flags (that's the build's to decide, and here it does).

The evidence is the target's own bytes (`evidence`), not a match: the sequence is what the linker makes of a TLSGD
pair and of nothing else. A unit gets -fPIC only if every function that matched in it still does (`give_pic`), with
its reason and the functions that show it written where its settings are (units.py, docs/build-settings.md).
tools/tls_slots.json names each slot the game reads (`write_slots`): relaxation needs the C to name the slot it reads.
"""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from . import units

SLOTS = "tools/tls_slots.json"
TOOL = Path(__file__).with_name("project_template") / "tools" / "tls_relax.py"
KEPT_OUT = "ps4_tls_kept_out"  # the project's config: units -fPIC lost a match in (not tried again)


def _relax():
    spec = importlib.util.spec_from_file_location("ps4_tls_relax", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def evidence(fp) -> dict[str, dict[str, set[str]]]:
    """unit -> {function: the TLS slots its target reads through the relaxed TLSGD sequence}."""
    relax = _relax()
    out = {}
    for unit in fp.units():
        if unit.target and unit.target.exists():
            try:
                found = relax._slot_evidence(unit.target)
            except Exception:  # an object the reader can't take: no evidence
                continue
            if found:
                out[unit.name] = found
    return out


def slot_name(slot: str) -> str:
    return f"decomp_tls_{slot}"


def write_slots(root: Path, slots: set[str]) -> bool:
    """tools/tls_slots.json with a name for every slot the game's code reads (a project's own names stay: one game
    named its first slot `decomp_tls_context`). Whether it changed."""
    path = root / SLOTS
    try:
        mapping = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except ValueError:
        mapping = {}
    named = set(mapping.values())
    new = {slot_name(s): s for s in sorted(slots - named)}
    if not new:
        return False
    units._write_atomic(path, json.dumps({**mapping, **new}, indent=2) + "\n")
    return True


def names(root: Path) -> dict[str, str]:
    """slot -> the name the C reads it by."""
    try:
        mapping = json.loads((root / SLOTS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {slot: name for name, slot in mapping.items()}


def reason(functions: list[str], slots: set[str]) -> str:
    return (f"its functions keep the game's three-instruction TLS access of code built for shared objects "
            f"({', '.join(functions[:6])}{', ...' if len(functions) > 6 else ''}: mov fs:[0]; add [{', '.join(sorted(slots))}]; "
            "load), which only a -fPIC build relaxes into (tools/tls_relax.py); every function that matched in it "
            "still does")


def give_pic(net, slug: str, log=print) -> list[str]:
    """Every unit whose target shows the sequence built with -fPIC, one at a time with the build held, kept only if
    none of its matches is lost; the slots named. Commits what changed; the units given -fPIC."""
    from ...app import unit_compilers

    engine = net.forks
    proj, fp, cfg = engine.get(slug)
    if not (fp.root / "tools" / "tls_relax.py").exists():
        return []  # (a project whose bind.py doesn't relax TLS: -fPIC alone changes nothing)
    found = evidence(fp)
    if not found:
        return []
    commit = []
    if write_slots(fp.root, {s for fns in found.values() for v in fns.values() for s in v}):
        commit.append(SLOTS)
    kept_out = set(cfg.get(KEPT_OUT, []))
    settings = units.read(fp.root)
    given, refused = [], []
    for name in sorted(found):
        unit = fp.unit(name)
        stem = units.stem_of(unit.source) if unit.source else ""
        if not stem or name in kept_out or "-fPIC" in settings.get(stem, {}).get("flags", []):
            continue
        if not (unit.base and unit.base.exists()):
            continue
        functions = sorted(found[name])
        slots = {s for v in found[name].values() for s in v}
        with engine._tree(proj).exclusive(), engine._unit_lock(proj, name):
            try:
                before = unit_compilers.scores(fp, unit, unit.base)
            except Exception:
                continue
            settings = units.read(fp.root)
            s = settings.setdefault(stem, {"compiler": units.DEFAULT, "flags": [], "why": "", "evidence": [], "set": ""})
            was = units._copy({stem: s})[stem]
            s["flags"] = [*s["flags"], "-fPIC"]
            s["why"] = "; ".join(x for x in (s["why"], reason(functions, slots)) if x)
            s["evidence"] = [*s["evidence"], *({"function": f, "tls": sorted(found[name][f])} for f in functions)]
            s["set"] = s["set"] or time.strftime("%Y-%m-%d")
            units.write(fp.root, settings)
            ok, _ = engine._build_unit(proj, fp, unit)
            try:
                after = unit_compilers.scores(fp, unit, unit.base) if ok else {}
            except Exception:
                after = {}
            _, gained, lost = unit_compilers.decide(before, after, 0)
            if not ok or lost or not after:
                settings[stem] = was
                units.write(fp.root, settings)
                engine._build_unit(proj, fp, unit)
                refused.append(name)
                log(f"PS4 TLS: {name} stays -fPIE ({'lost ' + ', '.join(lost[:4]) if lost else 'it did not build'})")
                continue
            if gained:
                text = (fp.root / unit.source).read_text(errors="replace", encoding="utf-8")
                engine._apply_scores(proj, name, after, agent=None, text=text, via="its unit built with -fPIC")
            given.append(name)
    if refused:
        net._update_cfg(slug, lambda c: c.__setitem__(KEPT_OUT, sorted(set(c.get(KEPT_OUT, [])) | set(refused))))
    if given:
        units.write_doc(fp.root)
        commit += [units.UNITS, units.DOC]
        net.bump_generation(proj["id"])  # (earlier compiles of these units aren't repeats)
    if commit:
        engine._commit_paths(proj, fp, sorted(set(commit)),
                             f"Build {len(given)} units with -fPIC, as their TLS access shows\n\nEach one's target reads "
                             "a thread-local through the relaxed TLSGD sequence of code built for shared objects; "
                             f"every match in them is kept. {len(refused)} stay -fPIE (one of their matches needs it). "
                             f"The slots are named in {SLOTS}.\n\nBy decompdecomp.")
    return given

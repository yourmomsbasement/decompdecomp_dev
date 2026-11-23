"""Small, revision-checked edits and bounded experiments for source-tree projects.

ROM candidates do not live in a unit source file; their transaction/preview support is tracked in ROADMAP.md.
All writes still pass through ForkEngine's ordinary strict judge and matched-neighbor guard.
"""

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from .. import consoles, experiments
from ..arch import x86
from ..forks import csource
from ..network import NetworkError
from .code_blocks import hints
from .function_patterns import hints as pattern_hints
from .function_patterns import library as pattern_library
from .tiny_functions import processor

MAX_TRIALS = 8
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_INPUT_FILES = 20000


def patch(source: str, edits: list[dict]) -> str:
    if not isinstance(edits, list) or not 1 <= len(edits) <= 16:
        raise NetworkError("Send between 1 and 16 exact replacements.")
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"old", "new"}:
            raise NetworkError("Each edit must contain only old and new strings.")
        old, new = edit["old"], edit["new"]
        if not isinstance(old, str) or not isinstance(new, str) or not old or source.count(old) != 1:
            raise NetworkError("Each old string must occur exactly once in the current definition.")
        source = source.replace(old, new, 1)
        if len(source.encode()) > 64 * 1024:
            raise NetworkError("Patched definition is too large.")
    return source


def _read(path, limit=MAX_INPUT_BYTES):
    """Bound external project reads and turn disappearing inputs into an actionable tool error."""
    try:
        if not path.is_file() or path.stat().st_size > limit:
            raise NetworkError("Project inputs are too large; use compile_and_diff instead.")
        with path.open('rb') as file:
            data = file.read(limit + 1)
        if len(data) > limit:
            raise NetworkError("Project inputs are too large; use compile_and_diff instead.")
        return data
    except OSError as exc:
        raise NetworkError("Cannot read project inputs; check their paths and permissions.") from exc


@contextmanager
def _locked(engine, slug, function, read=False):
    """Hold this unit while other units can build; only linked-game trials need the writer lock."""
    proj, fp, _ = engine.get(slug)
    fn = engine._fn(slug, function)
    unit = fp.unit(fn['unit'])
    linked = not read and unit.complete and fp.linkable()
    with engine._tree(proj).exclusive() if linked else engine._tree(proj).shared(), engine._unit_lock(proj, unit.name):
        yield


def revision_parts(engine, slug, function):
    """(fn, definition, revision) and the named inputs the revision is made of.

    Naming them lets a rejected patch say *what* moved instead of only that something did: until now every mismatch
    read "Stale revision", which is indistinguishable between "the agent edited its own function" and "a header
    changed".
    """
    fn, definition, parts = None, "", {}
    proj, fp, cfg = engine.get(slug)
    fn = engine._fn(slug, function)
    unit = fp.unit(fn["unit"])
    if not unit.source:
        raise NetworkError("This function needs a unit source file before using patches or recipes.")
    try:
        raw = _read(fp.safe_path(unit.source))
        text = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    except UnicodeError as exc:
        raise NetworkError("Cannot read the unit source; use UTF-8 before patching it.") from exc
    _, span = engine._find(text, engine._names(engine._meta(fn), fn))
    if span is None or csource.is_stub(text[span[0]:span[1]]):
        raise NetworkError("Add the definition with compile_and_diff first.")
    definition = text[span[0]:span[1]]
    build_cfg = {k: cfg[k] for k in ('platform', 'version', 'compiler', 'compiler_ranges') if k in cfg}
    # This file's own compiler and flags where each file has its own (PS4: tools/compiler_units.json), read through
    # the console rather than hashing the files that hold every file's (Console.unit_settings_files)
    console = consoles.get(cfg.get('platform') or (getattr(fp, 'manifest', None) or {}).get('platform'))
    try:
        own = (console.file_settings(fp) or {}).get(unit.source) or {}
        build_cfg['file'] = {'compiler': console.file_compiler(fp, unit.source), 'flags': console.file_flags(fp, unit.source),
                             'settings': {k: own.get(k) for k in ('compiler', 'flags')}}
    except Exception:  # noqa: BLE001  an unreadable build config: the fresh builds and the judge still see it
        build_cfg['file'] = None
    skipped = set(console.unit_settings_files)
    parts["unit"] = hashlib.sha256(raw).hexdigest()
    parts["definition"] = hashlib.sha256(definition.encode()).hexdigest()
    parts["config"] = hashlib.sha256(json.dumps(build_cfg, sort_keys=True).encode()).hexdigest()
    headers = hashlib.sha256()
    visited, total = 0, len(raw)
    for directory, dirs, files in os.walk(fp.root):
        dirs[:] = sorted(d for d in dirs if d not in {".git", ".venv", "build", "orig", "extract", "node_modules", "__pycache__", ".decomp-wtf", ".pytest_cache", ".ruff_cache"})
        visited += len(dirs) + len(files)
        if visited > MAX_INPUT_FILES:
            raise NetworkError("Project input scan is too large; use compile_and_diff instead.")
        for name in sorted(files):
            relative = (Path(directory) / name).relative_to(fp.root)
            if relative.as_posix() in skipped:
                continue
            if name.endswith((".h", ".hpp", ".hh", ".hxx", ".inc")) or (
                    relative.parts[0] in {"tools", "config", "harness"}):
                path = fp.root / relative
                # SDK links may point outside the project. Their contents are outside this revision; every
                # preview still builds fresh, and a keeping attempt is always judged independently.
                if path.is_symlink():
                    continue
                data = _read(path, MAX_INPUT_BYTES - total)
                total += len(data)
                headers.update(str(relative).encode() + b"\0" + data + b"\0")
    parts["headers"] = headers.hexdigest()
    build = hashlib.sha256()
    for name in ("objdiff.json", "build.ninja", "Makefile", "CMakeLists.txt", ".decomp.wtf.json"):
        config = fp.root / name
        if config.is_file():
            data = _read(config, MAX_INPUT_BYTES - total)
            total += len(data)
            build.update(name.encode() + b"\0" + data + b"\0")
    parts["build"] = build.hexdigest()
    if unit.target and unit.target.exists():
        parts["target"] = hashlib.sha256(_read(unit.target, MAX_INPUT_BYTES - total)).hexdigest()
    revision = hashlib.sha256((json.dumps(parts, sort_keys=True) + "\0" + str(fn["id"])).encode()).hexdigest()
    return fn, definition, revision, parts


def snapshot(engine, slug, function):
    """(fn, definition, revision): the revision names the inputs it was made from (revision_parts)."""
    fn, definition, revision, _parts = revision_parts(engine, slug, function)
    return fn, definition, revision


def definition(engine, fn, source):
    names = engine._names(engine._meta(fn), fn)
    clean = csource._blank(source)
    declared = next((re.search(r"(?<![\w:])" + re.escape(name) + r"\s*\(", clean) for name in names
                     if re.search(r"(?<![\w:])" + re.escape(name) + r"\s*\(", clean)), None)
    directives = re.findall(r"(?m)^\s*(#[^\n]*)", clean)
    loop_pragma = r"#\s*pragma\s+clang\s+loop\s+(?:(?:vectorize|interleave|unroll|distribute)(?:_count|_width)?\s*\(\s*(?:enable|disable|full|[0-9]+)\s*\)\s*)+"
    if (declared is None or re.search(r"[;{}#]", clean[:declared.start()])
            or any(not re.fullmatch(loop_pragma, directive) for directive in directives)):
        raise NetworkError("Patches and recipes may change only the complete function definition.")
    _, span = engine._find(source, names)
    if span is None or source[:span[0]].strip() or source[span[1]:].strip():
        raise NetworkError("Patches and recipes may change only the complete function definition.")
    return source


def brief(net, agent, slug, function):
    """Return a bounded source brief and revision while allowing unrelated units to build."""
    engine = net.forks
    proj, _, _ = engine.get(slug)
    with _locked(engine, slug, function, read=True):
        fn, source, revision = snapshot(engine, slug, function)
        return (f"{fn['name']}: {fn['status']}, best score {fn['best_score']} (0 = identical)\n"
                f"revision: {revision}\n```c\n{source.strip()}\n```\n"
                "Use compile_patch with this revision. get_function returns assembly and notes; last_diff returns the full diff.") + hints(net, fn) + pattern_hints(net, fn)


def compact(result):
    # Keep the verdict and compiler diagnostics; large assembly diffs remain available through last_diff.
    text = result["text"]
    if len(text) > 2400:
        text = text[:2400] + "\n… Use last_diff for the full comparison."
    return text


def compile_patch(net, agent, slug, function, revision, edits):
    """Check exact edits under the unit lock and submit them to the ordinary judge."""
    engine = net.forks
    proj, fp, _ = engine.get(slug)
    with _locked(engine, slug, function):
        fn, source, current = snapshot(engine, slug, function)
        if revision != current:
            raise NetworkError("Stale revision: get function_brief again before editing.")
        source = definition(engine, fn, patch(source, edits))
        _, fp, _ = engine.get(slug)
        unit = fp.unit(fn["unit"])
        text = fp.safe_path(unit.source).read_text(encoding="utf-8")
        if engine._baseline(proj, fp, unit, text)[0] is None:
            raise NetworkError("Cannot establish the unit's existing matches; fix the baseline build/diff first.")
        result = engine.attempt(agent, slug, function, source)
        return compact(result) + "\nrevision: " + snapshot(engine, slug, function)[2]


def try_recipes(net, agent, slug, function, revision, recipes, apply=False):
    """Evaluate named patch sets against one baseline; only a strictly better winner may be applied.

    Explicit patch sets work across compilers and avoid silently applying CPU-specific flags to unrelated games.
    A recipe is {name, edits: [{old, new}]}, or an x86 preset {name: "btver2"}/{name: "no-sse"}.
    Unchanged candidates and duplicate names are rejected up front.
    """
    if not isinstance(apply, bool):
        raise NetworkError("apply must be true or false.")
    if not isinstance(recipes, list) or not 1 <= len(recipes) <= MAX_TRIALS:
        raise NetworkError(f"Send between 1 and {MAX_TRIALS} recipes.")
    engine = net.forks
    proj, fp, _ = engine.get(slug)
    with _locked(engine, slug, function):
        fn, source, current = snapshot(engine, slug, function)
        if revision != current:
            raise NetworkError("Stale revision: get function_brief again before trying recipes.")
        variants, names, seen_sources = [], set(), set()
        for recipe in recipes:
            if not isinstance(recipe, dict) or set(recipe) not in ({"name", "edits"}, {"name"}):
                raise NetworkError("Each recipe needs name and edits.")
            name = recipe["name"]
            # one line: a name starts its row, and a second line could pass for the applied winner's verdict, which
            # the API helper reads to tell a kept recipe from a preview (agents/api.py APPLIED)
            if (not isinstance(name, str) or not name.strip() or len(name) > 80 or name in names
                    or "\n" in name or "\r" in name):
                raise NetworkError("Recipe names must be unique nonempty single lines of at most 80 characters.")
            names.add(name)
            if "edits" in recipe:
                candidate = patch(source, recipe["edits"])
            else:
                arch = processor(fp, fp.unit(fn["unit"]), function, proj["arch"])
                candidate = x86.compiler_recipe(name, source) if arch in {"x86", "x86_64"} else None
                if candidate is None:
                    raise NetworkError("Unknown recipe: supply edits, or use btver2/no-sse on an x86 project.")
            candidate = definition(engine, fn, candidate)
            if candidate == source:
                raise NetworkError("A recipe must change the definition.")
            variants.append((name, candidate, 'edits' not in recipe))
        started = time.monotonic()
        baseline = engine._attempt(agent, slug, function, source, probe=True)
        experiments.record(net, fn, agent, revision, 'baseline', source, baseline, time.monotonic() - started)
        if baseline["score"] is None:
            return "Baseline could not be judged; no recipes applied.\n" + compact(baseline)
        best, winner = baseline["score"], None
        rows = ["Baseline: " + compact(baseline)]
        suggestions = {item['name']: item for item in x86.recipe_suggestions(source, baseline.get('rows', []),
                                                                                 _donor_sources(net, slug, fn))}
        observed = {}
        if baseline.get('output_fingerprint'):
            observed[baseline['output_fingerprint']] = 'baseline'
        for name, candidate, preset in variants:
            if preset and name not in suggestions:
                rows.append(f"{name}: skipped; current mismatch evidence does not support this preset. "
                            "Inspect blocks and supply an explicit scoped edit for a different hypothesis.")
                continue
            if candidate in seen_sources:
                rows.append(f"{name}: duplicate source in this batch; no repeated build.")
                continue
            seen_sources.add(candidate)
            started = time.monotonic()
            result = engine._attempt(agent, slug, function, candidate, probe=True)
            experiment_id = experiments.record(net, fn, agent, revision, name, candidate, result, time.monotonic() - started)
            rows.append(f"E{experiment_id} {name}: {compact(result)}")
            output = result.get('output_fingerprint')
            if output and output in observed:
                rows.append(f"Same compiled function output as {observed[output]} (code/relocations/local data); "
                            "this is not a correctness verdict. Try a different hypothesis.")
            elif output:
                observed[output] = name
            if result["score"] is not None and result["score"] < best:
                best, winner = result["score"], (name, candidate)
            if result.get("matched"):
                rows.append(f"{name} matches exactly; the remaining recipes cannot beat it.")
                break
        if snapshot(engine, slug, function)[2] != revision:
            raise NetworkError("Project dependencies changed during trials; no winner applied. Refresh function_brief.")
    # Reacquire the unit's current locks and recheck its inputs after releasing the batch lock.
    if winner is None:
        rows.append("No strictly better candidate; source unchanged.")
    elif apply:
        rows.append(f"Applying {winner[0]} through the normal judge:\n" + compile_patch(
            net, agent, slug, function, revision, [{'old': source, 'new': winner[1]}]))
        return "\n".join(rows)
    else:
        rows.append(f"Best: {winner[0]}. Preview only; repeat with apply=true to judge and keep it.")
    rows.append("revision: " + revision)
    return "\n".join(rows)


def suggest_recipes(net, agent, slug, function):
    """Use a stored diff to suggest bounded experiments; a named preset is re-gated on a fresh preview later."""
    engine = net.forks
    # under the unit's read lock, as brief: a preview writes each candidate into the source while it builds it
    with _locked(engine, slug, function, read=True):
        fn, source, revision = snapshot(engine, slug, function)
    fn = engine._fn(slug, function)
    kept = net._last_diffs.get((agent.id, fn['id']))
    if not kept:
        return 'No compiled mismatch evidence for this owner yet. Inspect blocks, then compile one candidate first.'
    items = x86.recipe_suggestions(source, kept[0], _donor_sources(net, slug, fn))
    if not items:
        return 'No supported scheduling or integer-copy mismatch pattern; use an explicit evidence-backed recipe.'
    return ('Advisory proposals from your last diff; try_recipes rechecks evidence against its fresh baseline.\n'
            + '\n'.join(f"{item['name']}: {item['reason']}; {item['donor_observations']} retrieved donor(s) show the attribute (observation only)." for item in items)
            + '\nrevision: ' + revision
            + '\nCheck experiment_history before repeating them; local ISA/ABI effects still require review.')


def _donor_sources(net, slug, fn):
    """Reuse bounded matched-donor retrieval; observations cannot turn an unsupported preset into a proposal."""
    if not hasattr(net.store, 'all'):
        return []
    try:
        _, _, hits = pattern_library(net).find(slug, fn['name'], 2)
        return [entry.source for _, entry in hits if entry.source]
    except (NetworkError, OSError, ValueError):
        return []

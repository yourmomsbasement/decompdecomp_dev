"""Big functions in parts: one part of a function's C at a time, judged as the whole function always is.

A big function's draft is 10-25 KB of C, more than one compile_and_diff a model with a small output limit can send,
and its assembly takes most of a small context: on a DS game at 58%, 25 of the 29 functions over 400 instructions
were open (40% of the code left), released as "too big to transcribe in one go" (ROADMAP.md, "Big functions for
every model"). Here a function's target is cut into parts of 60-240 instructions at block boundaries (`carve`), the
C marks each part with comments (`/* part 3 */ ... /* end part 3 */`, no effect on the code), and compile_part
replaces one part's C, builds the unit and judges the whole function as compile_and_diff does; the answer is that
part's rows of the diff and how many rows differ in each part. Each call carries 100-200 instructions' worth.

Several agents can hold parts of one function (claim_part). The compiler allocates registers over the whole
function, so a change to one part can move another's code: while someone else works on the function, a part's
change is kept only when the function's score gets no worse and no other part's differing rows grow (a preview
first, the ordinary judge after). Parts held by others keep their text: a whole-definition compile that changes them
is refused (`guard`).

Source-tree projects only: a ROM project's candidate lives in no file, so there's no current definition a part can
be replaced in (the same transaction compile_patch waits for there; ROADMAP.md).
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from ..arch.blocks import control
from ..forks.objdiff import LEGEND
from ..network import NetworkError
from ..scoring import render_rows

LARGE = 400  # instructions: a big function (its brief says how big ones match; it can be worked on in parts)
PART_MIN = 60  # instructions in a part, at least (the last may be shorter), ...
PART_TARGET = 150  # ... cut at the first block boundary after this many, ...
PART_MAX = 240  # ... and never more, even in one long block
PART_TTL = 30 * 60  # a part claim's lease, renewed by each compile_part
MAX_PART_CODE = 16 * 1024
BIG_DONOR_CHARS = 32_000  # a matched look-alike's C shown whole in a big function's brief, up to this
ROWS_KEPT = 256  # function states whose per-part rows are remembered (the shared guard's baseline)
# a big function's release goes through after this much work on the claim (release_owed), whichever comes first:
BIG_RELEASE_COMPILES = 20  # compiles, ...
BIG_RELEASE_SECONDS = 30 * 60  # ... this long since the claim, with compiles in it, ...
# ... or a compile_part of every part
_OPEN = re.compile(r"/\*\s*part\s+(\d+)\s*\*/")
_CLOSE = re.compile(r"/\*\s*end\s+part\s+(\d+)\s*\*/")
_CALLS = {"call", "callq", "bl", "blx", "jal", "bla"}
MARKERS = ("Mark each part in the C with comments around the code that makes it: /* part N */ ... /* end part N */ "
           "(no effect on the code: marking parts in the current C keeps its score). Markers go between statements, "
           "and a part's C stays in the function's scope: no braces around a part, so variables it declares stay "
           "visible to later parts. Then compile_part(function, part=N, code=...) replaces only that part's C and "
           "answers with its rows of the diff.")


@dataclass(frozen=True)
class Part:
    number: int
    first: int  # index of its first instruction in the function's target
    last: int  # index after its last instruction
    start: int  # target offset of its first instruction
    end: int  # target offset of its last instruction
    kind: str

    @property
    def count(self) -> int:
        return self.last - self.first


def _ends_flow(processor: str, code: list, i: int) -> bool:
    """Whether code[i] is reached only by branches: what comes before it never falls through (a switch's case after
    the previous case's jump, a block after a return)."""
    if i < 1:
        return False
    if processor == "mips":  # the branch is before the delay slot
        if i < 2:
            return False
        prev = code[i - 2]
        return prev.op in {"j", "jr", "b"}
    prev = code[i - 1]
    op = prev.op
    if processor in {"x86", "x86_64"}:
        return op in {"jmp", "ud2", "hlt"} or op.startswith("ret")
    if processor == "ppc":
        return op in {"b", "blr", "bctr", "rfi"}
    if processor in {"arm", "thumb"}:
        return op in {"b", "bx"} or (op.startswith(("pop", "ldm")) and bool(re.search(r"\bpc\b", prev.text)))
    return False


def _dispatches(processor: str, code: list) -> bool:
    """Whether a part holds an indirect jump: a switch's dispatch through a jump table."""
    for ins in code:
        if processor in {"x86", "x86_64"} and ins.op == "jmp" and ins.target is None:
            return True
        if processor == "mips" and ins.op == "jr" and "ra" not in ins.text:
            return True
        if processor == "ppc" and ins.op == "bctr":
            return True
        if processor in {"arm", "thumb"} and (re.match(r"(add|ldr)\w*\s+pc\b", ins.text) or ins.op in {"tbb", "tbh"}):
            return True
    return False


def carve(code: list, processor: str) -> list[Part]:
    """A function's target cut into parts at block boundaries: from PART_MIN instructions on at a block only branches
    reach (a case), from PART_TARGET on at any block, and at PART_MAX in a block longer than that. Cuts follow the
    target's own blocks, so a part is code the C writes in one place more often than not."""
    n = len(code)
    if not n:
        return []
    index = {ins.offset: i for i, ins in enumerate(code)}
    leaders = set()
    for i, ins in enumerate(code):
        if ins.target is not None and ins.target in index:
            leaders.add(index[ins.target])
        if control(processor, ins.op):
            leaders.add(i + (2 if processor == "mips" else 1))
    cuts, start, waiting = [], 0, None  # waiting: the last block start since `start` not cut at
    for i in [*sorted(x for x in leaders if 0 < x < n), n]:
        if i - start > PART_MAX and waiting is not None and waiting - start >= PART_MIN:
            cuts.append(waiting)
            start = waiting
        while i - start > PART_MAX:  # one long block: no boundary to cut at
            start += PART_TARGET
            cuts.append(start)
        if i == n:
            break
        size = i - start
        if size >= PART_TARGET or (size >= PART_MIN and _ends_flow(processor, code, i)):
            cuts.append(i)
            start, waiting = i, None
        else:
            waiting = i
    if cuts and n - cuts[-1] < PART_MIN // 2:  # a short tail goes with the part before it
        cuts.pop()
    bounds = [0, *cuts, n]
    parts = []
    for k, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
        kind = ("entry" if a == 0 else "reached only by branches" if _ends_flow(processor, code, a) else "block")
        if _dispatches(processor, code[a:b]):
            kind += ", switch dispatch"
        parts.append(Part(k, a, b, code[a].offset, code[b - 1].offset, kind))
    return parts


def marked(definition: str) -> dict[int, tuple[int, int]]:
    """part -> (start, end) of the C between its markers in a definition."""
    opens, closes = {}, {}
    for pattern, found in ((_OPEN, opens), (_CLOSE, closes)):
        for m in pattern.finditer(definition):
            n = int(m.group(1))
            if n in found:
                raise NetworkError(f"part {n} is marked twice: each part has one /* part {n} */ and one "
                                   f"/* end part {n} */")
            found[n] = m.end() if pattern is _OPEN else m.start()
    if set(opens) != set(closes):
        n = min(set(opens) ^ set(closes))
        raise NetworkError(f"part {n} has {'no /* end part */' if n in opens else 'an end but no /* part */'} marker")
    spans = {n: (opens[n], closes[n]) for n in opens}
    ordered = sorted(spans.values())
    if any(a > b for a, b in ordered) or any(b > c for (_, b), (c, _) in zip(ordered, ordered[1:])):
        raise NetworkError("parts must not overlap or nest: each /* part N */ ends with its /* end part N */ before "
                           "the next part starts")
    return spans


def replace_part(definition: str, part: int, code: str) -> str:
    spans = marked(definition)
    if part not in spans:
        raise NetworkError(f"part {part} isn't marked in the current C ({_marked_text(spans)}). {MARKERS}")
    if not isinstance(code, str) or len(code) > MAX_PART_CODE:
        raise NetworkError(f"send the part's C as text, at most {MAX_PART_CODE // 1024} KB")
    if _OPEN.search(code) or _CLOSE.search(code):
        raise NetworkError("send only the C inside the part, without its markers")
    a, b = spans[part]
    indent = definition[definition.rfind("\n", 0, b) + 1:b]
    tail = indent if not indent.strip() else ""
    body = code.strip("\n").rstrip()
    return definition[:a] + "\n" + (body + "\n" if body else "") + tail + definition[b:]


def outline(definition: str, keep: int | None = None) -> str:
    """A definition with the C of every part but `keep` folded to a line count: what a part's holder needs around it."""
    spans = marked(definition)
    out, at = [], 0
    for n, (a, b) in sorted(spans.items(), key=lambda item: item[1]):
        out.append(definition[at:a])
        inner = definition[a:b]
        lines = sum(1 for line in inner.splitlines() if line.strip())
        out.append(inner if n == keep else f" /* {lines} line{'s' * (lines != 1)} */ ")
        at = b
    out.append(definition[at:])
    return "".join(out)


def _marked_text(spans: dict) -> str:
    return f"marked: {', '.join(map(str, sorted(spans)))}" if spans else "no parts are marked yet"


def rows_by_part(code: list, parts: list[Part], rows: list | None) -> dict[int, list] | None:
    """part -> its diff rows (an extra instruction of yours goes with the target instruction after it). None when the
    rows aren't of this target (a compile against an older one)."""
    if not rows:
        return None
    from .block_inspection import localized_rows

    local, note = localized_rows(code, rows)
    if note:
        return None
    return {p.number: [row for ins in code[p.first:p.last] for row in local.get(ins.offset, [])] for p in parts}


def _render(rows: list) -> str:
    return render_rows(rows, 2, 80, legend=LEGEND)


def differing(rows: list) -> int:
    return sum(row[0] != " " for row in rows)


def moved_parts(before: dict[int, list] | None, after: dict[int, list] | None, part: int) -> list[str]:
    """Other parts whose differing rows grew between two compiles: what the shared guard refuses."""
    if before is None or after is None:
        return []
    return [f"part {n} ({differing(before[n])} → {differing(after[n])} differing rows)"
            for n in sorted(after) if n != part and differing(after[n]) > differing(before.get(n, []))]


def summary(by_part: dict[int, list] | None, before: dict[int, list] | None = None) -> str:
    if by_part is None:
        return "Differing rows per part: unavailable (the diff isn't of the current target)."
    cells = []
    for n in sorted(by_part):
        now = differing(by_part[n])
        was = differing(before[n]) if before and n in before else None
        cells.append(f"{n}: {now}" + (" ✓" if not now else "") + (f" (was {was})" if was is not None and was != now else ""))
    return "Differing rows per part: " + " | ".join(cells)


# --------------------------------------------------------------------------------------------------- briefs

def repeated_calls(lines: list[str], least: int = 4) -> list[tuple[str, int]]:
    """Callees a function calls `least` times or more, most first: the runs of the same call with other numbers that
    matched big functions write as one-letter macros."""
    counts: dict[str, int] = {}
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower().rstrip(".") in _CALLS:
            callee = parts[1].split("#")[0].strip()
            counts[callee] = counts.get(callee, 0) + 1
    return sorted(((c, n) for c, n in counts.items() if n >= least), key=lambda item: (-item[1], item[0]))


def file_macros(text: str | None, limit: int = 20) -> list[str]:
    """The #define lines of a source file (or of matched code): macros agents wrote for big functions."""
    found = []
    for m in re.finditer(r"(?m)^[ \t]*#[ \t]*define[ \t]+[^\n]+", text or ""):
        line = m.group().strip()
        if line not in found:
            found.append(line[:160])
        if len(found) >= limit:
            break
    return found


def big_lines(n_insns: int, lines: list[str], macros: list[str], parts: bool) -> list[str]:
    """What a big function's brief adds: how such functions matched, with this one's repeated calls and the file's
    macros. A 2,793-instruction switch on a DS game matched in 2 compiles as 125 lines of C, and a 1,272-instruction
    one by a free model, in 2 compiles too: their cases were long runs of the same calls with other numbers, written
    as one-letter macros (`#define A(x) func_0206d4f8(x, 1)`)."""
    if n_insns < LARGE:
        return []
    out = ["", f"### A big function ({n_insns} instructions): how big ones matched",
           "They were written short: each run of the same call with other numbers became a one-letter macro "
           "(`#define A(x) func_X(x, 1)`), each case a line or two, so the whole function fits in one compile."
           + (" Or work a part at a time: list_parts shows the parts, compile_part sends one part's C." if parts else "")]
    calls = repeated_calls(lines)
    if calls:
        out.append("Calls that repeat in its code (a macro each shortens the C): "
                   + ", ".join(f"{c} ×{n}" for c, n in calls[:8]))
    if macros:
        out += ["Macros already defined (in its file, or by matched code):", "```c", *macros, "```"]
    return out


def donor_fits(source: str | None, n_insns: int, small: int) -> bool:
    """Whether a matched look-alike's (or twin's) C goes into a brief: under `small` characters as before, or whatever
    its length in a big function's brief, where the look-alike is the best start there is: two of a DS game's untried
    big functions had a matched look-alike their briefs never showed (it was over 3,000 characters)."""
    return bool(source) and (len(source) < small or (n_insns >= LARGE and len(source) <= BIG_DONOR_CHARS))


# ----------------------------------------------------------------------------------------------- the target

def target_parts(net, slug: str, fn) -> tuple[list, list[Part]]:
    from .block_inspection import _target

    proj = net._project(slug)
    code, obj = _target(net, proj, fn)
    processor = obj.arch if obj is not None and obj.arch else proj["arch"]
    return code, carve(code, processor)


def _definition(engine, slug: str, fn) -> str | None:
    """The function's definition as its source file has it now (no revision: briefs and offers)."""
    _, fp, _ = engine.get(slug)
    unit = fp.unit(fn["unit"])
    if not unit.source:
        return None
    try:
        text = fp.safe_path(unit.source).read_text(encoding="utf-8")
    except OSError:
        return None
    _, span = engine._find(text, engine._names(engine._meta(fn), fn))
    return text[span[0]:span[1]] if span else None


def _holders(net, fn) -> dict[int, int]:
    """part -> agent id of the live part claims on a function (expired ones go)."""
    with net.store.tx() as db:
        db.execute("DELETE FROM part_claims WHERE expires_at < ?", (time.time(),))
        return {r["part"]: r["agent_id"] for r in
                db.execute("SELECT part, agent_id FROM part_claims WHERE function_id=?", (fn["id"],))}


def _owner(net, fn) -> int | None:
    with net.store.tx() as db:
        row = db.execute("SELECT agent_id FROM claims WHERE function_id=? AND expires_at >= ?",
                         (fn["id"], time.time())).fetchone()
    return row["agent_id"] if row else None


def _rows_cache(net) -> OrderedDict:
    cache = getattr(net, "_part_rows", None)
    if cache is None:
        cache = net._part_rows = OrderedDict()
    return cache


def _state(net, fn, definition: str) -> tuple:
    """A function's state as far as its rows go: its definition, the rest of its file (a static helper there can be
    inlined into it) and the project's generation (a header, a name or the compiler changed: every compile's result
    is older than that; an edit of a source file doesn't bump it)."""
    try:
        _, fp, _ = net.forks.get(net.store.one("SELECT slug FROM projects WHERE id=?", fn["project_id"])["slug"])
        text = fp.safe_path(fp.unit(fn["unit"]).source).read_bytes()
    except (NetworkError, OSError, TypeError, AttributeError):
        text = b""
    return (fn["id"], net._generation.get(fn["project_id"], 0), hashlib.sha256(definition.encode()).hexdigest(),
            hashlib.sha256(text).hexdigest())


def _remember_rows(net, fn, definition: str, score, rows) -> None:
    cache = _rows_cache(net)
    cache[_state(net, fn, definition)] = (score, list(rows))
    while len(cache) > ROWS_KEPT:
        cache.popitem(last=False)


def _known_rows(net, agent, fn, definition: str | None):
    """(score, rows, fresh) of the function as it is: a compile_part of this exact definition, else (not fresh) the
    last compile of this agent or the function's owner."""
    if definition is not None:
        hit = _rows_cache(net).get(_state(net, fn, definition))
        if hit:
            return hit[0], hit[1], True
    for who in (agent.id, _owner(net, fn)):
        kept = net._last_diffs.get((who, fn["id"])) if who is not None else None
        if kept:
            return None, kept[0], False
    return None, None, False


# ------------------------------------------------------------------------------------------------- the tools

def _parts_table(net, agent, slug, fn, definition, code, parts) -> list[str]:
    try:
        spans = marked(definition) if definition else {}
    except NetworkError as e:
        spans, broken = {}, str(e)
    else:
        broken = ""
    holders = _holders(net, fn)
    _, rows, fresh = _known_rows(net, agent, fn, definition)
    by_part = rows_by_part(code, parts, rows)
    lines = []
    for p in parts:
        who = holders.get(p.number)
        cells = [f"part {p.number}: +0x{p.start:x}..+0x{p.end:x}, {p.count} instructions ({p.kind})",
                 "marked in the C" if p.number in spans else "not marked"]
        if by_part is not None:
            cells.append(f"{differing(by_part[p.number])} differing rows" + ("" if fresh else " at the last compile"))
        if who is not None:
            cells.append("yours" if who == agent.id else f"held by {net._agent_name(who)}")
        lines.append("- " + "; ".join(cells))
    if broken:
        lines.append(f"The C's part markers can't be read: {broken}")
    return lines


def list_parts(net, agent, slug: str, function: str, part: int = 0) -> str:
    engine = net.forks
    fn = engine._fn(slug, function)
    code, parts = target_parts(net, slug, fn)
    if not parts:
        return f"No readable target instructions for {fn['name']}: it can't be cut into parts."
    if part:
        return _part_brief(net, agent, slug, fn, int(part), code, parts)
    definition = _definition(engine, slug, fn)
    lines = [f"{fn['name']}: {len(code)} target instructions in {len(parts)} parts (cut at the target's blocks)",
             *_parts_table(net, agent, slug, fn, definition, code, parts), "", MARKERS,
             "list_parts(function, part=N) shows one part's assembly, rows and C. Others can work on parts at the same "
             "time: claim_part(function) takes a free marked part."]
    if definition is None:
        lines.append(f"{fn['name']} has no definition yet: compile_and_diff a skeleton first (the prologue, its "
                     "branches or switch with empty parts, the epilogue), with the part markers in it.")
    return "\n".join(lines)


def _part_brief(net, agent, slug, fn, number: int, code, parts) -> str:
    engine = net.forks
    p = next((x for x in parts if x.number == number), None)
    if p is None:
        raise NetworkError(f"{fn['name']} has parts 1-{len(parts)}")
    definition = _definition(engine, slug, fn)
    _, rows, fresh = _known_rows(net, agent, fn, definition)
    by_part = rows_by_part(code, parts, rows)
    lines = [f"## {fn['name']}, part {p.number} of {len(parts)}: +0x{p.start:x}..+0x{p.end:x}, {p.count} "
             f"instructions ({p.kind})", "", "### Its target assembly", "```asm",
             *(f"+0x{ins.offset:x}: {ins.text}" for ins in code[p.first:p.last]), "```"]
    if by_part is not None:
        mine = by_part[p.number]
        lines += ["", f"### Its rows of the diff ({'now' if fresh else 'at the last compile'}; "
                      f"{differing(mine)} differ)", _render(mine) if differing(mine) else "all equal",
                  summary(by_part)]
    if definition is None:
        lines += ["", f"{fn['name']} has no definition yet: compile_and_diff a skeleton with the part markers first."]
    else:
        try:
            spans = marked(definition)
            shown = outline(definition, p.number) if spans else definition
        except NetworkError as e:
            spans, shown = {}, definition
            lines.append(f"The C's part markers can't be read: {e}")
        lines += ["", f"### The C ({'every other part folded' if spans else 'no parts marked yet'})", "```c",
                  shown.strip(), "```"]
        if p.number not in spans:
            lines.append(f"Part {p.number} isn't marked yet. {MARKERS}")
    lines += ["", f"compile_part(function=\"{fn['name']}\", part={p.number}, code=...) sends this part's C; the whole "
                  "function is judged as always. Declarations, types and macros go in the file with edit_file."]
    return "\n".join(lines)


def claim_part(net, agent, slug: str, function: str, part: int = 0) -> str:
    """Hold one marked part of a function someone (or no one) works on: compile_part may change it, nobody else may."""
    engine = net.forks
    fn = engine._fn(slug, function)
    if fn["status"] != "open":
        raise NetworkError(f"{fn['name']} is {fn['status']}: claim_function another")
    definition = _definition(engine, slug, fn)
    if definition is None:
        raise NetworkError(f"{fn['name']} has no definition yet: its skeleton comes first (claim_function it, "
                           "compile_and_diff the skeleton with part markers)")
    spans = marked(definition)
    if not spans:
        raise NetworkError(f"No parts are marked in {fn['name']} yet: whoever holds it (or you, with claim_function) "
                           f"marks them first. {MARKERS}")
    code, parts = target_parts(net, slug, fn)
    numbers = {p.number for p in parts}
    now = time.time()
    with net.store.tx() as db:
        db.execute("DELETE FROM part_claims WHERE expires_at < ?", (now,))
        other = db.execute("SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=? "
                           "AND c.function_id != ?", (agent.id, fn["id"])).fetchone()
        if other is not None:
            raise NetworkError(f"you hold {other['name']}: match or release_function it before taking a part of "
                               f"{fn['name']}")
        holders = {r["part"]: r["agent_id"] for r in
                   db.execute("SELECT part, agent_id FROM part_claims WHERE function_id=?", (fn["id"],))}
        free = [n for n in sorted(spans) if n in numbers and holders.get(n) in (None, agent.id)]
        if part:
            if part not in spans or part not in numbers:
                raise NetworkError(f"part {part} isn't one of {fn['name']}'s marked parts ({_marked_text(spans)})")
            if holders.get(part) not in (None, agent.id):
                raise NetworkError(f"part {part} is held by {net._agent_name(holders[part])}; free: "
                                   + (", ".join(map(str, free)) or "none"))
            chosen = part
        else:
            if not free:
                raise NetworkError(f"every marked part of {fn['name']} is held: claim_function another")
            _, rows, _ = _known_rows(net, agent, fn, definition)
            by_part = rows_by_part(code, parts, rows)
            chosen = max(free, key=lambda n: (differing(by_part[n]) if by_part else 0, -n))
        db.execute("DELETE FROM part_claims WHERE agent_id=?", (agent.id,))
        db.execute("INSERT INTO part_claims VALUES(?,?,?,?,?)", (fn["id"], chosen, agent.id, now, now + PART_TTL))
        net._event(db, fn["project_id"], agent.id, "claim_part", f"{fn['name']} part {chosen}")
    return "CLAIMED " + _part_brief(net, agent, slug, fn, chosen, code, parts)


def release_part(net, agent, slug: str, function: str, notes: str = "") -> str:
    engine = net.forks
    fn = engine._fn(slug, function)
    with net.store.tx() as db:
        held = [r["part"] for r in db.execute("SELECT part FROM part_claims WHERE function_id=? AND agent_id=?",
                                              (fn["id"], agent.id))]
        db.execute("DELETE FROM part_claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))
    if not held:
        raise NetworkError(f"you hold no part of {fn['name']}")
    if notes.strip():
        net.add_note(agent, slug, f"part {held[0]}: {notes.strip()}", fn["name"])
    return f"Released part {held[0]} of {fn['name']}" + (" with your note." if notes.strip() else ".")


def _head(text: str) -> str:
    """A compile's verdict without its whole-function diff (the part's rows replace it)."""
    if text.startswith(("❌ BUILD", "BUILD ERROR")) or "MATCH" in text.split("\n", 1)[0]:
        return text[:3000]
    return text.split("\n", 1)[0]


def compile_part(net, agent, slug: str, function: str, part: int, code: str) -> str:
    from . import compile_tools

    if isinstance(part, str) and part.strip().isdigit():
        part = int(part)
    if not isinstance(part, int) or part < 1:
        raise NetworkError("part is a part's number (list_parts shows them)")
    engine = net.forks
    proj, _, _ = engine.get(slug)
    with engine._tree(proj).exclusive():
        fn, definition, _ = compile_tools.snapshot(engine, slug, function)
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} already matches")
        new = compile_tools.definition(engine, fn, replace_part(definition, part, code))
        if new == definition:
            raise NetworkError(f"that is part {part}'s C as it is: change something")
        holders, owner = _holders(net, fn), _owner(net, fn)
        if holders.get(part) not in (None, agent.id):
            raise NetworkError(f"part {part} is held by {net._agent_name(holders[part])}")
        if holders.get(part) != agent.id and owner not in (None, agent.id):
            raise NetworkError(f"{fn['name']} is claimed by {net._agent_name(owner)}: claim_part(function, "
                               f"part={part}) first, if it's free")
        if holders.get(part) == agent.id:
            with net.store.tx() as db:
                db.execute("UPDATE part_claims SET expires_at=? WHERE function_id=? AND part=?",
                           (time.time() + PART_TTL, fn["id"], part))
        shared = owner not in (None, agent.id) or any(a != agent.id for a in holders.values())
        target, parts = target_parts(net, slug, fn)
        before_score, before_rows, fresh = _known_rows(net, agent, fn, definition)
        if shared and not fresh:
            base = engine._attempt(agent, slug, function, definition, probe=True, part=part)
            if base["score"] is None:
                return f"The function as it is now can't be judged, so nothing changed: {_head(base['text'])}"
            before_score, before_rows = base["score"], base.get("rows") or []
            _remember_rows(net, fn, definition, before_score, before_rows)
        if not shared and not fresh:
            # alone on it: the agent's own last compile stands for the function as it is (no extra build)
            last = net.store.one("SELECT score FROM attempts WHERE function_id=? AND agent_id=? AND score IS NOT NULL "
                                 "ORDER BY id DESC LIMIT 1", fn["id"], agent.id)
            before_score = last["score"] if last else None
        before = rows_by_part(target, parts, before_rows)
        earlier = definition[slice(*marked(definition)[part])].strip("\n")
        if shared:
            # someone else's work is in the same function: a preview first, kept only if it moves nobody's part
            trial = engine._attempt(agent, slug, function, new, probe=True, part=part)
            if trial["score"] is None:
                return f"Part {part} not kept: {_head(trial['text'])}"
            after = rows_by_part(target, parts, trial.get("rows"))
            moved = moved_parts(before, after, part)
            if trial["score"] > before_score or moved:
                why = (f"the function's score would go from {before_score} to {trial['score']}"
                       if trial["score"] > before_score else "it moves other parts' code: " + ", ".join(moved))
                mine = (after or {}).get(part, [])
                return "\n".join([f"Part {part} not kept: {why}. Others work on this function, so a part's change "
                                  "may not make it or their parts worse (the compiler allocates registers over the "
                                  "whole function: try another spelling of your part).", summary(after, before),
                                  f"Your part's rows in that preview ({differing(mine)} differ):",
                                  _render(mine) if differing(mine) else "all equal"])
        result = engine.attempt(agent, slug, function, new, part=part)
        with net.store.tx() as db:  # what a big function's release asks for (release_owed)
            net._event(db, fn["project_id"], agent.id, "compile_part", f"{fn['name']} part {part}")
        now_def = compile_tools.snapshot(engine, slug, function)[1]
        kept = net._last_diffs.get((agent.id, fn["id"]))
        rows = kept[0] if kept else None
        if now_def == new and result.get("score") is not None:
            _remember_rows(net, fn, new, result["score"], rows or [])
        after = rows_by_part(target, parts, rows)
    lines = [_head(result["text"])]
    if result.get("matched"):
        return lines[0]
    score = result.get("score")
    if score is not None and before_score is not None and score > before_score:
        # a function without a saved best source keeps a worse version in its file (ForkEngine._attempt): say so
        lines.append(f"Worse than before (score {before_score} → {score})"
                     + (". The file keeps your change; to go back, send part " + str(part) + "'s earlier C:\n```c\n"
                        + earlier + "\n```" if now_def == new and len(earlier) <= 4000 else
                        ". The file keeps your change." if now_def == new else "; the file keeps what it had."))
    if after is not None and result.get("score") is not None:
        mine = after[part]
        # the counts first: a long part's rows can be cut on the way to the agent
        lines += [summary(after, before), f"Part {part}'s rows ({differing(mine)} differ):",
                  _render(mine) if differing(mine) else "all equal"]
        moved = moved_parts(before, after, part)
        if moved and not shared:
            lines.append("Other parts moved with it: " + ", ".join(moved) + " (registers are allocated over the "
                         "whole function).")
    elif result.get("score") is not None:
        lines.append("Rows per part unavailable: last_diff has the whole function's diff.")
    return "\n".join(lines)


def guard(net, agent, fn, old: str, new: str) -> None:
    """A whole-definition compile (compile_and_diff, compile_patch...) must leave the parts other agents hold as they
    are: their text, between their markers. Called before the source file is written."""
    with net.store.tx() as db:
        held = {r["part"]: r["agent_id"] for r in db.execute(
            "SELECT part, agent_id FROM part_claims WHERE function_id=? AND agent_id != ? AND expires_at >= ?",
            (fn["id"], agent.id, time.time()))}
    if not held or not old:
        return
    try:
        was = marked(old)
    except NetworkError:
        return  # nothing readable to protect
    try:
        now = marked(new)
    except NetworkError:
        now = {}
    for n, who in sorted(held.items()):
        if n not in was:
            continue
        if n not in now or old[slice(*was[n])].strip() != new[slice(*now[n])].strip():
            raise NetworkError(f"part {n} of {fn['name']} is being worked on by {net._agent_name(who)} (claim_part): "
                               f"keep its markers and its C as they are, or wait until it's released")


def offer(net, slug: str, function: str) -> str:
    """For an agent that asked for a function someone else holds: the parts of it that are free, if it's in parts."""
    try:
        fn = net.forks._fn(slug, function)
        if fn["status"] != "open" or (fn["n_insns"] or 0) < LARGE:
            return ""
        definition = _definition(net.forks, slug, fn)
        spans = marked(definition) if definition else {}
    except (NetworkError, OSError, KeyError):
        return ""
    holders = _holders(net, fn)
    free = [n for n in sorted(spans) if n not in holders]
    if not free:
        return ""
    return (f"{fn['name']} is worked on in parts: parts {', '.join(map(str, free))} are free. "
            f"claim_part(function=\"{fn['name']}\") takes one.")


def waiting(net, slug: str) -> str:
    """For an agent with nothing to claim: big functions others hold whose marked parts are free."""
    proj = net._project(slug)
    rows = net.store.all("SELECT f.name FROM functions f JOIN claims c ON c.function_id=f.id WHERE f.project_id=? "
                         "AND f.status='open' AND f.n_insns>=? ORDER BY f.n_insns DESC LIMIT 20", proj["id"], LARGE)
    found = [text for r in rows if (text := offer(net, slug, r["name"]))]
    return ("\n" + "\n".join(found[:5])) if found else ""


def parts_compiled(net, fn, agent_id: int, since: float) -> set[int]:
    """The parts of `fn` this agent sent with compile_part since `since` (its claim)."""
    rows = net.store.all("SELECT detail FROM events WHERE agent_id=? AND kind='compile_part' AND detail LIKE ? AND "
                         "created_at >= ?", agent_id, f"{fn['name']} part %", since)
    return {int(r["detail"].rsplit(" ", 1)[1]) for r in rows if r["detail"].rsplit(" ", 1)[1].isdigit()}


def release_owed(net, slug: str, fn, agent, progress: dict) -> str | None:
    """What a big function's claim still owes before a release asked again goes through (None: nothing): a helper
    took a 501-instruction function of a PS4 game from 2% to 48% in seven compiles, rebuilt three of its six parts,
    and released it ten minutes after the claim, past a push-back that asked for one compile more."""
    if (fn["n_insns"] or 0) < LARGE:
        return None
    n = progress["compiles"]
    if n >= BIG_RELEASE_COMPILES or (n and time.time() - progress["claimed_at"] >= BIG_RELEASE_SECONDS):
        return None
    try:
        _, parts = target_parts(net, slug, fn) if net.fork_slug(slug) else (None, [])
    except (NetworkError, OSError, ValueError, KeyError, TypeError):
        parts = []
    numbers = {p.number for p in parts}
    left = sorted(numbers - parts_compiled(net, fn, agent.id, progress["claimed_at"]))
    if numbers and not left:
        return None
    minutes = max(0, int(BIG_RELEASE_SECONDS - (time.time() - progress["claimed_at"]))) // 60
    return (f"after {BIG_RELEASE_COMPILES} compiles ({n} so far), {BIG_RELEASE_SECONDS // 60} minutes on it "
            f"({minutes} to go)"
            + (f" or a compile_part of each of its {len(numbers)} parts (not yet: {', '.join(map(str, left))})"
               if numbers else "")
            + f": it's a big function ({fn['n_insns']} instructions), and half its structure is a start the next "
              "agent rebuilds rather than finishes, so build the rest, part by part.")


def report(net, slug: str) -> str:
    """How the big functions are doing: how many are tried, compiled and how far, and the compiles on them this week
    against the week before (what ROADMAP.md's "Big functions for every model" is measured by)."""
    proj = net._project(slug)
    rows = net.store.all("SELECT id, name, n_insns, status, best_score, attempts FROM functions WHERE project_id=? "
                         "AND n_insns>=? AND status != 'asm' ORDER BY n_insns DESC", proj["id"], LARGE)
    if not rows:
        return f"No functions of {LARGE} instructions or more."
    open_ = [r for r in rows if r["status"] == "open"]
    tried = [r for r in open_ if r["attempts"]]
    compiled = [r for r in open_ if r["best_score"] is not None]
    fork = bool(net.fork_slug(slug))
    now = time.time()
    week = 7 * 24 * 3600

    def compiles(since, until, via=None):
        return net.store.one("SELECT COUNT(*) AS n FROM attempts a JOIN functions f ON f.id=a.function_id WHERE "
                             "f.project_id=? AND f.n_insns>=? AND a.created_at>=? AND a.created_at<?"
                             + (" AND a.via=?" if via else ""), proj["id"], LARGE, since, until,
                             *([via] if via else []))["n"]

    lines = [f"Functions of {LARGE} instructions or more: {len(rows)}, {len(rows) - len(open_)} matched, "
             f"{len(open_)} open: {len(tried)} tried, {len(compiled)} compiled at least once, "
             f"{len(open_) - len(tried)} never tried.",
             f"Compiles on them: {compiles(now - week, now)} in the last 7 days ({compiles(now - week, now, 'part')} "
             f"with compile_part), {compiles(now - 2 * week, now - week)} in the 7 days before."]
    if fork and compiled:
        def pct(r):
            return max(0.0, 100.0 * (1 - r["best_score"] / (r["n_insns"] * 100)))
        best = sorted(compiled, key=pct, reverse=True)
        lines.append("Closest open ones: " + ", ".join(f"{r['name']} {pct(r):.0f}% ({r['n_insns']} insns)"
                                                       for r in best[:8]))
    held = net.store.one("SELECT COUNT(*) AS n, COUNT(DISTINCT p.function_id) AS f FROM part_claims p JOIN functions f "
                         "ON f.id=p.function_id WHERE f.project_id=? AND p.expires_at >= ?", proj["id"], now)
    if held["n"]:
        lines.append(f"{held['n']} parts of {held['f']} functions are held right now.")
    return "\n".join(lines)

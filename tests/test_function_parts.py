"""Big functions in parts (app/function_parts.py): the target cut at blocks, parts marked in the C, compile_part's
judge and the guard that keeps one agent's part from another's changes. Synthetic targets and verdicts: no compiler."""

import asyncio
import sqlite3
from contextlib import contextmanager
from threading import RLock
from types import SimpleNamespace

import pytest

from decompnet.agents.api import api_tool_schemas
from decompnet.app import function_parts as fparts
from decompnet.app import tools
from decompnet.app.forkengine import ForkEngine, _TreeLock
from decompnet.codeblocks import Instruction
from decompnet.network import NetworkError
from decompnet.store import SCHEMA, Store


def x86(ops: list[str]) -> list[Instruction]:
    """Instructions 4 bytes apart; `jmp N`/`jne N` jump to instruction N."""
    out = []
    for i, text in enumerate(ops):
        op, _, arg = text.partition(" ")
        target = int(arg) * 4 if op.startswith("j") and arg.isdigit() else None
        out.append(Instruction(i * 4, text, target))
    return out


# ------------------------------------------------------------------------------------------------- carving

def test_parts_cover_the_target_and_stay_within_bounds():
    ops = []
    for case in range(12):  # a switch: 12 cases of 45 instructions, each ending in a jump to the end
        ops += ["mov eax, 1"] * 44 + ["jmp 540"]
    ops += ["ret"]
    parts = fparts.carve(x86(ops), "x86_64")
    assert parts[0].first == 0 and parts[-1].last == len(ops)
    assert all(a.last == b.first for a, b in zip(parts, parts[1:]))
    assert all(p.count <= fparts.PART_MAX for p in parts)
    assert all(p.count >= fparts.PART_MIN for p in parts[:-1])
    # the cuts fall at the cases' starts (after a jump): code the C writes in one place
    assert all(fparts._ends_flow("x86_64", x86(ops), p.first) for p in parts[1:])
    assert parts[0].kind == "entry" and "branches" in parts[1].kind


def test_one_long_block_is_cut_at_the_part_size():
    parts = fparts.carve(x86(["add eax, 1"] * 700), "x86_64")
    assert [p.count for p in parts] == [150, 150, 150, 150, 100]


def test_a_short_tail_goes_with_the_part_before_it():
    ops = ["add eax, 1"] * 150 + ["jmp 0"] + ["add eax, 2"] * 10
    parts = fparts.carve(x86(ops), "x86_64")
    assert len(parts) == 1 and parts[0].count == len(ops)


def test_mips_cuts_after_the_delay_slot():
    ops = (["addiu $a0, $a0, 1"] * 100 + ["j func", "nop"]) * 3
    code = [Instruction(i * 4, t) for i, t in enumerate(ops)]
    parts = fparts.carve(code, "mips")
    assert all(code[p.first - 1].op == "nop" for p in parts[1:])


def test_a_switch_dispatch_is_named():
    ops = ["cmp eax, 5"] + ["ja 200"] + ["jmp qword ptr [rax*8 + table]"] + ["mov eax, 1"] * 300
    assert "switch dispatch" in fparts.carve(x86(ops), "x86_64")[0].kind


# ------------------------------------------------------------------------------------------------- markers

DEFINITION = """int work(int x) {
    /* part 1 */
    x = x + 1;
    /* end part 1 */
    /* part 2 */
    x = x * 2;
    /* end part 2 */
    return x;
}"""


def test_markers_are_read_and_a_part_is_replaced_alone():
    spans = fparts.marked(DEFINITION)
    assert set(spans) == {1, 2}
    new = fparts.replace_part(DEFINITION, 2, "x = x * 3;\nx -= 1;")
    assert "x = x + 1;" in new and "x = x * 3;\nx -= 1;" in new and "x * 2" not in new
    assert set(fparts.marked(new)) == {1, 2}
    assert new.endswith("return x;\n}")


@pytest.mark.parametrize("bad, why", [
    (DEFINITION.replace("/* end part 2 */", ""), "no /\\* end part"),
    (DEFINITION.replace("/* part 2 */", "/* part 1 */"), "marked twice"),
    ("int f(void) { /* part 1 */ /* part 2 */ /* end part 2 */ /* end part 1 */ }", "overlap or nest"),
])
def test_bad_markers_say_what_is_wrong(bad, why):
    with pytest.raises(NetworkError, match=why):
        fparts.marked(bad)


def test_part_code_with_markers_or_an_unknown_part_is_refused():
    with pytest.raises(NetworkError, match="without its markers"):
        fparts.replace_part(DEFINITION, 1, "/* part 3 */ x++; /* end part 3 */")
    with pytest.raises(NetworkError, match="isn't marked"):
        fparts.replace_part(DEFINITION, 3, "x++;")


def test_outline_folds_the_other_parts():
    shown = fparts.outline(DEFINITION, 2)
    assert "x * 2" in shown and "x + 1" not in shown and "/* 1 line */" in shown


# ------------------------------------------------------------------------------------------- rows per part

CODE = [Instruction(i * 4, t) for i, t in enumerate(["a0", "a1", "a2", "b0", "b1", "b2"])]
PARTS = [fparts.Part(1, 0, 3, 0, 8, "entry"), fparts.Part(2, 3, 6, 12, 20, "block")]


def rows(bad1: int, bad2: int) -> list[tuple]:
    out = []
    for i, ins in enumerate(CODE):
        wrong = (i < 3 and i < bad1) or (i >= 3 and i - 3 < bad2)
        out.append(("|" if wrong else " ", ins.text, ins.text + ("?" if wrong else "")))
    return out


def test_rows_go_to_their_parts_and_moves_are_named():
    before = fparts.rows_by_part(CODE, PARTS, rows(2, 0))
    after = fparts.rows_by_part(CODE, PARTS, rows(1, 2))
    assert [fparts.differing(before[n]) for n in (1, 2)] == [2, 0]
    assert fparts.moved_parts(before, after, 1) == ["part 2 (0 → 2 differing rows)"]
    assert "1: 1 (was 2)" in fparts.summary(after, before) and "2: 2 (was 0)" in fparts.summary(after, before)


def test_rows_of_another_target_are_not_mapped():
    assert fparts.rows_by_part(CODE, PARTS, [(" ", "zz", "zz")]) is None


# -------------------------------------------------------------------------------------------- big briefs

def test_big_lines_name_the_repeated_calls_and_the_macros():
    lines = ["call func_0206d4f8"] * 9 + ["call rarely"] * 2 + ["mov eax, 1"]
    text = "\n".join(fparts.big_lines(900, lines, fparts.file_macros("#define A(x) func_0206d4f8(x, 1)\nint y;"),
                                      parts=True))
    assert "func_0206d4f8 ×9" in text and "rarely" not in text
    assert "#define A(x) func_0206d4f8(x, 1)" in text and "compile_part" in text
    assert fparts.big_lines(100, lines, [], parts=True) == []


def test_a_big_function_brief_shows_a_long_lookalike():
    long = "int f(void) {\n" + "    g(1);\n" * 600 + "}"
    assert not fparts.donor_fits(long, 100, 3000)
    assert fparts.donor_fits(long, 900, 3000)
    assert not fparts.donor_fits("x" * (fparts.BIG_DONOR_CHARS + 1), 900, 3000)


# ---------------------------------------------------------------------------------------------- the tools

def score_of(source: str) -> tuple[int, int]:
    """(part 1's, part 2's differing rows) of a synthetic compile: a part matches when its C says so; `spill` in
    part 1 moves part 2's registers."""
    one = 0 if "x + 7" in source else 2
    two = 0 if "x * 3" in source else 2
    if "spill" in source:
        two += 1
    return one, two


@pytest.fixture
def rig(tmp_path, monkeypatch):
    path = tmp_path / "unit.c"
    path.write_text(DEFINITION + "\n")
    store = Store(":memory:")
    db = store.conn
    names = {7: "tester", 8: "other"}
    net = SimpleNamespace(store=store, _generation={}, _last_diffs={}, _agent_name=lambda i: names.get(i, "?"),
                          _event=lambda db, *a: None, _project=lambda slug: dict(id=1, slug=slug, arch="x86_64"),
                          fork_slug=lambda slug: slug, claim_ttl=300)
    net.add_note = lambda agent, slug, text, function: net.__dict__.setdefault("notes", []).append(text)
    db.execute("INSERT INTO functions(id, project_id, name, n_insns, status, unit) VALUES(1, 1, 'work', 900, 'open', 'unit')")
    engine = ForkEngine.__new__(ForkEngine)
    engine.net = net
    net.forks = engine
    unit = SimpleNamespace(name="unit", source="unit.c", complete=False, target=None)
    fp = SimpleNamespace(root=tmp_path, unit=lambda name: unit, safe_path=lambda rel, **kw: tmp_path / rel)
    engine.get = lambda slug: (dict(id=1, slug=slug), fp, {})
    engine._fn = lambda slug, function: dict(db.execute("SELECT * FROM functions WHERE id=1").fetchone())
    engine._meta = lambda fn: {}
    engine._names = lambda meta, fn: ["work"]
    tree = _TreeLock()
    engine._tree = lambda proj: tree
    monkeypatch.setattr(fparts, "target_parts", lambda net, slug, fn: (CODE, PARTS))
    state = SimpleNamespace(probes=[], kept=[])

    def judged(source):
        one, two = score_of(source)
        return 10 * (one + two), rows(one, two)

    def _attempt(agent, slug, function, source, probe=False, part=None, **kw):
        assert probe
        state.probes.append(source)
        score, r = judged(source)
        return dict(text=f"score {score}", score=score, matched=False, rows=r)

    def attempt(agent, slug, function, source, part=None, **kw):
        fn = engine._fn(slug, function)
        text = path.read_text()
        old = text[:text.rindex("}") + 1]
        fparts.guard(net, agent, fn, old, source)
        score, r = judged(source)
        net._last_diffs[(agent.id, 1)] = (r, "")
        path.write_text(source + "\n")
        state.kept.append((agent.id, part, source))
        return dict(text=f"{100 - score}% match (score {score}; 0 = identical).\nwhole diff here", score=score,
                    matched=score == 0)

    engine._attempt = _attempt
    engine.attempt = attempt
    yield SimpleNamespace(net=net, db=db, path=path, state=state, me=SimpleNamespace(id=7, name="tester"),
                          other=SimpleNamespace(id=8, name="other"))


def hold(rig, agent_id, part=None, function=True):
    if part is None:
        rig.db.execute("INSERT INTO claims VALUES(1, ?, 0, 1e12)", (agent_id,))
    else:
        rig.db.execute("INSERT INTO part_claims VALUES(1, ?, ?, 0, 1e12)", (part, agent_id))


def test_alone_a_part_is_judged_once_and_answers_with_its_rows(rig):
    hold(rig, 7)
    out = fparts.compile_part(rig.net, rig.me, "demo", "work", 1, "x = x + 7;")
    assert not rig.state.probes, "nobody else works on it: no preview"
    assert rig.state.kept[-1][1] == 1 and "x = x + 7;" in rig.path.read_text() and "x * 2" in rig.path.read_text()
    assert "Part 1's rows (0 differ)" in out and "whole diff here" not in out
    assert "Differing rows per part: 1: 0 ✓ | 2: 2" in out and "all equal" in out


def test_alone_a_worse_part_says_so_with_the_earlier_c(rig):
    """A function with no saved best source keeps a worse version in its file: the answer says so, with the part's
    earlier C to go back (found on a real PS4 function: 16752 → 17112, said nowhere)."""
    hold(rig, 7)
    fparts.compile_part(rig.net, rig.me, "demo", "work", 2, "x = x * 3;")  # better: score 20
    rig.db.execute("INSERT INTO attempts(function_id, agent_id, score) VALUES(1, 7, 20)")
    out = fparts.compile_part(rig.net, rig.me, "demo", "work", 2, "x = x * 4;")
    assert "Worse than before (score 20 → 40)" in out and "x = x * 3;" in out.split("earlier C")[1]


def test_with_others_on_it_a_change_that_moves_their_part_is_not_kept(rig):
    hold(rig, 8)  # someone else holds the function; this agent holds part 1
    hold(rig, 7, part=1)
    out = fparts.compile_part(rig.net, rig.me, "demo", "work", 1, "x = x + 7; spill();")
    assert "not kept" in out and "part 2 (2 → 3 differing rows)" in out
    assert not rig.state.kept and "x + 1" in rig.path.read_text()
    out = fparts.compile_part(rig.net, rig.me, "demo", "work", 1, "x = x + 7;")
    assert rig.state.kept[-1][:2] == (7, 1) and "1: 0 ✓" in out


def test_a_part_needs_its_claim_while_someone_holds_the_function(rig):
    hold(rig, 8)
    with pytest.raises(NetworkError, match="claim_part"):
        fparts.compile_part(rig.net, rig.me, "demo", "work", 2, "x = x * 3;")
    hold(rig, 8, part=2)
    with pytest.raises(NetworkError, match="held by other"):
        fparts.compile_part(rig.net, rig.me, "demo", "work", 2, "x = x * 3;")


def test_claim_part_takes_the_free_part_with_most_differing_rows(rig):
    hold(rig, 8)
    rig.net._last_diffs[(8, 1)] = (rows(2, 3), "")
    out = fparts.claim_part(rig.net, rig.me, "demo", "work")
    assert out.startswith("CLAIMED ## work, part 2 of 2") and "+0xc: b0" in out and "a0" not in out.split("### Its rows")[0]
    assert rig.db.execute("SELECT part, agent_id FROM part_claims").fetchall()[0][:] == (2, 7)
    with pytest.raises(NetworkError, match="held by tester"):
        fparts.claim_part(rig.net, rig.other, "demo", "work", 2)
    assert "parts 1 are free" in fparts.offer(rig.net, "demo", "work")
    assert "Released part 2" in fparts.release_part(rig.net, rig.me, "demo", "work", "b1 still differs")
    assert rig.net.notes == ["part 2: b1 still differs"]


def test_claim_part_needs_marked_parts(rig):
    rig.path.write_text("int work(int x) { return x; }\n")
    with pytest.raises(NetworkError, match="No parts are marked"):
        fparts.claim_part(rig.net, rig.me, "demo", "work")


def test_a_whole_definition_may_not_change_a_part_someone_holds(rig):
    hold(rig, 7)
    hold(rig, 8, part=2)
    fn = rig.net.forks._fn("demo", "work")
    with pytest.raises(NetworkError, match="part 2 of work is being worked on by other"):
        fparts.guard(rig.net, rig.me, fn, DEFINITION, DEFINITION.replace("x * 2", "x * 5"))
    with pytest.raises(NetworkError, match="part 2"):
        fparts.guard(rig.net, rig.me, fn, DEFINITION, "int work(int x) { return x; }")
    fparts.guard(rig.net, rig.me, fn, DEFINITION, DEFINITION.replace("x + 1", "x + 5"))  # its own part 1 is free


def test_the_report_counts_big_functions(rig):
    rig.db.execute("UPDATE functions SET attempts=3, best_score=45000 WHERE id=1")
    rig.db.execute("INSERT INTO attempts(function_id, agent_id, score, created_at, via) VALUES(1, 7, 1, strftime('%s','now'), 'part')")
    out = fparts.report(rig.net, "demo")
    assert "1 open: 1 tried, 1 compiled" in out and "(1 with compile_part)" in out and "work 50%" in out


def test_part_tools_are_for_source_trees_in_both_interfaces():
    new = {"list_parts", "claim_part", "compile_part", "release_part"}
    fork = {t["function"]["name"] for t in api_tool_schemas(fork=True)}
    rom = {t["function"]["name"] for t in api_tool_schemas()}
    assert new <= fork and not new & rom
    assert "big_function_report" in fork & rom
    net = SimpleNamespace()
    mcp = tools.build_mcp(net, lambda ctx: (None, "demo"))
    assert new | {"big_function_report"} <= {t.name for t in asyncio.run(mcp.list_tools())}


def test_another_agents_claim_lets_a_part_compile_through(tmp_path):
    """_attempt refuses a function someone else holds, except for a part compile_part checked."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    db.execute("INSERT INTO claims VALUES(1, 8, 0, 1e12)")

    @contextmanager
    def tx():
        with db:
            yield db

    engine = ForkEngine.__new__(ForkEngine)
    engine.net = SimpleNamespace(store=SimpleNamespace(tx=tx), _agent_name=lambda i: "other", _expire_claims=lambda db: None,
                                 claim_ttl=300, _generation={})
    (tmp_path / "unit.c").write_text("int work(int x) { return x; }\n")
    unit = SimpleNamespace(name="unit", source="unit.c", complete=False, target=None)
    fp = SimpleNamespace(root=tmp_path, unit=lambda name: unit, safe_path=lambda rel, **kw: tmp_path / rel,
                         diff=lambda *a: SimpleNamespace(matched=False, match_percent=50, unit_scores={}, literals=[], rows=[]))
    engine.get = lambda slug: (dict(id=1), fp, {})
    engine._fn = lambda slug, function: dict(id=1, project_id=1, name="work", unit="unit", status="open", n_insns=5,
                                             best_score=None, best_source=None)
    engine._meta = lambda fn: {}
    engine._names = lambda meta, fn: ["work"]
    engine._tree = lambda proj: _TreeLock()
    engine._unit_lock = lambda proj, unit: RLock()
    engine._baseline = lambda *a: ({"work": 50}, True)
    engine._held = lambda *a: set()
    engine._keep_scores = lambda *a: None
    engine._build_unit = lambda *a: (True, "")
    agent = SimpleNamespace(id=7, name="tester")
    with pytest.raises(NetworkError, match="claimed by other"):
        engine._attempt(agent, "demo", "work", "int work(int x) { return x + 1; }", probe=True)
    assert engine._attempt(agent, "demo", "work", "int work(int x) { return x + 1; }", probe=True, part=1)["score"]


def test_the_gui_shows_part_holders_and_the_big_functions(tmp_path):
    """A helper on a part of someone's function is at work too: the live claims carry it with its part, and the map's
    signature changes with it. The progress line counts the open big functions and how many were ever tried."""
    from .test_claims import _game

    net, pid = _game(tmp_path, [("big", 900, "open"), ("small", 10, "open")])
    a, _ = net.local_agent("owner")
    b, _ = net.local_agent("helper")
    net.overview = lambda slug: {}
    fn = net.store.one("SELECT * FROM functions WHERE name='big'")
    with net.store.tx() as db:
        db.execute("INSERT INTO claims VALUES(?,?,0,1e12)", (fn["id"], a.id))
    sig = net.map_signature("game")
    with net.store.tx() as db:
        db.execute("INSERT INTO part_claims VALUES(?,3,?,0,1e12)", (fn["id"], b.id))
        db.execute("UPDATE functions SET attempts=2 WHERE id=?", (fn["id"],))
    claims = net.live("game")["claims"]
    assert {(c["agent"], c.get("part")) for c in claims} == {("owner", None), ("helper", 3)}
    assert net.map_signature("game") != sig
    assert net._big_functions(net._project("game")) == {"open": 1, "tried": 1, "parts_held": 1, "insns": 400}

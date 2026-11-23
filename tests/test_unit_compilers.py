"""Each PS4 unit's own compiler and flags, switched only on evidence and documented: compiler_units.json, the template's
compile.py that reads it, the evidence rule (app/unit_compilers.py) and the project's docs/build-settings.md."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from decompnet import toolchains
from decompnet.agents.api import api_tool_schemas
from decompnet.app import tools, unit_compilers
from decompnet.app.forkengine import ForkEngine
from decompnet.consoles.ps4 import units
from decompnet.store import Store

TEMPLATE = Path(units.__file__).with_name("project_template") / "tools"


def project(tmp_path, entries=None):
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "configure.py").write_text('CL_FLAGS = ["-O2", "-mavx"]\n')
    if entries is not None:
        (tmp_path / units.UNITS).write_text(json.dumps(entries))
    return tmp_path


# ------------------------------------------------------------------------------------------- the settings file

def test_settings_keep_the_old_form_and_say_why_in_the_new(tmp_path):
    root = project(tmp_path, {"text_00001000": ["-march=btver2"], "text_00002000": ["-fPIC"]})
    units.set_compiler(root, "src/auto/text_00002000.c", "clang-3.8.1", "two functions match only with it",
                       [{"function": "func_00002010", "before": 97.5}])
    raw = json.loads((root / units.UNITS).read_text())
    assert raw["text_00001000"] == ["-march=btver2"]  # flags only, nothing to say: the old form, unchanged
    assert raw["text_00002000"]["compiler"] == "clang-3.8.1" and raw["text_00002000"]["flags"] == ["-fPIC"]
    assert raw["text_00002000"]["why"] and raw["text_00002000"]["evidence"][0]["function"] == "func_00002010"
    assert units.file_compiler(root, "src/auto/text_00002000.c") == "clang-3.8.1"
    assert units.file_compiler(root, "src/auto/text_00009000.c") == units.DEFAULT
    units.set_compiler(root, "src/auto/text_00002000.c", units.DEFAULT)  # back: its reason goes with it
    assert json.loads((root / units.UNITS).read_text())["text_00002000"] == ["-fPIC"]


def test_the_page_lists_every_unit_with_its_reason_and_evidence(tmp_path):
    entries = {f"text_{a:08X}": {"flags": ["-march=btver2"], "why": "a btver2 range"} for a in (0x1000, 0x2000, 0x3000)}
    entries["text_00009000"] = {"compiler": "clang-3.9.1", "why": "two functions match only with it",
                                "evidence": [{"function": "func_00009010", "before": 98.9, "by": "codex-1"}],
                                "set": "2026-10-25"}
    entries["text_0000A000"] = ["-fPIC"]
    root = project(tmp_path, entries)
    text = units.write_doc(root, order=["text_00001000", "text_00002000", "text_00003000", "text_00009000",
                                        "text_0000A000"]).read_text()
    assert "Built with **clang-5.0.2** and `-O2 -mavx`" in text
    assert "### 3 units: flags `-march=btver2`" in text and "`text_00001000` to `text_00003000` (3)" in text
    assert "Why: a btver2 range" in text
    assert "compiler **clang-3.9.1**" in text and "`func_00009010` 98.9% with clang-5.0.2, 100% with it by codex-1" in text
    assert "not recorded" in text  # the -fPIC unit: set before reasons were kept, and the page says so...
    assert "What this flag does: `-fPIC` the unit's target keeps the three-instruction TLS sequence" in text  # ...and why
    assert "| clang-3.9.1 | LLVM 3.9.1 release" in text


def test_the_pins_are_upstream_releases_with_checksums():
    for tid in units.OLDER:
        tc = toolchains.get(tid)
        assert tc.platform == "PS4" and not tc.proprietary
        assert all(len(p["sha256"]) == 64 and p["url"].startswith("https://releases.llvm.org/") for p in tc.parts)


# --------------------------------------------------------------------------- the template's compile.py, for real

FAKE = """import json, sys
from pathlib import Path
args = sys.argv[1:]
Path(args[args.index("-o") + 1]).write_bytes(b"obj")
Path(__file__).with_suffix(".log").write_text(json.dumps([Path(__file__).name, *args]))
"""


def compile_with(tmp_path, entries, compilers=None, extra=()):
    root = project(tmp_path, entries)
    for name in ("compile.py",):
        (root / "tools" / name).write_text((TEMPLATE / name).read_text())
    for name in ("base", "old"):
        script = root / f"{name}_clang.py"
        script.write_text(FAKE)
    wrapper = root / "clang"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {root / 'base_clang.py'} \"$@\"\n")
    wrapper.chmod(0o755)
    old = root / "old_clang"
    old.write_text(f"#!/bin/sh\nexec {sys.executable} {root / 'old_clang.py'} \"$@\"\n")
    old.chmod(0o755)
    if compilers is not None:
        (root / units.COMPILERS).write_text(json.dumps({k: dict(v, path=str(old) if v.get("path") else "")
                                                        for k, v in compilers.items()}))
    (root / "src").mkdir(exist_ok=True)
    (root / "src/text_00001000.c").write_text("int f(void) { return 0; }\n")
    done = subprocess.run([sys.executable, "tools/compile.py", "--compiler", str(wrapper), "--include", "include",
                           "--config", "c.json", "--output", "out/text_00001000.o", "--source", "src/text_00001000.c",
                           *extra, "--", "-O2", "-fPIE"], cwd=root, capture_output=True, text=True)
    used = next((json.loads(p.read_text()) for p in (root / "old_clang.log", root / "base_clang.log") if p.exists()), None)
    return done, used


def test_compile_py_adds_a_units_flags_as_before(tmp_path):
    done, used = compile_with(tmp_path, {"text_00001000": ["-march=btver2", "-fPIC"]})
    assert done.returncode == 0 and used[0] == "base_clang.py"
    assert "-march=btver2" in used and "-fPIC" in used and "-fPIE" not in used  # -fPIC drops the base -fPIE


def test_compile_py_builds_a_unit_with_its_own_compiler(tmp_path):
    spec = {"clang-3.8.1": {"path": "x", "flags": ["--target=x86_64-scei-ps4"], "default_march": "x86-64"}}
    done, used = compile_with(tmp_path, {"text_00001000": {"compiler": "clang-3.8.1", "why": "evidence"}}, spec)
    assert done.returncode == 0 and used[0] == "old_clang.py"
    assert used[1] == "--target=x86_64-scei-ps4" and "-march=x86-64" in used  # the build's CPU, not the target's
    done, used = compile_with(tmp_path, {"text_00001000": {"compiler": "clang-3.8.1", "flags": ["-march=btver2"]}}, spec)
    assert "-march=btver2" in used and "-march=x86-64" not in used


def test_compile_py_says_which_compiler_to_install(tmp_path):
    done, used = compile_with(tmp_path, {"text_00001000": {"compiler": "clang-3.9.1"}}, {})
    assert done.returncode == 2 and "clang-3.9.1" in done.stderr and "install it" in done.stderr and used is None


def test_the_survey_builds_a_unit_with_another_compiler_without_changing_its_settings(tmp_path):
    spec = {"clang-3.8.1": {"path": "x", "flags": ["--target=x86_64-scei-ps4"]}}
    done, used = compile_with(tmp_path, {}, spec, extra=("--unit-compiler", "clang-3.8.1"))
    assert done.returncode == 0 and used[0] == "old_clang.py"
    assert json.loads((tmp_path / units.UNITS).read_text()) == {}


# -------------------------------------------------------------------------------------------- the evidence rule

def test_a_file_switches_only_on_enough_new_matches_and_no_loss():
    before = {"a": 100.0, "b": 97.0, "c": 95.0, "d": None}
    assert unit_compilers.decide(before, {"a": 100, "b": 100, "c": 100, "d": 50}, 2) == (True, ["b", "c"], [])
    assert unit_compilers.decide(before, {"a": 100, "b": 100, "c": 96}, 2)[0] is False  # one could be luck
    assert unit_compilers.decide(before, {"a": 100, "b": 100}, 1)[0] is True  # where one is enough (DS)
    ok, gained, lost = unit_compilers.decide(before, {"a": 99, "b": 100, "c": 100}, 2)
    assert not ok and lost == ["a"]


def test_evidence_is_kept_per_file_and_compiler_until_used(tmp_path):
    store = Store(":memory:")
    db = store.conn
    db.execute("INSERT INTO functions(id, project_id, name, status) VALUES(1, 1, 'f', 'open'), (2, 1, 'g', 'matched')")
    net = SimpleNamespace(store=store)
    proj, agent = {"id": 1}, SimpleNamespace(id=7)
    assert unit_compilers.keep(net, proj, "src/a.c", "clang-3.8.1", {"id": 1}, "int f(void){}", 97.0, agent) == 1
    unit_compilers.keep(net, proj, "src/a.c", "clang-3.8.1", {"id": 2}, "int g(void){}", 90.0, agent)
    assert [e["name"] for e in unit_compilers.stored(net, proj, "src/a.c", "clang-3.8.1")] == ["f"]  # g matched since
    assert unit_compilers.stored(net, proj, "src/a.c", "clang-3.9.1") == []
    unit_compilers.forget(net, proj, "src/a.c")
    assert unit_compilers.stored(net, proj, "src/a.c", "clang-3.8.1") == []


@pytest.fixture
def evidence_rig(tmp_path):
    store = Store(":memory:")
    store.conn.execute("INSERT INTO functions(id, project_id, name, status, unit) VALUES(1, 1, 'f', 'open', 'u'), "
                       "(2, 1, 'g', 'open', 'u')")
    calls = SimpleNamespace(attempts=[], switches=[], commits=[])
    engine = ForkEngine.__new__(ForkEngine)
    engine.net = SimpleNamespace(store=store, bump_generation=lambda pid: None)
    engine._fn = lambda slug, name: dict(store.one("SELECT * FROM functions WHERE name=?", name))
    engine._meta = lambda fn: {}
    engine._attempt = lambda agent, slug, name, code, **kw: calls.attempts.append(name) or {"matched": True}
    engine._commit_paths = lambda proj, fp, paths, message: calls.commits.append((paths, message))
    from collections import defaultdict
    from threading import Lock
    engine._build_locks = defaultdict(Lock)
    console = SimpleNamespace(compiler_evidence=2, settings_doc=lambda fp: "docs/build-settings.md",
                              set_file_compiler=lambda fp, src, c, why="", evidence=None:
                              calls.switches.append((c, why, evidence)) or "tools/compiler_units.json")
    rig = SimpleNamespace(engine=engine, console=console, calls=calls, proj={"id": 1}, unit=SimpleNamespace(source="src/u.c"),
                          agent=SimpleNamespace(id=7, name="codex-1"))
    return rig


def switch(rig, name, base, now):
    fn = rig.engine._fn("demo", name)
    return rig.engine._evidence_switch(rig.agent, "demo", rig.proj, None, rig.console, rig.unit, fn, "clang-5.0.2",
                                       "clang-3.8.1", f"int {name}(void){{}}", base, now)


def test_one_match_with_another_compiler_is_kept_as_evidence_and_moves_nothing(evidence_rig):
    said = switch(evidence_rig, "f", {"f": 97.0, "g": 90.0}, {"f": 100.0, "g": 90.0})
    assert "Kept as evidence: 1 of the 2" in said and "stays with clang-5.0.2" in said
    assert not evidence_rig.calls.switches and not evidence_rig.calls.commits and not evidence_rig.calls.attempts


def test_the_second_function_completes_the_evidence_and_the_file_switches_documented(evidence_rig):
    switch(evidence_rig, "f", {"f": 97.0, "g": 90.0}, {"f": 100.0, "g": 90.0})
    assert switch(evidence_rig, "g", {"f": 97.0, "g": 90.0}, {"f": 97.0, "g": 100.0}) is None
    assert evidence_rig.calls.attempts == ["g", "f"]  # its own C, and the evidence's put back with the switch
    compiler, why, evidence = evidence_rig.calls.switches[-1]
    assert compiler == "clang-3.8.1" and "2 of its functions match byte for byte only with clang-3.8.1" in why
    assert sorted(e["function"] for e in evidence) == ["f", "g"]
    paths, message = evidence_rig.calls.commits[-1]
    assert paths == ["tools/compiler_units.json", "docs/build-settings.md"] and "docs/build-settings.md" in message


def test_a_lost_match_never_switches_a_file(evidence_rig):
    said = switch(evidence_rig, "f", {"f": 97.0, "g": 100.0}, {"f": 100.0, "g": 98.0})
    assert "no longer match" in said and not evidence_rig.calls.switches


# ---------------------------------------------------------------------------- an older project's build tools

def test_an_older_projects_tool_is_brought_up_to_date_only_if_every_match_holds(tmp_path, monkeypatch):
    root = project(tmp_path, {})
    (root / "tools/compile.py").write_text("# the old wrapper\n")
    (root / ".gitignore").write_text("build/\n")
    measured = iter([{"u:f", "u:g"}, {"u:f"}])  # before, after: g lost
    monkeypatch.setattr(units, "_matched", lambda fp: next(measured))
    monkeypatch.setattr(units, "write_compilers", lambda root: None)
    events, commits = [], []
    fp = SimpleNamespace(root=root)
    engine = SimpleNamespace(get=lambda slug: ({"id": 1}, fp, {"origin": "scratch"}),
                             _tree=lambda proj: SimpleNamespace(exclusive=lambda: __import__("contextlib").nullcontext()),
                             _build_all=lambda proj, fp: (True, ""),
                             _commit_paths=lambda proj, fp, paths, message: commits.append(paths),
                             sync=lambda slug, log=None: events.append(("synced",)))
    from contextlib import contextmanager

    @contextmanager
    def tx():
        yield SimpleNamespace()
    net = SimpleNamespace(forks=engine, store=SimpleNamespace(tx=tx), _event=lambda db, *a: events.append(a))
    units.upgrade(net, "demo", log=lambda *a: None)
    assert (root / "tools/compile.py").read_text() == "# the old wrapper\n"  # put back: a match would be lost
    assert "lost 1 match" in events[0][-1]
    assert "tools/compile.py" not in commits[0] and ".gitignore" in commits[0]  # what did change is committed
    assert not (root / "tools/jump_tables.py").exists()  # the new tool went back with it
    measured = iter([{"u:f"}, {"u:f", "u:g"}])
    said = units.upgrade(net, "demo", log=lambda *a: None)
    assert "def unit_settings" in (root / "tools/compile.py").read_text() and "every match kept, 1 more" in said
    assert (root / "tools/jump_tables.py").exists() and {"tools/compile.py", "tools/jump_tables.py"} <= set(commits[-1])
    assert ("synced",) in events  # the new match is recorded at once
    assert units.upgrade(net, "demo", log=lambda *a: None) is None  # up to date: nothing more to do
    assert "tools/compilers.json" in (root / ".gitignore").read_text()


def test_the_survey_tool_is_for_source_trees_in_both_interfaces():
    fork = {t["function"]["name"] for t in api_tool_schemas(fork=True)}
    rom = {t["function"]["name"] for t in api_tool_schemas()}
    assert "compiler_survey" in fork and "compiler_survey" not in rom
    mcp = tools.build_mcp(SimpleNamespace(), lambda ctx: (None, "demo"))
    assert "compiler_survey" in {t.name for t in asyncio.run(mcp.list_tools())}


def test_settings_are_parsed_once_per_change_and_written_whole(tmp_path):
    """The overview asks every source file's compiler: parsing the file each time took 6.5 s a poll on a PS4 game."""
    root = project(tmp_path, {"text_00001000": ["-march=btver2"]})
    assert units.file_compiler(root, "src/auto/text_00001000.c") == units.DEFAULT
    units.set_compiler(root, "src/auto/text_00001000.c", "clang-3.9.1", "evidence")
    assert units.file_compiler(root, "src/auto/text_00001000.c") == "clang-3.9.1"  # the change is seen at once
    assert not list((root / "tools").glob("*.tmp"))  # written whole, then put in place
    mine = units.read(root)
    mine["text_00001000"]["flags"].append("-O0")  # a caller's copy: the cache isn't changed through it
    assert units.read(root)["text_00001000"]["flags"] == ["-march=btver2"]



def test_a_run_that_waited_for_the_build_doesnt_rebuild_again(tmp_path, monkeypatch):
    """The app's start and a Check again both bring the tools up to date: the one that waits for the build must find
    them current and leave the build alone (it rebuilt the whole game a second time, holding the build 23 minutes)."""
    from contextlib import contextmanager, nullcontext

    root = project(tmp_path, {})
    (root / "tools/compile.py").write_text("# the old wrapper\n")
    monkeypatch.setattr(units, "write_compilers", lambda root: None)
    builds = []

    @contextmanager
    def held():  # meanwhile, the other run brought the tools up to date
        for name in units.TEMPLATE_TOOLS:
            (root / "tools" / name).write_text(units.TEMPLATE_TOOL.with_name(name).read_text())
        yield
    fp = SimpleNamespace(root=root)
    engine = SimpleNamespace(get=lambda slug: ({"id": 1}, fp, {"origin": "scratch"}),
                             _tree=lambda proj: SimpleNamespace(exclusive=held),
                             _build_all=lambda proj, fp: builds.append(1) or (True, ""),
                             _commit_paths=lambda *a: None)
    net = SimpleNamespace(forks=engine, store=SimpleNamespace(tx=lambda: nullcontext(SimpleNamespace())),
                          _event=lambda *a: None)
    units.upgrade(net, "demo", log=lambda *a: None)
    assert builds == []


def test_a_one_function_file_switches_on_its_one_match(evidence_rig):
    assert switch(evidence_rig, "f", {"f": 62.0}, {"f": 100.0}) is None
    assert evidence_rig.calls.switches[-1][0] == "clang-3.8.1"


def test_another_compilers_try_shows_its_diff_rows(tmp_path, monkeypatch):
    """try_compiler with clang 3.8.1 said 99.3% and nothing else: agents had to guess the one row left from a
    percentage. A try that doesn't match shows that compiler's diff."""
    from collections import defaultdict
    from contextlib import nullcontext
    from threading import Lock

    from decompnet import consoles

    (tmp_path / "src").mkdir()
    (tmp_path / "src/u.c").write_text("int f(void){return 1;}\n")
    unit = SimpleNamespace(name="u", source="src/u.c", complete=False)
    fp = SimpleNamespace(root=tmp_path, unit=lambda name: unit, linkable=lambda: False)
    console = SimpleNamespace(compilers=[SimpleNamespace(id="clang-5.0.2"), SimpleNamespace(id="clang-3.8.1")],
                              project_compilers=lambda fp: console.compilers, compiler_evidence=2, file_compiler=lambda fp, src: "clang-5.0.2",
                              set_file_compiler=lambda fp, src, c, *a: "tools/compiler_units.json")
    monkeypatch.setattr(consoles, "get", lambda platform: console)
    engine = ForkEngine.__new__(ForkEngine)
    engine.net = SimpleNamespace(links={}, _source_of=lambda fn: "int f(void){return 1;}")
    engine.get = lambda slug: ({"id": 1}, fp, {"platform": "ps4"})
    engine._fn = lambda slug, name: {"id": 1, "name": "f", "status": "open", "unit": "u"}
    engine._hold_for_try = lambda slug, function, agent: None
    engine._tree = lambda proj: SimpleNamespace(shared=nullcontext, exclusive=nullcontext)
    engine._unit_lock = lambda proj, name: nullcontext()
    engine._baseline = lambda proj, fp, unit, text: ({"f": 60.0}, True)
    engine._build_locks = defaultdict(Lock)
    engine._build_unit = lambda proj, fp, unit: (True, "")
    rows = [(" ", "push rbp", "push rbp"), ("~", "mov eax, 0x1", "mov eax, 0x2"), (" ", "ret", "ret")]
    engine._attempt = lambda *a, **kw: {"text": "99.3% (score 13)", "score": 13, "matched": False, "rows": rows}
    said = engine.try_compiler(SimpleNamespace(id=7, name="codex-2"), "demo", "f", "clang-3.8.1")
    assert "Its diff with clang-3.8.1:" in said and "mov eax, 0x2" in said and "stays with clang-5.0.2" in said

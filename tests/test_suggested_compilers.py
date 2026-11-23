"""An agent's compiler suggestion, applied with the project page's button, on a project whose source files each have
a compiler. Two of a PS4 game's did nothing: one named a range that was only the last function of a file (the file's
other functions need its own compiler), and one named no part of the game at all."""

from types import SimpleNamespace

import pytest

from decompnet.app.engine.compilers import CompilerMixin
from decompnet.app.forkengine import ForkEngine
from decompnet.network import NetworkError


def rig(files: dict[str, list[tuple[str, int]]], refuse: set[str]):
    """A fork engine over `files` ({source: [(function, address)]}): switching a file in `refuse` loses a match;
    split_file moves a function and those after it into a file of their own, as the PS4's does."""
    files = {k: list(v) for k, v in files.items()}
    calls = SimpleNamespace(switched=[], splits=[])
    console = SimpleNamespace(
        file_compiler=lambda fp, src: "clang-5.0.2",
        file_ranges=lambda fp: {f: [(fns[0][1], fns[-1][1] + 0x10)] for f, fns in files.items()},
        function_addresses=lambda fp: {(f, n): a for f, fns in files.items() for n, a in fns})
    fp = SimpleNamespace(units_for=lambda src: [SimpleNamespace(name=src)] if src in files else [],
                         unit=lambda name: SimpleNamespace(source=name))
    engine = ForkEngine.__new__(ForkEngine)
    engine.get = lambda slug: ({"id": 1}, fp, {"platform": "ps4"})

    def by_hand(slug, source, compiler):
        if source in refuse:
            raise NetworkError(f"{source} stays with clang-5.0.2: func_keep no longer match with it")
        calls.switched.append((source, compiler))

    def split_file(agent, slug, path, at, new_name="", data=""):
        fns = files[path]
        i = [n for n, _ in fns].index(at)
        new = f"src/auto/text_{fns[i][1]:08X}.c"
        files[path], files[new] = fns[:i], fns[i:]
        calls.splits.append((path, at))
        return f"✅ {path} is divided: {new} now holds {at}"

    engine.set_file_compiler_by_hand = by_hand
    engine.split_file = split_file
    return engine, console, calls, files


A = "src/auto/text_00589120.c"
FUNCS = [("func_00589120", 0x589120), ("func_005899E0", 0x5899E0), ("func_00589E30", 0x589E30)]


def test_a_range_at_a_files_end_gets_a_file_of_its_own_and_its_compiler(monkeypatch):
    engine, console, calls, files = rig({A: FUNCS}, refuse={A})
    monkeypatch.setattr("decompnet.app.forkengine.consoles.get", lambda platform: console)
    said = engine.set_compiler_ranges("demo", [[0x589E30, 0x589E9C]], "clang-3.8.1")
    assert calls.splits == [(A, "func_00589E30")]  # one split: the range runs to the file's end
    assert calls.switched == [("src/auto/text_00589E30.c", "clang-3.8.1")]
    assert files[A] == FUNCS[:2]  # the neighbour that needs clang 5 keeps its file and compiler
    assert "a file of its own: src/auto/text_00589E30.c (out of " + A in said


def test_a_range_in_a_files_middle_is_split_off_on_both_sides(monkeypatch):
    engine, console, calls, files = rig({A: FUNCS}, refuse={A})
    monkeypatch.setattr("decompnet.app.forkengine.consoles.get", lambda platform: console)
    engine.set_compiler_ranges("demo", [[0x5899E0, 0x589A00]], "clang-3.8.1")
    assert calls.splits == [(A, "func_005899E0"), ("src/auto/text_005899E0.c", "func_00589E30")]
    assert calls.switched == [("src/auto/text_005899E0.c", "clang-3.8.1")]
    assert [n for n, _ in files["src/auto/text_005899E0.c"]] == ["func_005899E0"]


@pytest.mark.parametrize("ranges", [[[0x589120, 0x589E9C]], [[0x589120, 0x589130], [0x589E30, 0x589E40]]],
                         ids=["the whole file", "not one run of functions"])
def test_no_split_when_the_range_isnt_one_part_of_the_file(monkeypatch, ranges):
    engine, console, calls, _ = rig({A: FUNCS}, refuse={A})
    monkeypatch.setattr("decompnet.app.forkengine.consoles.get", lambda platform: console)
    with pytest.raises(NetworkError, match="stays with clang-5.0.2"):
        engine.set_compiler_ranges("demo", ranges, "clang-3.8.1")
    assert not calls.splits and not calls.switched


def test_a_file_that_switches_whole_isnt_split(monkeypatch):
    engine, console, calls, _ = rig({A: FUNCS}, refuse=set())
    monkeypatch.setattr("decompnet.app.forkengine.consoles.get", lambda platform: console)
    engine.set_compiler_ranges("demo", [[0x589E30, 0x589E9C]], "clang-3.8.1")
    assert calls.switched == [(A, "clang-3.8.1")] and not calls.splits


def test_a_suggestion_naming_no_part_says_why_it_cant_apply_on_a_source_tree():
    net = SimpleNamespace(cfg=lambda proj: {"suggestions": [{"id": 77, "toolchain": "clang-3.8.1", "flags": [],
                                                              "ranges": [], "status": "open"}]},
                          _project=lambda slug: {}, fork_slug=lambda slug: True,
                          set_compiler=lambda *a, **k: pytest.fail("a ROM project's switch on a source tree"))
    with pytest.raises(NetworkError, match="names no part of the game.*Settings → Compiler"):
        CompilerMixin.resolve_suggestion(net, "demo", 77, True)


def test_an_agents_compiler_suggestion_must_name_its_file_or_ranges(monkeypatch):
    from decompnet.app.filecompilers import FileCompilers

    console = SimpleNamespace(project_compilers=lambda fp: [SimpleNamespace(id="clang-3.8.1", name="Clang 3.8.1")])
    monkeypatch.setattr("decompnet.app.filecompilers.consoles.get", lambda platform: console)
    fc = FileCompilers.__new__(FileCompilers)
    fc.net = SimpleNamespace(forks=SimpleNamespace(get=lambda slug: ({"id": 1}, None, {"platform": "ps4"})),
                             _update_cfg=lambda slug, change: pytest.fail("kept a suggestion for the whole game"))
    with pytest.raises(NetworkError, match="function=<a function of the file>"):
        fc.suggest(SimpleNamespace(name="agent"), "demo", "unit text_00580B20 should be built with it", "clang-3.8.1",
                   None, "")

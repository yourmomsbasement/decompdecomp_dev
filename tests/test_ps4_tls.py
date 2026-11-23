"""Units whose target reads thread-locals as code built for shared objects does get -fPIC from the build
(consoles/ps4/tls.py): on a PS4 game 344 units' targets had the relaxed TLSGD sequence and were built with -fPIE, and
none of their 715 open functions with it had matched. Agents reported the same boundary unit after unit; the flags are
the build's, so the build decides them, from the target's bytes, keeping every match."""

import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from decompnet.app import unit_compilers
from decompnet.consoles.ps4 import tls, units

TOOLS = Path(units.__file__).with_name("project_template") / "tools"


def delinked(path: Path, slot: str) -> Path:
    """A target object whose function reads `slot` the game's way: mov rax,fs:[0]; add rax,[rip+slot]; mov rax,[rax]."""
    spec = importlib.util.spec_from_file_location("ps4_delink", TOOLS / "delink.py")
    delink = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TOOLS))
    try:
        spec.loader.exec_module(delink)
    finally:
        sys.path.remove(str(TOOLS))
    code = bytes.fromhex("64488b042500000000" "48030500000000" "488b00" "c3")
    path.write_bytes(delink.elf64(path.stem, [{"name": "f", "addr": 0, "size": len(code),
                                               "relocs": [(12, 2, slot, -4)]}], code, 0))
    return path


def test_the_targets_tls_access_is_read_from_its_bytes(tmp_path):
    target = delinked(tmp_path / "text_00001000.o", "D_00D9E080")
    fp = SimpleNamespace(units=lambda: [SimpleNamespace(name="a", target=target),  # (b's isn't built yet)
                                        SimpleNamespace(name="b", target=tmp_path / "text_00002000.o"),
                                        SimpleNamespace(name="c", target=None)])
    assert tls.evidence(fp) == {"a": {"f": {"D_00D9E080"}}}


def test_explicit_offset_plus_fs_base_is_not_a_tlsgd_relaxation(tmp_path):
    """Giving PIC to this different instruction form cannot reproduce its register/dataflow layout."""
    spec = importlib.util.spec_from_file_location("explicit_fs_delink", TOOLS / "delink.py")
    delink = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(delink)
    code = bytes.fromhex("488b0500000000" "64488b0c2500000000" "4c8b3c01" "c3")
    target = tmp_path / "explicit.o"
    target.write_bytes(delink.elf64("explicit", [{"name": "f", "addr": 0, "size": len(code),
                                                   "relocs": [(3, 2, "D_00D9E058", -4)]}], code, 0))
    assert tls.evidence(SimpleNamespace(units=lambda: [SimpleNamespace(name="explicit", target=target)])) == {}


def test_every_slot_gets_a_name_and_a_projects_own_names_stay(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / tls.SLOTS).write_text(json.dumps({"decomp_tls_context": "D_00D9E060"}))
    assert tls.write_slots(tmp_path, {"D_00D9E060", "D_00D9E080"})
    assert json.loads((tmp_path / tls.SLOTS).read_text()) == {"decomp_tls_context": "D_00D9E060",
                                                               "decomp_tls_D_00D9E080": "D_00D9E080"}
    assert not tls.write_slots(tmp_path, {"D_00D9E080"})  # nothing new
    assert tls.names(tmp_path)["D_00D9E080"] == "decomp_tls_D_00D9E080"


def rig(tmp_path, monkeypatch, scores_with_pic):
    root = tmp_path / "game"
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "configure.py").write_text('CL_FLAGS = ["-O2", "-fPIE"]\n')
    (root / "tools" / "tls_relax.py").write_text("")
    (root / units.UNITS).write_text(json.dumps({"text_c": ["-fPIC"], "text_b": ["-march=btver2"]}))
    base = tmp_path / "base.o"
    base.write_bytes(b"")
    (root / "src/auto").mkdir(parents=True)
    for n in "abc":
        (root / f"src/auto/text_{n}.c").write_text("int g(void){return 0;}\n")
    made = {n: SimpleNamespace(name=n, source=f"src/auto/text_{n}.c", base=base) for n in "abc"}
    fp = SimpleNamespace(root=root, unit=lambda n: made[n])
    cfg, calls = {}, SimpleNamespace(builds=[], commits=[], scored=[])
    engine = SimpleNamespace(get=lambda slug: ({"id": 1}, fp, cfg),
                             _tree=lambda proj: SimpleNamespace(exclusive=nullcontext),
                             _unit_lock=lambda proj, name: nullcontext(),
                             _build_unit=lambda proj, fp, unit: calls.builds.append(unit.name) or (True, ""),
                             _apply_scores=lambda proj, name, after, **kw: calls.scored.append(name),
                             _commit_paths=lambda proj, fp, paths, message: calls.commits.append((paths, message)))
    net = SimpleNamespace(forks=engine, bump_generation=lambda pid: None,
                          _update_cfg=lambda slug, change: change(cfg))
    monkeypatch.setattr(tls, "evidence", lambda fp: {n: {f"func_{n}": {"D_00D9E060"}} for n in "abc"})

    def scores(fp, unit, obj):
        pic = "-fPIC" in units.read(fp.root).get(f"text_{unit.name}", {}).get("flags", [])
        return scores_with_pic[unit.name] if pic else {"g": 100.0, f"func_{unit.name}": 60.0}
    monkeypatch.setattr(unit_compilers, "scores", scores)
    return net, root, cfg, calls


def test_a_tls_unit_gets_fpic_with_its_reason_and_one_losing_a_match_stays_pie(tmp_path, monkeypatch):
    net, root, cfg, calls = rig(tmp_path, monkeypatch, {"a": {"g": 100.0, "func_a": 100.0},
                                                        "b": {"g": 99.0, "func_b": 100.0}})
    assert tls.give_pic(net, "demo", log=lambda *a: None) == ["a"]
    settings = units.read(root)
    assert settings["text_a"]["flags"] == ["-fPIC"] and "three-instruction TLS access" in settings["text_a"]["why"]
    assert settings["text_a"]["evidence"] == [{"function": "func_a", "tls": ["D_00D9E060"]}]
    assert settings["text_b"]["flags"] == ["-march=btver2"] and not settings["text_b"]["why"]  # put back as it was
    assert calls.builds == ["a", "b", "b"]  # c already has it; b built again as it was
    assert calls.scored == ["a"]  # its new match recorded
    assert cfg[tls.KEPT_OUT] == ["b"]
    paths, message = calls.commits[-1]
    assert set(paths) == {units.UNITS, units.DOC, tls.SLOTS} and "Build 1 units with -fPIC" in message
    assert "-fPIC" in (root / units.DOC).read_text()
    calls.builds.clear()
    assert tls.give_pic(net, "demo", log=lambda *a: None) == [] and calls.builds == []  # none tried twice


def test_a_project_without_the_tls_relaxation_is_left_alone(tmp_path, monkeypatch):
    net, root, cfg, calls = rig(tmp_path, monkeypatch, {})
    (root / "tools" / "tls_relax.py").unlink()
    assert tls.give_pic(net, "demo", log=lambda *a: None) == [] and not calls.builds and not calls.commits


def test_the_template_relaxes_tls_and_keeps_its_tool_up_to_date():
    assert "relax_tls_gd_to_ie" in (TOOLS / "bind.py").read_text() and (TOOLS / "tls_relax.py").exists()
    assert "tls_relax.py" in units.TEMPLATE_TOOLS


def test_a_one_function_file_needs_one_match_of_evidence():
    """A one-function unit of a PS4 game could never show two functions matching only with clang 3.9.1."""
    console = SimpleNamespace(compiler_evidence=2)
    assert unit_compilers.needed(console, {"f": 60.0}) == 1
    assert unit_compilers.needed(console, {"f": 60.0, "g": 10.0, "h": 0.0}) == 2
    assert unit_compilers.needed(console, {}) == 2

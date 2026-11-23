"""Autopilot's drafts where m2c can't read the code: Ghidra's, from the project's cache. On a PS4 game the cache held a
draft for 21,612 of its 21,624 open functions and autopilot tried none (it drafted with m2c only, which doesn't read
x86). Each is judged as any draft is, and a draft already judged isn't built again."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from decompnet import consoles
from decompnet.app import forkengine
from decompnet.app.forkengine import ForkEngine
from decompnet.network import NetworkError
from decompnet.store import Store

DRAFT = ("/* WARNING: Globals starting with '_' overlap smaller symbols at the same address */\n\n"
         "undefined8 func_00001000(code *param_1,uint param_2)\n\n{\n  return (ulong)param_2;\n}\n")


def test_ps4_writes_ghidras_draft_in_the_projects_type_names():
    text = consoles.get("ps4").adapt_draft(DRAFT)
    assert text == "u64 func_00001000(void *param_1,u32 param_2)\n\n{\n  return (u64)param_2;\n}"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    target = tmp_path / "build/eboot/target/text_00001000.o"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x7fELF the unit's target object")
    cache = tmp_path / "build/ghidra"
    cache.mkdir(parents=True)
    key = hashlib.sha1(target.read_bytes()).hexdigest()[:16]
    (cache / f"text_00001000-{key}.json").write_text(json.dumps({"func_00001000": DRAFT}))
    store = Store(":memory:")
    store.conn.execute("INSERT INTO functions(id, project_id, name, n_insns, status, unit) "
                       "VALUES(1, 1, 'func_00001000', 3, 'open', 'text_00001000')")
    net = SimpleNamespace(store=store, _expire_claims=lambda db: None)
    engine = ForkEngine.__new__(ForkEngine)
    engine.net = net
    unit = SimpleNamespace(name="text_00001000", source="src/auto/text_00001000.c", target=target, scratch=None)
    fp = SimpleNamespace(root=tmp_path, unit=lambda name: unit)
    cfg = {"platform": "ps4", "origin": "scratch", "ready": True}
    engine.get = lambda slug: (dict(id=1, slug=slug), fp, cfg)
    monkeypatch.setattr(forkengine.ghidra, "available", lambda: False)  # the cache only, as on a computer without it
    monkeypatch.setattr(ForkEngine, "_uses_m2c", staticmethod(lambda cfg: False))
    tried = []

    def attempt(agent, slug, name, code, keep=True):
        tried.append(code)
        definition = code.split("\n", 1)[1]
        store.conn.execute("INSERT INTO attempts(function_id, agent_id, score, source) VALUES(1, ?, 50, ?)",
                           (agent.id, definition))
        return {"matched": False, "score": 50, "percent": 50.0, "text": "50%"}

    engine.attempt = attempt
    return SimpleNamespace(engine=engine, fp=fp, cfg=cfg, tried=tried, cache=cache)


def test_autopilot_judges_the_cached_ghidra_draft_once(rig):
    agent = SimpleNamespace(id=9, name="autopilot")
    assert rig.engine.autopilot_prepare("demo") == [1]
    assert rig.engine.autopilot_one(agent, {"slug": "demo"}, 1) == "far"
    assert rig.tried == ["/* machine draft (Ghidra), tried by autopilot */\n"
                         "u64 func_00001000(void *param_1,u32 param_2)\n\n{\n  return (u64)param_2;\n}"]
    assert rig.engine.autopilot_one(agent, {"slug": "demo"}, 1) == "skip" and len(rig.tried) == 1


def test_ghidras_drafts_are_tried_for_small_functions_only(rig):
    """3 of 400 drafts of 4-9 instructions matched on a PS4 game, none of 240 from 10 up (most didn't build)."""
    rig.engine.net.store.conn.execute("INSERT INTO functions(id, project_id, name, n_insns, status, unit) "
                                      "VALUES(2, 1, 'func_00002000', 40, 'open', 'text_00001000')")
    assert rig.engine.autopilot_prepare("demo") == [1]


def test_without_m2c_or_ghidra_drafts_autopilot_says_what_it_needs(rig):
    for f in rig.cache.iterdir():
        f.unlink()
    with pytest.raises(NetworkError, match="Ghidra"):
        rig.engine.autopilot_prepare("demo")

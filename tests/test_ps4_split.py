"""A compiler boundary can split PS4 source files without changing original function records."""
import json
from types import SimpleNamespace

import pytest

from decompnet.app.forkengine import ForkEngine
from decompnet.consoles.ps4 import PS4_CONSOLE, scratch
from decompnet.forks.project import ForkError


def project(tmp_path):
    (tmp_path / "config/v").mkdir(parents=True)
    (tmp_path / "src/auto").mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    functions = [{"name": n, "addr": a, "size": 16, "offset": a + 100,
                  "relocs": [[1, 2, "external", -4]], "body_ranges": [[a, a + 16]]}
                 for n, a in [("f", 16), ("g", 32), ("h", 48)]]
    cfg = {"sha1": "unchanged", "units": {"old": functions}}
    (tmp_path / "config/v/functions.json").write_text(json.dumps(cfg))
    settings = {"old": {"compiler": "clang-3.9.1", "flags": ["-fPIC"], "why": "measured"}}
    (tmp_path / "tools/compiler_units.json").write_text(json.dumps(settings))
    return SimpleNamespace(root=tmp_path), cfg, settings


def test_split_preserves_records_and_inherits_settings(tmp_path):
    fp, cfg, settings = project(tmp_path)
    assert PS4_CONSOLE.splits_sources
    saved = PS4_CONSOLE.split_source(fp, "v", "src/auto/old.c", "g", "src/auto/new.c")
    after = json.loads((tmp_path / "config/v/functions.json").read_text())
    assert after["sha1"] == cfg["sha1"]
    assert after["units"]["old"] + after["units"]["new"] == cfg["units"]["old"]
    current = json.loads((tmp_path / "tools/compiler_units.json").read_text())
    assert current["old"] == current["new"] == settings["old"]
    for path, previous in saved.items():
        path.write_text(previous)
    assert json.loads((tmp_path / "config/v/functions.json").read_text()) == cfg
    assert json.loads((tmp_path / "tools/compiler_units.json").read_text()) == settings


@pytest.mark.parametrize("at,new", [("f", "src/auto/new.c"), ("missing", "src/auto/new.c"),
                                     ("g", "src/auto/../new.c"), ("g", "src/new.c"),
                                     ("g", "src/auto/old.c")])
def test_invalid_split_is_not_a_mutation(tmp_path, at, new):
    fp, cfg, settings = project(tmp_path)
    with pytest.raises(ForkError):
        scratch.split_source(fp, "v", "src/auto/old.c", at, new)
    assert json.loads((tmp_path / "config/v/functions.json").read_text()) == cfg
    assert json.loads((tmp_path / "tools/compiler_units.json").read_text()) == settings


def test_split_file_dispatches_to_the_ps4_config_adapter():
    engine = ForkEngine.__new__(ForkEngine)
    engine.get = lambda slug: (None, None, {"platform": "ps4"})
    seen = []
    engine._split_source = lambda *args: seen.append(args) or "split through PS4 config"
    assert engine.split_file(None, "demo", "src/auto/old.c", "g") == "split through PS4 config"
    assert seen[0][2] is PS4_CONSOLE

"""Fresh baselines cannot trust source/object timestamps or incomplete custom-build dependency lists."""
import os
from types import SimpleNamespace

import pytest

from decompnet.app.forkengine import ForkEngine
from decompnet.forks.target_validation import ValidationResult


@pytest.fixture
def rig(tmp_path):
    source = tmp_path / 'unit.c'
    source.write_text('unchanged source')
    header = tmp_path / 'layout.h'
    header.write_text('good')
    compiler = tmp_path / 'compile.py'
    compiler.write_text('good')
    target = tmp_path / 'target.o'
    target.write_bytes(b'good')
    base = tmp_path / 'base.o'
    base.write_bytes(b'good')
    unit = SimpleNamespace(name='unit', source='unit.c', base=base, target=target)
    state = SimpleNamespace(builds=0, diffs=0, invalid=False)

    def build(unit):
        state.builds += 1
        # Simulate a custom build which mistakenly does not declare its header or compiler dependencies.
        # A missing output still forces regeneration, just as make/ninja do.
        if not unit.base.exists():
            unit.base.write_text(header.read_text() + compiler.read_text())
        return True, ''

    def diff(unit, symbol):
        state.diffs += 1
        return SimpleNamespace(unit_scores={'owner': 50, 'neighbor': 100 if base.read_text() == 'goodgood' else 20})

    fp = SimpleNamespace(root=tmp_path, build_unit=build, diff=diff,
                         validate_targets=lambda units: ValidationResult('invalid' if state.invalid else 'valid'))
    net = SimpleNamespace(store=SimpleNamespace(one=lambda *args: {'meta': '{}'}))
    engine = ForkEngine(net)
    return SimpleNamespace(engine=engine, fp=fp, unit=unit, state=state, source=source, header=header,
                           compiler=compiler, project={'id': 1}, root=tmp_path)


def test_unchanged_source_never_reuses_old_baseline_verdict(rig):
    first, _ = rig.engine._baseline(rig.project, rig.fp, rig.unit, rig.source.read_text())
    assert first['neighbor'] == 100
    second, _ = rig.engine._baseline(rig.project, rig.fp, rig.unit, rig.source.read_text())
    assert second['neighbor'] == 100
    assert rig.state.builds == rig.state.diffs == 2


@pytest.mark.parametrize('dependency', ['header', 'compiler'])
def test_same_mtime_dependency_change_rebuilds_and_rechecks_matched_neighbor(rig, dependency):
    assert rig.engine._baseline(rig.project, rig.fp, rig.unit, rig.source.read_text())[0]['neighbor'] == 100
    changed = getattr(rig, dependency)
    stamp = changed.stat()
    changed.write_text('evil')  # same length, same timestamp, unchanged function source
    os.utime(changed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    scores, builds = rig.engine._baseline(rig.project, rig.fp, rig.unit, rig.source.read_text())
    assert builds and scores['neighbor'] == 20
    assert rig.state.builds == rig.state.diffs == 2


def test_invalid_target_never_returns_cached_baseline(rig):
    rig.engine._baseline(rig.project, rig.fp, rig.unit, rig.source.read_text())
    rig.state.invalid = True
    assert rig.engine._baseline(rig.project, rig.fp, rig.unit, rig.source.read_text()) == (None, False)
    assert rig.state.builds == rig.state.diffs == 1


def test_historical_scores_are_copied_and_not_used_as_baseline(rig):
    rig.engine._keep_scores(rig.project, rig.unit, 'text', {'neighbor': 100})
    snapshot = rig.engine._last_scores(rig.project, rig.unit)
    snapshot['neighbor'] = 0
    assert rig.engine._last_scores(rig.project, rig.unit)['neighbor'] == 100
    rig.state.invalid = True
    assert rig.engine._baseline(rig.project, rig.fp, rig.unit, 'text') == (None, False)


def test_same_mtime_target_change_invalidates_index(rig, monkeypatch):
    calls = []

    def diff(*args, **kwargs):
        calls.append(rig.unit.target.read_bytes())
        return SimpleNamespace(order=['owner'], ops={'owner': rig.unit.target.read_bytes().hex()})

    monkeypatch.setattr('decompnet.app.forkengine.objdiff_diff', diff)
    rig.engine._meta = lambda row: {'symbol': 'owner'}
    rig.engine._save_index = lambda *args: None
    first = rig.engine._unit_index(rig.project, rig.fp, rig.unit)
    stamp = rig.unit.target.stat()
    rig.unit.target.write_bytes(b'evil')
    os.utime(rig.unit.target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    second = rig.engine._unit_index(rig.project, rig.fp, rig.unit)
    assert len(calls) == 2 and first['sha256'] != second['sha256']
    rig.state.invalid = True
    assert rig.engine._unit_index(rig.project, rig.fp, rig.unit) is None
    assert len(calls) == 2


@pytest.mark.parametrize('alias', ['same_path', 'hardlink', 'symlink', 'outside', 'original'])
def test_forced_build_never_unlinks_protected_or_outside_paths(rig, tmp_path, alias):
    if alias == 'same_path':
        rig.unit.base = rig.unit.target
    elif alias == 'hardlink':
        rig.unit.base.unlink()
        os.link(rig.unit.target, rig.unit.base)
    elif alias == 'symlink':
        rig.unit.base.unlink()
        rig.unit.base.symlink_to(rig.unit.target)
    elif alias == 'outside':
        outside = tmp_path.parent / (tmp_path.name + '-outside.o')
        outside.write_bytes(b'protected')
        rig.unit.base = outside
    else:
        config = rig.root / 'config/v'
        config.mkdir(parents=True)
        (config / 'functions.json').write_text('{"elf":"base.o"}')
        rig.fp.manifest = {'version': 'v'}
    before = rig.unit.base.read_bytes()
    assert rig.engine._baseline(rig.project, rig.fp, rig.unit, 'text') == (None, False)
    assert rig.unit.base.read_bytes() == before
    assert rig.unit.target.read_bytes() == b'good'
    assert rig.state.builds == 0


def test_failed_forced_rebuild_restores_old_artifact_without_accepting_it(rig):
    rig.engine._keep_scores(rig.project, rig.unit, 'text', {'neighbor': 100})
    old = rig.unit.base.read_bytes()
    rig.fp.build_unit = lambda unit: (False, 'compiler failed')
    assert rig.engine._baseline(rig.project, rig.fp, rig.unit, 'text') == (None, False)
    assert rig.unit.base.read_bytes() == old
    assert rig.state.diffs == 0

"""Every project comparison checks complete-unit original evidence before calling objdiff."""
import hashlib
import json

import pytest

from decompnet.forks.project import ForkError, ForkProject
from tests.test_target_validation import object_for, original_image


def project(tmp_path, declared=True):
    (tmp_path / 'objdiff.json').write_text(json.dumps({'units': [{'name': 'unit', 'target_path': 'target.o',
                                                               'base_path': 'base.o'}]}))
    if declared:
        (tmp_path / '.decomp.wtf.json').write_text('{"version":"v"}')
        config = tmp_path / 'config/v'
        config.mkdir(parents=True)
        original = original_image(b'\xc3')
        (tmp_path / 'original.elf').write_bytes(original)
        (config / 'functions.json').write_text(json.dumps({'elf': 'original.elf',
            'sha1': hashlib.sha1(original).hexdigest(), 'units': {
                'unit': [{'name': 'demo', 'addr': 0x1000, 'offset': 120, 'size': 1}]}}))
    (tmp_path / 'target.o').write_bytes(object_for(b'\xc3'))
    return ForkProject(tmp_path)


def test_diff_checks_current_evidence_and_preserves_options(tmp_path, monkeypatch):
    fp = project(tmp_path)
    calls = []
    monkeypatch.setattr('decompnet.forks.project.objdiff.diff', lambda *args, **kwargs: calls.append((args, kwargs)))
    fp.diff(fp.unit('unit'), 'demo')
    assert fp.target_validation.status == 'valid'
    assert calls[0][1]['options'] == fp.diff_options(fp.unit('unit'))
    (tmp_path / 'original.elf').write_bytes(original_image(b'\x90'))
    with pytest.raises(ForkError, match='hash differs'):
        fp.diff(fp.unit('unit'), 'demo')
    assert len(calls) == 1
    assert fp.target_validation.status == 'invalid'


def test_corrupt_or_missing_original_blocks_comparison(tmp_path, monkeypatch):
    fp = project(tmp_path)
    monkeypatch.setattr('decompnet.forks.project.objdiff.diff', lambda *a, **k: pytest.fail('judge was called'))
    (tmp_path / 'original.elf').unlink()
    with pytest.raises(ForkError, match='validation'):
        fp.diff(fp.unit('unit'), 'demo')


def test_complete_unit_checked_even_for_unrelated_selected_symbol(tmp_path, monkeypatch):
    fp = project(tmp_path)
    monkeypatch.setattr('decompnet.forks.project.objdiff.diff', lambda *a, **k: pytest.fail('judge was called'))
    # Selection must not hide corruption elsewhere in the unit.
    (tmp_path / 'target.o').write_bytes(object_for(b'\x90'))
    with pytest.raises(ForkError, match='Unrelocated'):
        fp.diff(fp.unit('unit'), 'some_other_symbol')


def test_unsupported_target_in_declared_x86_image_fails_closed(tmp_path):
    fp = project(tmp_path)
    (tmp_path / 'target.o').write_bytes(b'not an object')
    assert fp.validate_targets().status == 'invalid'


def test_ordinary_project_without_evidence_still_uses_judge(tmp_path, monkeypatch):
    fp = project(tmp_path, False)
    monkeypatch.setattr('decompnet.forks.project.objdiff.diff', lambda *a, **k: 'strict result')
    assert fp.diff(fp.unit('unit'), 'demo') == 'strict result'
    assert fp.target_validation.status == 'unavailable'


def test_required_manifest_absence_fails_closed(tmp_path):
    fp = project(tmp_path, False)
    (tmp_path / '.decomp.wtf.json').write_text('{"version":"v", "target_validation":{"required":true}}')
    assert fp.validate_targets().status == 'invalid'
    with pytest.raises(ForkError, match='manifest is missing'):
        fp.diff(fp.unit('unit'), 'demo')


def test_optional_version_without_evidence_explicitly_unavailable(tmp_path):
    fp = project(tmp_path, False)
    (tmp_path / '.decomp.wtf.json').write_text('{"version":"v"}')
    assert fp.validate_targets().status == 'unavailable'


def test_disappearing_previously_verified_manifest_cannot_silently_pass(tmp_path):
    fp = project(tmp_path)
    assert fp.validate_targets().status == 'valid'
    (tmp_path / 'config/v/functions.json').unlink()
    assert fp.validate_targets().status == 'invalid'


def test_report_rejects_invalid_original_evidence_before_judge(tmp_path, monkeypatch):
    fp = project(tmp_path)
    (tmp_path / 'original.elf').unlink()
    monkeypatch.setattr('decompnet.forks.project.objdiff.report', lambda *a, **k: pytest.fail('judge was called'))
    with pytest.raises(ForkError, match='failed validation'):
        fp.report()


def test_report_rejects_target_mutation_during_judge(tmp_path, monkeypatch):
    fp = project(tmp_path)

    def report(*args, **kwargs):
        (tmp_path / 'target.o').write_bytes(object_for(b'\x90'))
        return {'units': [{'name': 'unit', 'functions': [{'name': 'demo', 'fuzzy_match_percent': 100}]}]}

    monkeypatch.setattr('decompnet.forks.project.objdiff.report', report)
    with pytest.raises(ForkError, match='changed during the report'):
        fp.report()
    assert fp.target_validation.status == 'invalid'


def test_diff_rejects_target_mutation_during_judge(tmp_path, monkeypatch):
    fp = project(tmp_path)
    calls = []

    def diff(*args, **kwargs):
        calls.append(kwargs)
        (tmp_path / 'target.o').write_bytes(object_for(b'\x90'))
        return 'would otherwise be a match'

    monkeypatch.setattr('decompnet.forks.project.objdiff.diff', diff)
    with pytest.raises(ForkError, match='changed during comparison'):
        fp.diff(fp.unit('unit'), 'demo')
    assert calls[0]['options'] == fp.diff_options(fp.unit('unit'))


def test_verified_requirement_survives_new_engine_and_project_instances(tmp_path):
    from decompnet.app.forkengine import ForkEngine
    from decompnet.network import Network

    project(tmp_path)
    database = tmp_path / 'test.db'
    net = Network(database)
    with net.store.tx() as db:
        db.execute('INSERT INTO projects(id,slug,root,config) VALUES(?,?,?,?)',
                   (1, 'synthetic', str(tmp_path), '{"kind":"fork"}'))
    engine = ForkEngine(net)
    _, first, _ = engine.get('synthetic')
    assert first.validate_targets().status == 'valid'
    row = net.store.one('SELECT value FROM settings WHERE key=?', 'original-target-evidence:1')
    assert row is not None and row['value'] == 'required'
    (tmp_path / 'config/v/functions.json').unlink()
    # A new database connection and engine model the process restarting, with no in-memory requirement bit.
    restarted = ForkEngine(Network(database))
    _, second, _ = restarted.get('synthetic')
    assert second is not first
    assert second.validate_targets().status == 'invalid'
    with pytest.raises(ForkError, match='no longer available'):
        second.diff(second.unit('unit'), 'demo')


@pytest.mark.parametrize('operation', ['diff', 'report'])
def test_comparison_rejects_changed_object_even_when_new_object_still_validates(tmp_path, monkeypatch, operation):
    fp = project(tmp_path)

    def judge(*args, **kwargs):
        # STT_FILE metadata does not affect code correctness, but a verdict must describe one stable object.
        (tmp_path / 'target.o').write_bytes(object_for(b'\xc3', filename='b.c'))
        return {}

    monkeypatch.setattr(f'decompnet.forks.project.objdiff.{operation}', judge)
    with pytest.raises(ForkError, match='changed during'):
        fp.report() if operation == 'report' else fp.diff(fp.unit('unit'), 'demo')
    assert fp.target_validation.status == 'valid'


def test_invalid_first_elf_declaration_remains_required_after_removal(tmp_path):
    fp = project(tmp_path)
    (tmp_path / 'original.elf').unlink()
    first = fp.validate_targets()
    assert first.status == 'invalid' and first.required
    (tmp_path / 'config/v/functions.json').unlink()
    assert fp.validate_targets().status == 'invalid'


def test_supported_declaration_with_first_invalid_check_persists_across_restart(tmp_path):
    from decompnet.app.forkengine import ForkEngine
    from decompnet.network import Network

    project(tmp_path)
    (tmp_path / 'original.elf').unlink()
    database = tmp_path / 'test.db'
    net = Network(database)
    with net.store.tx() as db:
        db.execute('INSERT INTO projects(id,slug,root,config) VALUES(?,?,?,?)',
                   (1, 'synthetic', str(tmp_path), '{"kind":"fork"}'))
    _, first, _ = ForkEngine(net).get('synthetic')
    assert first.validate_targets().status == 'invalid'
    (tmp_path / 'config/v/functions.json').unlink()
    _, second, _ = ForkEngine(Network(database)).get('synthetic')
    assert second.validate_targets().status == 'invalid'


def test_recognized_unsupported_original_processor_does_not_latch_requirement(tmp_path):
    import struct

    fp = project(tmp_path)
    original = bytearray((tmp_path / 'original.elf').read_bytes())
    struct.pack_into('<H', original, 18, 8)  # EM_MIPS: a recognized unsupported processor, without game data.
    (tmp_path / 'original.elf').write_bytes(original)
    config_path = tmp_path / 'config/v/functions.json'
    config = json.loads(config_path.read_text())
    config['sha1'] = hashlib.sha1(original).hexdigest()
    config_path.write_text(json.dumps(config))
    result = fp.validate_targets()
    assert result.status == 'unavailable' and not result.required
    assert not fp._target_evidence_seen

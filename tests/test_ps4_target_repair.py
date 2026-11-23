"""Missing references may be proposed; existing wrong references must never be replaced."""
import copy
import hashlib

from decompnet.consoles.ps4.scratch import read_elf
from decompnet.consoles.ps4.target_repair import _object, plan_missing_relocations
from tests.test_target_validation import original_image


def fixture(code, relocs=None):
    original = original_image(code + bytes(32))
    config = {'sha1': hashlib.sha1(original).hexdigest(), 'units': {'unit': [
        {'name': 'demo', 'addr': 0x1000, 'offset': 120, 'size': len(code), 'relocs': relocs or []}]}}
    target = _object('unit', config['units']['unit'], read_elf(original))
    return config, original, {'unit': target}


def test_missing_rip_reference_proposes_only_new_operand():
    config, original, targets = fixture(bytes.fromhex('488b0509000000c3'))
    before = copy.deepcopy(config)
    plan = plan_missing_relocations(config, original, targets)
    assert plan.status == 'valid' and len(plan.additions) == 1
    assert plan.config['units']['unit'][0]['relocs'] == [[3, 2, 'D_00001010', -4]]
    assert config == before
    assert targets['unit'] != plan.objects['unit']


def test_existing_wrong_symbol_is_rejected_without_deployable_output():
    config, original, targets = fixture(bytes.fromhex('488b0509000000c3'), [[3, 2, 'D_00001011', -4]])
    plan = plan_missing_relocations(config, original, targets)
    assert plan.status == 'invalid' and not plan.objects and plan.config is None
    assert 'wrong original destination' in plan.errors[0]


def test_missing_branch_and_short_branch_are_proven():
    for code, relocation in [(bytes.fromhex('e80b000000c3'), [1, 2, 'D_00001010', -4]),
                             (bytes.fromhex('750ec3'), [1, 15, 'D_00001010', -1])]:
        config, original, targets = fixture(code)
        plan = plan_missing_relocations(config, original, targets)
        assert plan.status == 'valid'
        assert plan.config['units']['unit'][0]['relocs'] == [relocation]


def test_stale_target_and_original_hash_are_rejected():
    config, original, targets = fixture(b'\xc3')
    targets['unit'] += b'changed'
    assert plan_missing_relocations(config, original, targets).status == 'invalid'
    assert plan_missing_relocations(config, original + b'changed', targets).status == 'invalid'

"""Target objects must preserve instructions and the destinations the original code uses."""
import importlib.util
import io
import json
import shutil
import subprocess
import sys

import pytest
from elftools.elf.elffile import ELFFile

from decompnet import toolchains
from decompnet.consoles.ps4 import scratch


def delinker():
    spec = importlib.util.spec_from_file_location('ps4_delink_test', scratch.TEMPLATE / 'tools/delink.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image(code, base=0x1000):
    return scratch.ELF(code, base, 2, [scratch.Segment(base, 0, len(code), len(code), 1)])


def object_for(code, relocs, size=None):
    f = {'name': 'demo', 'addr': 0, 'size': size or len(code), 'relocs': relocs}
    return delinker().elf64('demo', [f], code, 0)


def refs(blob):
    elf = ELFFile(io.BytesIO(blob))
    syms = elf.get_section_by_name('.symtab')
    return [(r['r_offset'], r['r_info_type'], syms.get_symbol(r['r_info_sym']).name, r['r_addend'])
            for r in elf.get_section_by_name('.rela.text').iter_relocations()]


def test_symbol_index_accounts_for_null_and_reuses_defined_symbols():
    code = bytes.fromhex('e800000000e900000000')
    obj = object_for(code, [(1, 2, 'external', -4), (6, 2, 'demo', -4)])
    assert refs(obj) == [(1, 2, 'external', -4), (6, 2, 'demo', -4)]
    names = [s.name for s in ELFFile(io.BytesIO(obj)).get_section_by_name('.symtab').iter_symbols()]
    assert names.count('demo') == 1


def test_short_branch_masks_only_its_one_byte_operand():
    code = bytes.fromhex('4885ff7405c390909090c3')
    fn = scratch.analyse(image(code), [{'entry': 0x1000, 'end': 0x1005}])[0]
    assert fn.relocs == [(4, 15, 'D_0000100A', -1)]
    obj = object_for(code[:5], fn.relocs)
    assert ELFFile(io.BytesIO(obj)).get_section_by_name('.text').data() == bytes.fromhex('4885ff7400')


def test_internal_short_branch_is_not_relocated_when_true_end_is_known():
    code = bytes.fromhex('4885ff7405c390909090c3')
    fn = scratch.analyse(image(code), [{'entry': 0x1000, 'end': 0x100b}])[0]
    assert fn.relocs == []


def test_rip_displacement_addend_accounts_for_trailing_immediate():
    code = bytes.fromhex('c7050100000007000000c3') + b'\0' * 16
    fn = scratch.analyse(image(code), [{'entry': 0x1000, 'end': 0x100b}])[0]
    assert fn.relocs == [(2, 2, 'D_0000100B', -8)]


def test_discontinuous_body_keeps_span_and_reads_later_block():
    code = bytes.fromhex('c3909090e801000000c3c3')
    fn = scratch.analyse(image(code), [{'entry': 0x1000, 'end': 0x100a,
                                       'ranges': [[0x1000, 0x1001], [0x1004, 0x100a]]}])[0]
    assert fn.size == 10
    assert fn.relocs == [(5, 2, 'D_0000100A', -4)]


def test_relocations_are_not_pruned_by_decoding_padding():
    fn = scratch.Function(0x1000, 5, 'demo', [(1, 2, 'external', -4)])
    scratch.prune_unit_relocs(image(bytes.fromhex('e800000000')), [fn])
    assert fn.relocs == [(1, 2, 'external', -4)]


@pytest.mark.parametrize('reloc', [(-1, 2, 'external', -4), (3, 2, 'external', -4), (1, 99, 'external', 0)])
def test_invalid_relocation_bounds_fail_instead_of_corrupting_neighbors(reloc):
    with pytest.raises(ValueError, match='invalid relocation'):
        object_for(bytes.fromhex('e800000000c3'), [reloc], size=5)


def test_legacy_address_count_is_not_used_as_contiguous_size(tmp_path):
    (tmp_path / 'functions.jsonl').write_text(json.dumps({'a': '0', 's': 5}) + '\n')
    assert scratch.map_functions(tmp_path / 'game.elf', image(b'\xc3'*20, 0).text) is None


def test_explicit_end_wins_over_body_byte_count(tmp_path):
    (tmp_path / 'functions.jsonl').write_text(json.dumps({'a': '0', 's': 5, 'end': 'b',
                                                        'ranges': [[0, 1], [4, 11]]}) + '\n')
    row = scratch.map_functions(tmp_path / 'game.elf', image(b'\xc3'*20, 0).text)[0]
    assert row['end'] == 11
    assert row['ranges'] == [[0, 1], [4, 11]]


def test_generated_project_requires_relocation_identity(tmp_path):
    root = tmp_path / 'project'
    shutil.copytree(scratch.TEMPLATE, root)
    (root / 'config/eboot').mkdir(parents=True)
    (root / 'config/eboot/functions.json').write_text(json.dumps({'units': {}}))
    subprocess.run([sys.executable, str(root / 'tools/configure.py'), 'eboot', '--clang', 'unused-clang'],
                   check=True, capture_output=True)
    cfg = json.loads((root / 'objdiff.json').read_text())
    assert cfg['options']['functionRelocDiffs'] == 'all'


def test_objdiff_rejects_wrong_call_and_accepts_same_call_with_strict_options(tmp_path):
    exe = toolchains.binary('objdiff-cli')
    if not exe.exists():
        pytest.skip('pinned objdiff is not installed here')
    code = bytes.fromhex('e800000000c3')
    target, base = tmp_path / 'target.o', tmp_path / 'base.o'
    target.write_bytes(object_for(code, [(1, 2, 'correct', -4)]))
    for callee, addend, matches in [('wrong', -4, False), ('correct', -3, False), ('correct', -4, True)]:
        base.write_bytes(object_for(code, [(1, 4, callee, addend)]))
        assert binder().normalize_plt(base) == 1
        result = subprocess.run([str(exe), 'diff', '-1', str(target), '-2', str(base), '-o', '-',
                                 '-c', 'functionRelocDiffs=all', 'demo'], capture_output=True, text=True, check=True)
        symbols = json.loads(result.stdout)['left']['symbols']
        percent = next(s['match_percent'] for s in symbols if s['name'] == 'demo')
        assert (percent == 100) == matches


def test_shared_tail_in_another_function_does_not_swallow_intervening_functions():
    code = bytes.fromhex('eb02c390c3')
    rows = [{'entry': 0x1000, 'end': 0x1005, 'ranges': [[0x1000, 0x1002], [0x1004, 0x1005]]},
            {'entry': 0x1002, 'end': 0x1003, 'ranges': [[0x1002, 0x1003]]},
            {'entry': 0x1004, 'end': 0x1005, 'ranges': [[0x1004, 0x1005]]}]
    fn = scratch.analyse(image(code), rows)[0]
    assert fn.size == 2
    assert fn.relocs == [(1, 15, 'func_00001004', -1)]


def test_shared_tail_before_entry_is_external_not_a_negative_relocation():
    code = bytes.fromhex('c39090ebfb')
    rows = [{'entry': 0x1000, 'end': 0x1001, 'ranges': [[0x1000, 0x1001]]},
            {'entry': 0x1003, 'end': 0x1005, 'ranges': [[0x1000, 0x1001], [0x1003, 0x1005]]}]
    fn = scratch.analyse(image(code), rows)[1]
    assert fn.size == 2
    assert fn.relocs == [(1, 15, 'func_00001000', -1)]


def test_typed_linkage_declaration_is_allowed_but_assembly_is_not():
    from decompnet.app.forkengine import _CHEAT, _strip_comments

    declaration = 'extern __typeof__(old) recovered __asm__("correct") __attribute__((visibility("hidden")));'
    assert not _CHEAT.search(_strip_comments(declaration))
    for text in ['__asm__("correct");', 'asm volatile("nop");',
                 declaration.replace('"correct"', '"correct\\n nop"'),
                 declaration + ' __asm__("nop");']:
        assert _CHEAT.search(_strip_comments(text))


def test_rip_reference_to_bss_is_relocated_without_file_backing():
    code = bytes.fromhex('488d05f90f0000c3')
    elf = image(code)
    elf.segments.append(scratch.Segment(0x2000, len(code), 0, 0x100, 6))
    fn = scratch.analyse(elf, [{'entry': 0x1000, 'end': 0x1008}])[0]
    assert fn.relocs == [(3, 2, 'D_00002000', -4)]
    assert elf.offset(0x2000) is None
    assert elf.read(0x2000, 8) == b''
    assert not elf.contains(0x2000)


def test_rip_reference_past_mapped_memory_stays_literal():
    code = bytes.fromhex('488d05f9100000c3')
    elf = image(code)
    elf.segments.append(scratch.Segment(0x2000, len(code), 0, 0x100, 6))
    fn = scratch.analyse(elf, [{'entry': 0x1000, 'end': 0x1008}])[0]
    assert fn.relocs == []


def test_bss_reference_with_trailing_immediate_keeps_pc_addend():
    code = bytes.fromhex('c705f60f000007000000c3')
    elf = image(code)
    elf.segments.append(scratch.Segment(0x2000, len(code), 0, 0x100, 6))
    fn = scratch.analyse(elf, [{'entry': 0x1000, 'end': 0x100b}])[0]
    assert fn.relocs == [(2, 2, 'D_00002000', -8)]


def test_sce_relro_is_mapped_but_unrelated_metadata_is_not():
    import struct
    data = bytearray(0x400)
    data[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', data, 18, 62)
    struct.pack_into('<QQ', data, 24, 0x1000, 64)
    struct.pack_into('<HH', data, 54, 56, 3)
    for i, (kind, va) in enumerate(((1, 0x1000), (0x61000010, 0x2000), (0x61000000, 0x3000))):
        struct.pack_into('<IIQQQQQQ', data, 64 + 56*i, kind, 4, 0x200 + i*32, va, va, 16, 32, 8)
    elf = scratch.read_elf(bytes(data))
    assert elf.offset(0x2004) == 0x224
    assert elf.segment_at(0x2018) is not None
    assert elf.read(0x2018, 8) == b''
    assert elf.segment_at(0x3000) is None


def test_builtin_alias_requires_matching_target_call_in_same_function(tmp_path):
    spec = importlib.util.spec_from_file_location('binder_test', scratch.TEMPLATE / 'tools/bind.py')
    binder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(binder)
    base = tmp_path / 'base.o'
    target = tmp_path / 'text_00001000.o'
    original = object_for(bytes.fromhex('e800000000c3'), [(1, 4, '__stack_chk_fail', -4)])
    base.write_bytes(original)
    target.write_bytes(object_for(bytes.fromhex('e800000000c3'), [(1, 2, 'different', -4)]))
    aliases = {'__stack_chk_fail': 'fail'}
    assert binder.bind_builtins(base, target, aliases) == 0
    assert base.read_bytes() == original
    target.write_bytes(object_for(bytes.fromhex('e800000000c3'), [(1, 2, 'fail', -4)]))
    assert binder.bind_builtins(base, target, aliases) == 1
    assert refs(base.read_bytes()) == [(1, 2, 'fail', -4)]
    assert binder.bind_builtins(base, target, aliases) == 0
    base.write_bytes(original)
    target.write_bytes(object_for(bytes.fromhex('e900000000c3'), [(1, 2, 'fail', -4)]))
    assert binder.bind_builtins(base, target, aliases) == 0
    assert base.read_bytes() == original


def test_named_guard_slot_preserves_load_and_uses_target_symbol(tmp_path):
    spec = importlib.util.spec_from_file_location('guard_binder_test', scratch.TEMPLATE / 'tools/bind.py')
    binder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(binder)
    code = bytes.fromhex('488b0500000000c3')
    base, target = tmp_path / 'base.o', tmp_path / 'text_00001000.o'
    import struct
    raw = bytearray(object_for(code, [(3, 2, '__stack_chk_guard', -4)]))
    rela = ELFFile(io.BytesIO(raw)).get_section_by_name('.rela.text')['sh_offset']
    struct.pack_into('<I', raw, rela + 8, 9)
    base.write_bytes(raw)
    target.write_bytes(object_for(code, [(3, 2, 'D_00002000', -4)]))
    assert binder.bind_builtins(base, target, {'__stack_chk_guard': 'D_00002000'}) == 1
    assert refs(base.read_bytes()) == [(3, 2, 'D_00002000', -4)]
    assert ELFFile(io.BytesIO(base.read_bytes())).get_section_by_name('.text').data() == code


def test_each_target_function_has_its_own_section_and_original_address(tmp_path):
    code = bytes.fromhex('e800000000c3') + b'\x90'*10 + b'\xc3'
    functions = [{'name': 'caller', 'addr': 0x1000, 'size': 6, 'relocs': [(1, 2, 'callee', -4)]},
                 {'name': 'callee', 'addr': 0x1010, 'size': 1, 'relocs': []}]
    obj = delinker().elf64('unit', functions, code, 0x1000)
    elf = ELFFile(io.BytesIO(obj))
    syms = elf.get_section_by_name('.symtab')
    caller = syms.get_symbol_by_name('caller')[0]
    callee = syms.get_symbol_by_name('callee')[0]
    assert caller['st_shndx'] != callee['st_shndx']
    assert caller['st_value'] == callee['st_value'] == 0
    assert elf.get_section(caller['st_shndx']).data() == code[:6]
    assert elf.get_section(callee['st_shndx']).data() == b'\xc3'
    assert refs(obj) == [(1, 2, 'callee', -4)]
    target = tmp_path / 'renamed_unit.o'
    target.write_bytes(obj)
    assert binder().game_addresses(target) == {'caller': 0x1000, 'callee': 0x1010}


def test_plt_normalization_preserves_every_part_of_the_reference_except_its_type(tmp_path):
    obj = tmp_path / 'caller.o'
    code = bytes.fromhex('e800000000c3')
    original = got_object_for(code, [(1, 4, 'precise_callee', -7)])
    obj.write_bytes(original)
    assert binder().normalize_plt(obj) == 1
    assert refs(obj.read_bytes()) == [(1, 2, 'precise_callee', -7)]
    before, after = (ELFFile(io.BytesIO(blob)).get_section_by_name('.text').data()
                     for blob in [original, obj.read_bytes()])
    assert before == after
    assert binder().normalize_plt(obj) == 0


def test_explicit_guard_binding_uses_the_later_functions_original_address(tmp_path):
    import struct

    code = bytes.fromhex('488b0500000000c3')
    target = tmp_path / 'arbitrary_unit.o'
    target.write_bytes(delinker().elf64('unit', [
        {'name': 'first', 'addr': 0x1000, 'size': 1, 'relocs': []},
        {'name': 'demo', 'addr': 0x1010, 'size': 8, 'relocs': []},
    ], b'\xc3'+b'\x90'*15+code, 0x1000))
    obj = tmp_path / 'demo.o'
    obj.write_bytes(got_object_for(code, [(3, 9, '__stack_chk_guard', -4)]))
    assert binder().bind(obj, target, 0x2000) == 1
    text = ELFFile(io.BytesIO(obj.read_bytes())).get_section_by_name('.text').data()
    assert struct.unpack_from('<i', text, 3)[0] == 0x2000 - (0x1010 + 7)
    assert refs(obj.read_bytes()) == []


@pytest.mark.parametrize('addr,size', [(0xfff, 1), (0x1000, 0), (0x1000, 4)])
def test_invalid_target_extents_fail_instead_of_truncating_functions(addr, size):
    with pytest.raises(ValueError, match='invalid function extent'):
        delinker().elf64('unit', [{'name': 'demo', 'addr': addr, 'size': size, 'relocs': []}], b'\xc3', 0x1000)


def test_original_addresses_are_read_from_the_import_config(tmp_path):
    from types import SimpleNamespace

    from decompnet import consoles

    conf = tmp_path / 'config/demo/functions.json'
    conf.parent.mkdir(parents=True)
    conf.write_text(json.dumps({'units': {'first': [{'name': 'a', 'addr': 0x1003}],
                                        'second': [{'name': 'a', 'addr': 0x2001}]}}))
    fp = SimpleNamespace(root=tmp_path, manifest={'version': 'demo'})
    assert consoles.get('ps4').function_addresses(fp) == {('first', 'a'): 0x1003, ('second', 'a'): 0x2001}
    assert consoles.get('pc').function_addresses(fp) == {}




def binder():
    spec = importlib.util.spec_from_file_location('ps4_bind_test', scratch.TEMPLATE / 'tools/bind.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def got_object_for(code, relocations):
    """Model a compiler's GOT relocations without widening the target delinker."""
    import struct

    blob = bytearray(object_for(code, [(offset, 2, name, addend) for offset, _, name, addend in relocations]))
    elf = ELFFile(io.BytesIO(blob))
    section = elf.get_section_by_name('.rela.text')
    for index, (_, kind, _, _) in enumerate(relocations):
        at = section['sh_offset'] + index*24 + 8
        info, = struct.unpack_from('<Q', blob, at)
        struct.pack_into('<Q', blob, at, (info & ~0xffffffff) | kind)
    return bytes(blob)




def test_sync_restores_original_addresses_in_existing_imports(tmp_path, monkeypatch):
    from decompnet.app.engine import RomNetwork
    from decompnet.forks.project import ForkProject

    net = RomNetwork(tmp_path / 'app.db', tmp_path / 'ws')
    root = tmp_path / 'ws' / 'p'
    root.mkdir(parents=True)
    (root / 'objdiff.json').write_text(json.dumps({'units': []}))
    (root / '.decomp.wtf.json').write_text(json.dumps({'version': 'demo'}))
    cfg = root / 'config/demo/functions.json'
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({'units': {'unit': [{'name': 'demo', 'addr': 0x1040}]}}))
    net.forks._register('p', 'P', 'fork', root,
                        {'kind': 'fork', 'platform': 'ps4', 'version': 'demo', 'fork_kind': 'objdiff', 'ready': False}, 'x')
    report = {'measures': {}, 'units': [{'name': 'unit', 'functions': [{'name': 'demo', 'size': '4'}]}]}
    monkeypatch.setattr(ForkProject, 'report', lambda self: report)
    monkeypatch.setattr(ForkProject, 'units', lambda self: [])
    net.forks.sync('p', log=lambda *_: None)
    assert net.store.one('SELECT vram FROM functions')['vram'] == 0x1040
    with net.store.tx() as db:
        db.execute('UPDATE functions SET vram=NULL')
    net.forks.sync('p', log=lambda *_: None)
    assert net.store.one('SELECT vram FROM functions')['vram'] == 0x1040

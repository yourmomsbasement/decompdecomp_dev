"""Synthetic instructions exercise advisory analysis without a game, compiler, or running app."""

from types import SimpleNamespace

from decompnet.app import block_inspection as inspection
from decompnet.arch.x86_flow import effects
from decompnet.codeblocks import Instruction
from decompnet.elfobj import FunctionObj


def test_branch_boundaries_keep_fallthrough_back_edges_and_external_destinations():
    code = [Instruction(0, 'cmp eax, 0'), Instruction(3, 'je 9', 9), Instruction(5, 'add eax, 1'),
            Instruction(8, 'jmp 0', 0), Instruction(9, 'jmp 100', 100)]
    blocks, _, _ = inspection.analyze(code, 'x86')
    assert [block.start for block in blocks] == [0, 5, 9]
    assert blocks[0].successors == [9, 5]
    assert blocks[0].predecessors == [5]
    assert blocks[2].successors == [100]


def test_indirect_jump_is_unknown_and_has_no_guessed_fallthrough():
    blocks, _, _ = inspection.analyze([Instruction(0, 'jmp eax'), Instruction(2, 'ret')], 'x86')
    assert blocks[0].unknown
    assert not blocks[0].successors


def test_relocated_jump_does_not_claim_placeholder_destination():
    blocks, _, _ = inspection.analyze([Instruction(0, 'jmp symbol', 0, 'symbol')], 'x86')
    assert blocks[0].unknown and blocks[0].successors == []


def test_capstone_tracks_partial_registers_stack_flags_and_call_barriers():
    # mov al,1; mov eax,[rsp+8]; cmp eax,0; call rax; ret
    obj = FunctionObj('synthetic', 'x86_64', False, bytes.fromhex('b0018b44240883f800ffd0c3'))
    facts = effects(obj)
    assert 'a' in facts[0]['use'] and 'a' in facts[0]['def']
    assert not facts[2]['partial']
    assert facts[2]['stack'] == ['read 4 bytes [rsp+0x8]']
    assert facts[6]['flags'] and 'zf' in facts[6]['flag_def']
    assert facts[9]['barrier']
    assert facts[9]['stack']


def test_local_mismatches_follow_target_rows_and_reject_stale_targets():
    code = [Instruction(0, 'mov eax, 1'), Instruction(5, 'ret')]
    rows = [(' ', 'mov eax, 1', 'mov eax, 1'), ('>', '', 'nop'), ('|', 'ret', 'ret 4')]
    local, note = inspection.localized_rows(code, rows)
    assert not note
    assert len(local[5]) == 2
    local, note = inspection.localized_rows(code, [(' ', 'stale instruction', '')])
    assert not local and 'unavailable' in note


def test_unsupported_processors_abstain_instead_of_guessing_delay_slots():
    blocks, facts, note = inspection.analyze([Instruction(0, 'jr ra')], 'mips')
    assert not blocks and not facts and 'delay slots' in note


def test_analysis_caps_instructions(monkeypatch):
    monkeypatch.setattr(inspection, 'MAX_INSNS', 3)
    blocks, _, note = inspection.analyze([Instruction(n, 'nop') for n in range(5)], 'x86')
    assert sum(len(block.code) for block in blocks) == 3
    assert 'capped' in note


def test_selection_pagination_and_output_limits_do_not_build_or_write(monkeypatch):
    code = [Instruction(0, 'nop'), Instruction(1, 'nop'), Instruction(2, 'ret'), Instruction(3, 'ret')]
    net = SimpleNamespace(_project=lambda slug: {'slug': slug, 'arch': 'x86'},
                          _function=lambda name, slug: {'id': 1, 'name': name}, _last_diffs={})
    monkeypatch.setattr(inspection, '_target', lambda *args: (code, None))
    listing = inspection.inspect_blocks(net, SimpleNamespace(id=2), 'synthetic', 'fn', limit=1)
    assert 'Blocks 0:1 of 2' in listing
    selected = inspection.inspect_blocks(net, SimpleNamespace(id=2), 'synthetic', 'fn', offset='0x1', instruction_limit=1)
    assert 'Instructions truncated to 1/3' in selected
    assert 'data flow unavailable' in selected
    assert net._last_diffs == {}


def test_project_target_cache_invalidates_when_object_changes(tmp_path, monkeypatch):
    from decompnet.app.code_blocks import Finder

    path = tmp_path / 'synthetic.o'
    path.write_bytes(b'first')
    calls = []

    def diff(*args, **kwargs):
        calls.append(path.read_bytes())
        return SimpleNamespace(block_code={'fn': [(0, 'ret', None, '')]})

    monkeypatch.setattr('decompnet.app.code_blocks.objdiff.diff', diff)
    reader = Finder(SimpleNamespace())
    project = SimpleNamespace(root=tmp_path, diff_options=lambda unit: {})
    unit = SimpleNamespace(name='synthetic', target=path)
    reader.unit_code(project, unit)
    reader.unit_code(project, unit)
    path.write_bytes(b'replacement')
    reader.unit_code(project, unit)
    assert calls == [b'first', b'replacement']
    assert path.read_bytes() == b'replacement'


def test_rom_relocated_control_keeps_original_target_from_becoming_a_false_cfg_edge(monkeypatch):
    from decompnet.elfobj import Reloc

    obj = FunctionObj('fn', 'x86', False, bytes.fromhex('e9fbffffff'), [Reloc(1, 1, 'external', 0)])
    net = SimpleNamespace(fork_slug=lambda slug: None, _target_obj=lambda *args: obj)
    reader = SimpleNamespace(rom_code=lambda *args: [Instruction(0, 'jmp 0', 0)])
    monkeypatch.setattr(inspection, 'finder', lambda net: reader)
    code, returned = inspection._target(net, {'slug': 'synthetic'}, {})
    assert returned is obj
    blocks, _, _ = inspection.analyze(code, 'x86', obj)
    assert blocks[0].unknown and not blocks[0].successors


def test_32_bit_stack_access_is_read_from_capstone_detail():
    obj = FunctionObj('fn', 'x86', False, bytes.fromhex('8945fcc3'))
    assert effects(obj)[0]['stack'] == ['write 4 bytes [ebp-0x4]']


def test_project_with_blank_database_architecture_uses_raw_target_arch(monkeypatch):
    obj = FunctionObj('fn', 'x86_64', False, bytes.fromhex('b801000000c3'))
    code = [Instruction(0, 'mov eax, 1'), Instruction(5, 'ret')]
    net = SimpleNamespace(_project=lambda slug: {'slug': slug, 'arch': ''},
                          _function=lambda name, slug: {'id': 1, 'name': name}, _last_diffs={})
    monkeypatch.setattr(inspection, '_target', lambda *args: (code, obj))
    text = inspection.inspect_blocks(net, SimpleNamespace(id=2), 'synthetic', 'fn', offset=0)
    assert 'Block +0x0' in text and 'writes: a' in text
    assert 'unavailable for this processor' not in text


def test_legacy_scalar_sse_write_retains_old_vector_lanes_and_is_not_a_full_definition():
    obj = FunctionObj('fn', 'x86_64', False, bytes.fromhex('f30f10c1c3'))  # movss xmm0,xmm1; ret
    fact = effects(obj)[0]
    assert 'v0' in fact['use'] and 'v1' in fact['use']
    assert 'v0' in fact['partial'] and 'v0' in fact['def']
    assert 'v0' not in fact['full_def']


def test_conditional_move_retains_previous_destination_and_is_not_a_full_definition():
    obj = FunctionObj('fn', 'x86_64', False, bytes.fromhex('0f45c1c3'))  # cmovne eax,ecx; ret
    fact = effects(obj)[0]
    assert 'a' in fact['use'] and 'c' in fact['use']
    assert 'a' in fact['partial'] and 'a' not in fact['full_def']
    assert 'zf' in fact['flag_use']


def test_absolute_index_only_and_tls_operands_have_no_stack_base():
    for code in ('488b042500000000c3', '488b04cd00000000c3', '64488b042500000000c3'):
        facts = effects(FunctionObj('fn', 'x86_64', False, bytes.fromhex(code)))
        assert facts[0]['stack'] == []
        assert 'a' in facts[0]['def']

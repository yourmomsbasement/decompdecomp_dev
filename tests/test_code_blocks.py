"""Synthetic code only: operand-aware similarity must never become an alternative matching judge."""

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from decompnet import codeblocks, toolchains
from decompnet.agents.api import api_tool_schemas
from decompnet.app import code_blocks, tools
from decompnet.codeblocks import Instruction
from decompnet.forks import objdiff

BODY = ['vmulps xmm0, xmm1, xmm2', 'vaddps xmm3, xmm0, xmm1', 'vshufps xmm4, xmm3, xmm0, 0x55',
        'vsubps xmm5, xmm4, xmm2', 'vdivps xmm6, xmm5, xmm1', 'vsqrtps xmm7, xmm6',
        'vmaxps xmm8, xmm7, xmm0', 'vminps xmm9, xmm8, xmm2', 'vxorps xmm10, xmm9, xmm3']


def instructions(lines=BODY, start=0):
    return [Instruction(start+i*4, line) for i, line in enumerate(lines)]


def renamed():
    import re
    return [re.sub(r'xmm(\d+)', lambda m: 'xmm'+str((int(m[1])+3) % 16), line) for line in BODY]


def test_consistent_register_renaming_and_full_fragment():
    target, donor = instructions(), instructions(renamed(), 100)
    hits, exhausted = codeblocks.search(target, {'donor': donor}, 'x86_64')
    assert not exhausted
    assert len(hits) == 1 and hits[0].count == len(BODY)
    assert not hits[0].exact_operands


@pytest.mark.parametrize('change', ['constant', 'dependency', 'width', 'symbol', 'stack', 'relocation'])
def test_meaningful_operand_differences_are_not_erased(change):
    target, donor = instructions(), instructions(renamed())
    if change == 'constant':
        donor[2] = Instruction(8, donor[2].text.replace('0x55', '0xAA'))
    elif change == 'dependency':
        donor[4] = Instruction(16, donor[4].text.replace('xmm4', 'xmm15'))
    elif change == 'width':
        donor[4] = Instruction(16, donor[4].text.replace('xmm', 'ymm'))
    elif change == 'symbol':
        target[4], donor[4] = Instruction(16, 'mov rax, [global_a]'), Instruction(16, 'mov rbx, [global_b]')
    elif change == 'stack':
        target[4], donor[4] = Instruction(16, 'mov rax, [rbp-0x10]'), Instruction(16, 'mov rbx, [rbp-0x20]')
    else:
        target[4] = Instruction(16, BODY[4], relocation='[2,0,"a"]')
        donor[4] = Instruction(16, renamed()[4], relocation='[2,0,"b"]')
    assert not codeblocks.search(target, {'donor': donor}, 'x86_64')[0]


def test_register_alias_relationships_are_preserved():
    assert codeblocks.canonical(('mov eax, ebx', 'add rax, rbx'), 'x86_64') == codeblocks.canonical(
        ('mov ecx, esi', 'add rcx, rsi'), 'x86_64')
    assert codeblocks.canonical(('mov eax, ebx', 'add rax, rbx'), 'x86_64') != codeblocks.canonical(
        ('mov ecx, esi', 'add rdx, rsi'), 'x86_64')
    assert codeblocks.canonical(('mov rax, <rbx>',), 'x86_64') != codeblocks.canonical(
        ('mov rcx, <rdx>',), 'x86_64')


def test_incoming_branch_targets_and_calls_split_regions():
    target = instructions(BODY[:4] + ['call helper'] + BODY[4:])
    assert not codeblocks.search(target, {'donor': instructions()}, 'x86_64')[0]
    target = instructions() + [Instruction(40, 'jmp 0x10', target=16)]
    assert not codeblocks.search(target, {'donor': instructions()}, 'x86_64')[0]


def test_mips_delay_slot_is_not_folded_into_fallthrough():
    code = instructions(['add t0, t1, t2', 'beq t0, t1, .L20', 'add t3, t4, t5', 'xor t0, t2, t3'])
    assert list(codeblocks.regions(code, 'mips')) == [(0, 1), (3, 4)]


def test_no_common_prologue_or_tiny_false_hits():
    for body in (['push rbx', 'mov rbx, rdi']*12, BODY[:7], ['nop']*30):
        assert not codeblocks.search(instructions(body), {'donor': instructions(body)}, 'x86_64')[0]


def test_large_function_tail_beyond_whole_function_similarity_limit():
    target = instructions(['nop']*1100+BODY+['ret'])
    hits, _ = codeblocks.search(target, {'donor': instructions()}, 'x86_64')
    assert hits[0].start == 1100


def test_overlap_deduplication_and_search_budget(monkeypatch):
    hits, _ = codeblocks.search(instructions(), {'first': instructions(), 'second': instructions()}, 'x86_64')
    assert len(hits) == 1
    monkeypatch.setattr(codeblocks, 'MAX_COMPARISONS', 1)
    _, exhausted = codeblocks.search(instructions(), {'first': instructions(BODY*4)}, 'x86_64')
    assert exhausted


@pytest.fixture
def project(tmp_path, monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE projects(id,slug,arch);
        INSERT INTO projects VALUES(1,'test','x86_64');
        CREATE TABLE functions(id,name,project_id,unit,meta,status,n_insns,matched_source);
        INSERT INTO functions VALUES(1,'large',1,'unit','{"symbol":"large"}','open',500,NULL);
        INSERT INTO functions VALUES(2,'donor',1,'unit','{"symbol":"donor"}','matched',9,'int donor(void) { return 1; }');''')
    target = tmp_path/'target.o'
    target.write_bytes(b'synthetic immutable target')
    source = tmp_path/'unit.c'
    source.write_text('int donor(void) { return 1; }')
    unit = SimpleNamespace(name='unit', target=target)
    fp = SimpleNamespace(root=tmp_path, unit=lambda name: unit, units=lambda: [unit], diff_options=lambda unit: {})
    index = {'unit': {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                      'ops': {'large': [x.op for x in instructions()], 'donor': [x.op for x in instructions()]}}}
    store = SimpleNamespace(one=lambda sql, *args: db.execute(sql, args).fetchone(),
                            all=lambda sql, *args: db.execute(sql, args).fetchall())
    proj = store.one('SELECT * FROM projects')
    engine = SimpleNamespace(get=lambda slug: (proj, fp, {}), _ilock=threading.RLock(),
                             _meta=lambda row: json.loads(row['meta']), _index_of=lambda *args: index,
                             _unit_index=lambda *args, **kw: index["unit"] | {"sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    class Net:
        pass
    net = Net()
    net.store, net.forks = store, engine
    net._project = lambda slug: proj
    net._function = lambda name, slug: store.one('SELECT * FROM functions WHERE name=?', name)
    net.fork_slug = lambda slug: slug
    calls = []

    def diff(target, base, symbol, **kwargs):
        assert base is None and kwargs['blocks'] is True  # target-only: no build/candidate access
        calls.append(target)
        return SimpleNamespace(block_code={
            'large': [(i.offset, i.text, i.target, i.relocation) for i in instructions()],
            'donor': [(i.offset, i.text, i.target, i.relocation) for i in instructions(renamed())]})

    monkeypatch.setattr(objdiff, 'diff', diff)
    cached_finder = code_blocks.finder
    before = source.read_bytes(), target.read_bytes(), list(db.iterdump())
    # Any attempted progress/claim/score update fails, including accidental notes or automatic acceptance.
    db.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} else sqlite3.SQLITE_OK)
    yield SimpleNamespace(net=net, db=db, source=source, target=target, before=before, calls=calls, unit=unit, fp=fp)
    assert (source.read_bytes(), target.read_bytes()) == before[:2] or getattr(net, 'changed_target', False)
    assert list(db.iterdump()) == before[2]
    db.close()
    cached_finder.cache_clear()


def test_tool_returns_offsets_and_recorded_source_without_mutation(project):
    output = code_blocks.find_code_blocks(project.net, 'test', 'large')
    assert 'target +0x0..+0x20' in output and 'donor (unit)' in output
    assert 'recorded matched donor' in output and 'int donor(void)' in output
    assert 'not proof' in output
    assert len(project.calls) == 1
    assert 'large (unit)' not in output  # never suggest self as donor


def test_target_cache_is_reused_and_invalidated(project):
    code_blocks.find_code_blocks(project.net, 'test', 'large')
    code_blocks.find_code_blocks(project.net, 'test', 'large')
    assert len(project.calls) == 1
    project.target.write_bytes(b'replaced target')
    project.net.changed_target = True
    code_blocks.find_code_blocks(project.net, 'test', 'large')
    assert len(project.calls) == 2


def test_large_brief_hints_and_autopilot_warm_are_read_only(project):
    fn = project.net._function('large', 'test')
    assert 'Similar code blocks' in code_blocks.hints(project.net, fn)
    assert code_blocks.hints(project.net, dict(fn) | {'n_insns': 199}) == ''
    assert code_blocks.hints(project.net, dict(fn) | {'status': 'matched'}) == ''
    state = {'stop': threading.Event()}
    code_blocks.warm(project.net, project.net._project('test'), state)
    assert state['block_scanned'] == state['block_hints'] == 1
    state['stop'].set()
    code_blocks.warm(project.net, project.net._project('test'), state)
    assert state['block_scanned'] == 0


def test_advisory_failure_cannot_block_claiming(project, monkeypatch):
    monkeypatch.setattr(code_blocks, 'finder', lambda net: (_ for _ in ()).throw(ValueError('bad object')))
    assert code_blocks.hints(project.net, project.net._function('large', 'test')) == ''


def test_api_and_mcp_expose_tool_in_both_modes(project):
    for fork in (False, True):
        assert 'find_code_blocks' in tools.tool_names(fork)
        assert 'find_code_blocks' in {t['function']['name'] for t in api_tool_schemas(fork)}
    assert tools.build_mcp(project.net, lambda ctx: (None, 'test'))


def test_real_target_object_block_metadata(tmp_path):
    clang = os.environ.get('DECOMP_TEST_CLANG')
    if not clang or not Path(clang).is_file() or toolchains.installed('objdiff-cli') is None:
        pytest.skip('needs explicit pinned Clang and installed objdiff-cli')
    source = '.intel_syntax noprefix\n.text\n'
    for name, body in [('large', BODY), ('donor', renamed())]:
        source += f'.globl {name}\n.type {name},@function\n{name}:\n'+'\n'.join(body)+'\nret\n'
        source += f'.size {name},.-{name}\n'
    asm, target = tmp_path/'sample.s', tmp_path/'sample.o'
    asm.write_text(source)
    subprocess.run([clang, '-mavx', '-c', str(asm), '-o', str(target)], check=True, timeout=30)
    before = hashlib.sha256(target.read_bytes()).digest()
    diff = objdiff.diff(target, None, None, cwd=tmp_path, blocks=True)
    assert set(diff.block_code) == {'large', 'donor'}
    a, b = ([Instruction(*row) for row in diff.block_code[name]] for name in ('large', 'donor'))
    assert a[0].offset == b[0].offset == 0
    hits, _ = codeblocks.search(a, {'donor': b}, 'x86_64')
    assert hits[0].count == 9
    assert hashlib.sha256(target.read_bytes()).digest() == before
    ordinary = objdiff.diff(target, target, 'large', cwd=tmp_path)
    assert ordinary.matched and ordinary.block_code == {}


def test_rom_mode_uses_symbolic_target_instructions():
    from decompnet import scoring
    from decompnet.elfobj import FunctionObj

    words = [0x00854021, 0x01064826, 0x01275024, 0x01445825, 0x01656023,
             0x000c68c0, 0x01a6702b, 0x01c77827, 0x03e00008, 0]
    code = b''.join(word.to_bytes(4, 'big') for word in words)
    target = FunctionObj('work', 'mips', True, code)
    proj = {'id': 1, 'slug': 'rom', 'arch': 'mips'}
    fn = dict(id=1, name='large', unit='unit', status='open', vram=0x80001000, size=len(code), n_insns=500)
    donor = dict(fn, id=2, name='donor', status='matched', vram=0x80002000)
    class Net:
        pass
    net = Net()
    net._project, net._function = lambda slug: proj, lambda *args: fn
    net.fork_slug = lambda slug: None
    net._target_obj = lambda *args: target
    net._rom_ops = lambda proj: {(donor['vram'], donor['size']): [i.mnemonic for i in scoring.disassemble(target)]}
    net.store = SimpleNamespace(all=lambda *args: [donor], one=lambda sql, *args:
                                {'matched_source': 'int donor(void) { return 7; }'} if 'matched_source' in sql else donor)
    text = code_blocks.find_code_blocks(net, 'rom', 'large')
    assert '8 instructions' in text and 'recorded matched donor' in text
    code_blocks.finder.cache_clear()


def test_rom_claim_brief_includes_large_function_findings(project, monkeypatch):
    from decompnet.app.engine.briefs import BriefMixin

    note, facts = code_blocks.finder(project.net).find('test', 'large')
    monkeypatch.setattr(code_blocks, 'finder', lambda net: SimpleNamespace(find=lambda *args, **kw: (note, facts)))
    class Base:
        def _brief(self, fn, agent, header):
            return 'Original brief'
    class Brief(BriefMixin, Base):
        pass
    brief = Brief()
    brief.store = SimpleNamespace(one=lambda *args: {'id': 1, 'slug': 'test', 'config': '{}'})
    brief.cfg, brief.is_rom = lambda row: {'kind': 'rom'}, lambda row: True
    fn = dict(project.net._function('large', 'test'), best_source='existing C')
    result = brief._brief(fn, None, 'CLAIM')
    assert result.startswith('Original brief') and 'Similar code blocks' in result


def test_autopilot_prepares_hints_even_without_machine_drafts(monkeypatch):
    from contextlib import contextmanager

    from decompnet.app.engine.autopilot import AutopilotMixin

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('CREATE TABLE projects(id,config); INSERT INTO projects VALUES(1,\'{}\');')
    @contextmanager
    def tx():
        yield db
    net = SimpleNamespace(local_agent=lambda name: (None, None), _project=lambda slug: {'id': 1, 'slug': slug},
                          links={'test': True}, store=SimpleNamespace(tx=tx))
    warmed = []
    def warm(net, proj, state):
        warmed.append(proj['slug'])
        state.update(block_scanned=1, block_hints=1)
    monkeypatch.setattr(code_blocks, 'warm', warm)
    state = dict(stop=threading.Event(), matched=0, tried=0, twins=0, near=0)
    AutopilotMixin._autopilot_run(net, 'test', [], state, 1, lambda *args: pytest.fail('no drafts'))
    assert warmed == ['test'] and state['state'] == 'done'
    cfg = json.loads(db.execute('SELECT config FROM projects').fetchone()[0])
    assert cfg['autopilot']['block_hints'] == 1
    db.close()


def test_x86_implicit_registers_are_not_arbitrarily_renamed():
    a = ('mov eax, ebx', 'mul ecx')
    b = ('mov edx, ebx', 'mul ecx')
    assert codeblocks.canonical(a, 'x86_64') != codeblocks.canonical(b, 'x86_64')


def test_repeated_block_lookups_do_not_reread_stable_target_objects(project, monkeypatch):
    from decompnet.app import target_stamps
    now = target_stamps.time.time_ns()
    monkeypatch.setattr(target_stamps.time, 'time_ns', lambda: now + 3_000_000_000)
    opened = []
    original = Path.open
    def read(path, *args, **kwargs):
        if path == project.target:
            opened.append(1)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', read)
    first = code_blocks.find_code_blocks(project.net, 'test', 'large')
    assert 'donor' in first
    assert code_blocks.find_code_blocks(project.net, 'test', 'large') == first
    assert len(opened) == 1, 'the unchanged object was reread for every hint'


def test_decoded_blocks_are_refreshed_after_a_same_size_mtime_restore(project):
    cache = code_blocks.finder(project.net)
    cache.unit_code(project.fp, project.unit)
    stamp = project.target.stat()
    project.target.write_bytes(b'x' * stamp.st_size)
    os.utime(project.target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    project.net.changed_target = True
    cache.unit_code(project.fp, project.unit)
    assert len(project.calls) == 2

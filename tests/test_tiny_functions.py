"""Tiny templates are only candidates; claims and the normal strict judge remain authoritative."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from decompnet import tiny
from decompnet.agents.api import api_tool_schemas
from decompnet.app import tiny_functions as app
from decompnet.app import tools
from decompnet.codeblocks import Instruction


def code(*lines):
    return [Instruction(i, text) for i, text in enumerate(lines)]


@pytest.mark.parametrize('processor,lines', [('x86_64', ['ret']), ('ppc', ['blr']), ('arm', ['bx lr']),
                                           ('thumb', ['bx lr']), ('mips', ['jr $ra', 'nop'])])
def test_empty_return_preserves_explicit_signature(processor, lines):
    source, reason = tiny.candidate('void f(int x) { existing(); }', 'f', processor, code(*lines))
    assert source == 'void f(int x) {  }' and reason == 'empty'


@pytest.mark.parametrize('text,lines', [('int f(void){}', ['ret']), ('void f(){}', ['ret']),
    ('void f(int){}', ['ret']), ('void f(int x, ...){}', ['ret']), ('void f(void){}', ['nop']),
    ('void f(void){}', ['jmp rax']), ('void f(void){}', ['ret 8'])])
def test_ambiguous_signatures_padding_and_indirect_jumps_are_skipped(text, lines):
    assert tiny.candidate(text, 'f', 'x86_64', code(*lines))[0] is None


def test_constant_trap_and_compatible_tail_templates():
    assert 'return 7;' in tiny.candidate('int f(void){}', 'f', 'x86_64', code('mov eax, 7', 'ret'))[0]
    assert 'return 0;' in tiny.candidate('unsigned long f(void){}', 'f', 'x86_64', code('xor eax, eax', 'ret'))[0]
    assert '__builtin_trap();' in tiny.candidate('void f(void){}', 'f', 'x86_64', code('ud2'))[0]
    jump = [Instruction(0, 'jmp dest', relocation='[4,-4,"dest"]')]
    source = 'int dest(int x);\nint f(int a) { return 1; }'
    assert 'return dest(a);' in tiny.candidate(source, 'f', 'x86_64', jump)[0]
    assert tiny.candidate(source.replace('dest(int x)', 'dest(long x)'), 'f', 'x86_64', jump)[0] is None
    assert tiny.candidate(source.replace('dest(int x)', 'dest()'), 'f', 'x86_64', jump)[0] is None


@pytest.fixture
def engine(monkeypatch):
    db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE functions(id,name,status,unit); CREATE TABLE claims(function_id,agent_id,claimed_at,expires_at);
    INSERT INTO functions VALUES(1,'f','open','u'); INSERT INTO functions VALUES(2,'g','matched','u');''')
    @contextmanager
    def tx():
        yield db
    @contextmanager
    def exclusive():
        yield
    net = SimpleNamespace(store=SimpleNamespace(tx=tx), _expire_claims=lambda db: None, claim_ttl=300,
                          fork_slug=lambda slug: slug)
    calls = []
    def probe(*args, **kw):
        calls.append(('probe', kw))
        return {'matched': True}
    def attempt(*args, **kw):
        calls.append(('apply', kw))
        return {'matched': True}
    net.forks = SimpleNamespace(_tree=lambda proj: SimpleNamespace(exclusive=exclusive), _attempt=probe, attempt=attempt)
    monkeypatch.setattr(app, 'inspect', lambda *args: ('void f(void) {}', 'empty'))
    yield net, db, calls
    db.close()


def test_only_exact_preview_is_applied_and_claim_released(engine):
    net, db, calls = engine
    assert app.solve_one(net, SimpleNamespace(id=7), {'slug': 'p'}, 1) == 'matched'
    assert calls == [('probe', {'probe': True}), ('apply', {'keep': False})]
    assert not db.execute('SELECT * FROM claims').fetchall()


def test_failed_preview_claimed_and_matched_functions_never_apply(engine):
    net, db, calls = engine
    net.forks._attempt = lambda *args, **kw: {'matched': False}
    assert app.solve_one(net, SimpleNamespace(id=7), {'slug': 'p'}, 1).startswith('failed')
    assert not calls
    assert app.solve_one(net, SimpleNamespace(id=7), {'slug': 'p'}, 2).startswith('skipped')
    db.execute('INSERT INTO claims VALUES(1,8,0,99999999999)')
    assert app.solve_one(net, SimpleNamespace(id=7), {'slug': 'p'}, 1).startswith('skipped')
    assert db.execute('SELECT agent_id FROM claims').fetchone()[0] == 8


def test_preview_error_releases_only_our_claim(engine):
    net, db, calls = engine
    def fail(*args, **kw):
        raise ValueError('broken baseline')
    net.forks._attempt = fail
    with pytest.raises(ValueError):
        app.solve_one(net, SimpleNamespace(id=7), {'slug': 'p'}, 1)
    assert not db.execute('SELECT * FROM claims').fetchall() and not calls


def test_rom_records_only_strict_matches(engine):
    net, db, calls = engine
    net.fork_slug = lambda slug: None
    net._context = lambda *args: {'header': '', 'version': 1}
    net._compile = lambda *args: SimpleNamespace(ok=True, obj=None)
    net._evaluate = lambda *args: (None, False, None)
    net._record = lambda *args, **kw: calls.append('record')
    assert app.solve_one(net, SimpleNamespace(id=7), {'slug': 'p', 'id': 1}, 1) == 'failed: judge'
    assert not calls


def test_tool_available_in_both_modes():
    for fork in (False, True):
        assert 'solve_tiny_functions' in tools.tool_names(fork)
        assert 'solve_tiny_functions' in {t['function']['name'] for t in api_tool_schemas(fork)}


def test_real_compiler_empty_template_matches(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    from decompnet import toolchains
    from decompnet.forks import objdiff

    clang = os.environ.get('DECOMP_TEST_CLANG')
    if not clang or not Path(clang).is_file() or toolchains.installed('objdiff-cli') is None:
        pytest.skip('needs explicit installed Clang and pinned objdiff')
    source = tmp_path/'tiny.c'
    target, candidate = tmp_path/'target.o', tmp_path/'candidate.o'
    source.write_text('void f(void) {}\nint neighbor(void) { return 17; }')
    subprocess.run([clang, '-O2', '-c', str(source), '-o', str(target)], check=True, timeout=30)
    rows = objdiff.diff(target, None, None, cwd=tmp_path, blocks=True).block_code['f']
    generated, reason = tiny.candidate('void f(void) { unknown(); }', 'f', 'x86_64', [Instruction(*row) for row in rows])
    assert generated and reason == 'empty'
    source.write_text(generated+'\nint neighbor(void) { return 17; }')
    subprocess.run([clang, '-O2', '-c', str(source), '-o', str(candidate)], check=True, timeout=30)
    assert objdiff.diff(target, candidate, 'f', cwd=tmp_path).matched
    assert objdiff.diff(target, candidate, 'neighbor', cwd=tmp_path).matched


def got_jump(slot='D_00D9E160'):
    """An x86-64 PLT entry's jump through its GOT slot, as objdiff reads it (the addend as text)."""
    return [Instruction(0, f'jmp qword ptr [{slot}-0x4]', relocation=f'[2, "-4", "{slot}"]')]


def test_a_jump_through_a_pointer_slot_is_a_tail_call_through_it():
    """Four of a PS4 game's PLT jumps matched as `D_00D9E160[0]();`: with no signature in the file the template
    claims no arguments (the jump forwards them all untouched), and declares the slot when the file doesn't."""
    source, kind = tiny.candidate('#include "common.h"\nvoid g(void) { }', 'func_009844D0', 'x86_64', got_jump())
    assert kind == 'pointer_tail'
    assert source == ('extern void (*D_00D9E160[])(void) __attribute__((visibility("hidden")));\n'
                      'void func_009844D0(void) { D_00D9E160[0](); }')
    declared = 'extern void (*D_00D9E160[])(void) __attribute__((visibility("hidden")));\n'
    assert tiny.candidate(declared, 'f', 'x86_64', got_jump())[0] == 'void f(void) { D_00D9E160[0](); }'


def test_a_pointer_tail_passes_a_known_signatures_arguments_on():
    source = tiny.candidate('int f(int a, char *b);', 'f', 'x86_64', got_jump())[0]
    assert source.endswith('int f(int a, char *b) { return ((int (*)(int, char *))D_00D9E160[0])(a, b); }')


@pytest.mark.parametrize('text', ['extern char D_00D9E160[];',  # the slot declared as something else
                                  'void caller(void) { f(1); }'])  # f named, but no simple declaration
def test_a_pointer_tail_leaves_what_the_file_says_otherwise_alone(text):
    assert tiny.candidate(text, 'f', 'x86_64', got_jump())[0] is None


def test_tail_jumps_read_objdiffs_text_addend_and_skip_registers_and_offsets():
    jump = [Instruction(0, 'jmp dest-0x4', relocation='[2, "-4", "dest"]')]
    assert 'return dest(a);' in tiny.candidate('int dest(int x);\nint f(int a) { return 1; }', 'f', 'x86_64', jump)[0]
    offset = [Instruction(0, 'jmp qword ptr [D_1+0x8]', relocation='[2, "4", "D_1"]')]
    assert tiny.candidate('void f(void);', 'f', 'x86_64', offset)[0] is None
    assert tiny.candidate('void f(void);', 'f', 'x86', got_jump())[0] is None  # 64-bit PLT shape only


def test_autopilot_runs_tiny_passes_while_they_still_match(monkeypatch):
    """One pass of 100 a run left a PS4 game's 420 jumps through GOT slots to five runs of autopilot; passes now go
    on, each from where the last stopped, until one matches nothing."""
    import json
    import threading

    from decompnet.app import code_blocks, finishing
    from decompnet.app.engine.autopilot import AutopilotMixin

    db = sqlite3.connect(':memory:', check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("CREATE TABLE projects(id,config); INSERT INTO projects VALUES(1,'{}');")

    @contextmanager
    def tx():
        yield db

    net = SimpleNamespace(local_agent=lambda name: (None, None), _project=lambda slug: {'id': 1, 'slug': slug},
                          links={'test': True}, store=SimpleNamespace(tx=tx))
    passes = []

    def run(net, agent, proj, limit=100, apply=False, state=None):
        passes.append(state.get('tiny_cursor', 0))
        matched = [100, 20, 0, 50][len(passes) - 1]
        state.update(tiny_total=100, tiny_scanned=100, tiny_matched=matched, tiny_cursor=len(passes) * 1000)

    monkeypatch.setattr(app, 'run', run)
    monkeypatch.setattr(finishing, 'run', lambda *a, **k: None)
    monkeypatch.setattr(code_blocks, 'warm', lambda *a, **k: None)
    state = dict(stop=threading.Event(), matched=0, tried=0, twins=0, near=0)
    AutopilotMixin._autopilot_run(net, 'test', [], state, 1, lambda *args: pytest.fail('no drafts'))
    assert passes == [0, 1000, 2000]  # each from the last one's cursor; the third matched nothing
    assert state['tiny_matched'] == 120 and state['matched'] == 120 and state['tiny_scanned'] == 300
    assert json.loads(db.execute('SELECT config FROM projects').fetchone()[0])['autopilot']['tiny_matched'] == 120

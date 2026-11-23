"""Independent synthetic fixtures for conservative templates and evidence-directed recipes."""


import pytest

from decompnet import tiny
from decompnet.arch import x86
from decompnet.codeblocks import Instruction


def code(*lines):
    return [Instruction(i, line) for i, line in enumerate(lines)]


@pytest.mark.parametrize('text,arch,lines,body', [
    ('int f(int x){}', 'x86_64', ('mov eax, edi', 'ret'), 'return x;'),
    ('unsigned int f(unsigned int x){}', 'x86_64', ('mov eax, ecx', 'ret'), 'return x;'),
    ('int * f(int * x){}', 'x86_64', ('mov rax, rdi', 'ret'), 'return x;'),
    ('int f(const int * x){}', 'x86_64', ('mov eax, [rdi]', 'ret'), 'return *x;'),
    ('int f(int x){}', 'x86', ('mov eax, [esp+0x4]', 'ret'), 'return x;'),
    ('int f(int * x){}', 'x86', ('mov eax, [esp+0x4]', 'mov eax, [eax]', 'ret'), 'return *x;'),
])
def test_explicit_compatible_argument_templates(text, arch, lines, body):
    assert body in tiny.candidate(text, 'f', arch, code(*lines))[0]


@pytest.mark.parametrize('text,lines', [
    ('int f(int x, int y){}', ('mov eax, edi', 'ret')),
    ('float f(float x){}', ('mov eax, edi', 'ret')),
    ('long f(long x){}', ('mov eax, edi', 'ret')),
    ('int f(short * x){}', ('mov eax, [rdi]', 'ret')),
    ('int f(int * x){}', ('mov eax, [rdi+4]', 'ret')),
    ('int f(int x){}', ('mov eax, esi', 'ret')),
    ('int * f(long * x){}', ('mov rax, rdi', 'ret')),
])
def test_unproven_argument_abi_width_or_offset_is_skipped(text, lines):
    assert tiny.candidate(text, 'f', 'x86_64', code(*lines))[0] is None


COPY = 'void f(unsigned long * dst, const unsigned long * src) { __builtin_memcpy(dst, src, 16); }'
COPY_ROWS = [('i', 'mov rax, [rsi]', 'movups xmm0, [rsi]'),
             ('i', 'mov [rdi], rax', 'movups [rdi], xmm0'),
             ('i', 'mov rcx, [rsi+8]', ''), ('i', 'mov [rdi+8], rcx', '')]


def test_copy_recipe_requires_observed_vector_copy_and_known_integer_abi():
    result = x86.recipe_suggestions(COPY, COPY_ROWS, ['__attribute__((target("no-sse"))) void donor(void) {}'])
    assert [x['name'] for x in result] == ['no-sse']
    assert result[0]['donor_observations'] == 1
    for source in [COPY.replace('unsigned long', 'double'), COPY.replace('void f', 'double f'),
                   COPY.replace('unsigned long', 'Unknown'), COPY.replace('__builtin_memcpy(dst, src, 16)', 'other(dst, src)')]:
        assert x86.recipe_suggestions(source, COPY_ROWS) == []
    assert x86.recipe_suggestions(COPY, COPY_ROWS[:1]) == []
    assert x86.recipe_suggestions(COPY, [('i', 'je 0x10', 'je 0x20')]) == []


def test_donors_cannot_trigger_recipes_without_mismatch_evidence():
    donors = ['__attribute__((target("arch=btver2"))) void f(void) {}']
    assert x86.recipe_suggestions(COPY, [], donors) == []
    rows = [('i', 'mov eax, 1', 'mov ecx, 2'), ('i', 'mov ecx, 2', 'mov eax, 1')]
    assert x86.recipe_suggestions(COPY, rows, donors)[0]['donor_observations'] == 1


def test_integer_struct_copy_requires_readable_integer_fields():
    source = 'struct S { unsigned long a; unsigned long b; };\nvoid f(struct S * dst, const struct S * src) { *dst = *src; }'
    assert x86.recipe_suggestions(source, COPY_ROWS)[0]['name'] == 'no-sse'
    assert x86.recipe_suggestions(source.replace('unsigned long b', 'double b'), COPY_ROWS) == []
    assert x86.recipe_suggestions(source[source.index('void f'):], COPY_ROWS) == []


def test_project_processor_reads_target_and_abstains_on_unsupported(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from decompnet.app import tiny_functions

    target = tmp_path/'target.o'
    target.write_bytes(b'object')
    unit = SimpleNamespace(target=target)
    monkeypatch.setattr(tiny_functions.elfobj, 'extract_function', lambda data, name: SimpleNamespace(arch='x86_64'))
    assert tiny_functions.processor(None, unit, 'f', '') == 'x86_64'
    assert tiny_functions.processor(None, unit, 'f', 'mips') == 'x86_64'
    monkeypatch.setattr(tiny_functions.elfobj, 'extract_function', lambda data, name: None)
    assert tiny_functions.processor(None, unit, 'missing', '') == ''
    monkeypatch.undo()
    assert tiny_functions.processor(None, unit, 'f', 'x86') == ''
    assert tiny_functions.processor(None, SimpleNamespace(target=None), 'f', 'x86') == ''
    assert tiny_functions.processor(None, SimpleNamespace(), 'f', 'x86') == 'x86'


def test_tiny_project_inspection_uses_actual_target_processor(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from decompnet.app import tiny_functions

    source = tmp_path/'source.c'
    source.write_text('int f(int x) {}')
    unit = SimpleNamespace(source='source.c')
    fp = SimpleNamespace(unit=lambda _: unit, safe_path=lambda _: source)
    engine = SimpleNamespace(get=lambda _: ({}, fp, {}), _meta=lambda _: {})
    net = SimpleNamespace(fork_slug=lambda _: 'p', forks=engine)
    monkeypatch.setattr(tiny_functions, 'finder', lambda _: SimpleNamespace(unit_code=lambda *a: {'f': code('mov eax, edi', 'ret')}))
    monkeypatch.setattr(tiny_functions, 'processor', lambda *a: 'x86_64')
    generated, kind = tiny_functions.inspect(net, {'slug': 'p', 'arch': ''}, {'unit': 'u', 'name': 'f'})
    assert kind == 'identity32' and 'return x;' in generated
    monkeypatch.setattr(tiny_functions, 'processor', lambda *a: '')
    assert tiny_functions.inspect(net, {'slug': 'p', 'arch': ''}, {'unit': 'u', 'name': 'f'})[0] is None

"""The public compiler's callers must match linked targets without relaxing reference identity."""
import io
import json
import subprocess

import pytest
from elftools.elf.elffile import ELFFile

from decompnet import toolchains
from decompnet.consoles.ps4 import scratch
from decompnet.limits import capped

from .test_ps4_targets import binder, delinker


@pytest.mark.parametrize('same_file', [True, False])
def test_compiled_callers_match_linked_code_and_reject_wrong_calls(tmp_path, same_file):
    if toolchains.current_platform() != toolchains.LINUX:
        pytest.skip('needs Linux x86-64 public Clang')
    if scratch.clang_path() is None or toolchains.installed('objdiff-cli') is None:
        pytest.skip('needs pinned Clang 5.0.2 and objdiff')
    clang = scratch.clang_path()
    if not clang.with_name('ld.lld').exists():
        pytest.skip('needs the compiler pack’s lld linker')
    flags = ['-target', 'x86_64-scei-ps4', '-O2', '-ffreestanding', '-ffunction-sections',
             '-fno-asynchronous-unwind-tables']
    callee = '__attribute__((noinline)) int callee(int v) { return v * 3 + 1; }\n'
    source = tmp_path / 'caller.c'
    body = 'int caller(int v) { return callee(v) + 7; }\n'
    prefix = callee if same_file else 'extern int callee(int);\n'
    source.write_text(prefix + body)
    obj, other, linked = (tmp_path / name for name in ['caller.o', 'callee.o', 'linked.elf'])
    def run(args):
        return subprocess.run(capped([str(a) for a in args]), check=True, capture_output=True, timeout=60)
    def compile_source():
        run([clang, *flags, '-c', source, '-o', obj])
    compile_source()
    inputs = [obj]
    if not same_file:
        separate = tmp_path / 'callee.c'
        separate.write_text(callee)
        run([clang, *flags, '-c', separate, '-o', other])
        inputs.append(other)
    run([clang.with_name('ld.lld'), '-e', 'caller', '-Ttext=0x1000', *inputs, '-o', linked])
    elf = ELFFile(io.BytesIO(linked.read_bytes()))
    symbols = {s.name: s for s in elf.get_section_by_name('.symtab').iter_symbols() if s.name in ('caller', 'callee')}
    image = scratch.read_elf(linked.read_bytes())
    functions = scratch.analyse(image, [{'entry': s['st_value'], 'end': s['st_value'] + s['st_size'], 'name': name}
                                        for name, s in sorted(symbols.items(), key=lambda row: row[1]['st_value'])])
    # Use the symbol identities supplied by the independent linker symbol table.
    names = {symbols[name]['st_value']: name for name in symbols}
    rows = []
    for fn in functions:
        relocs = [(off, kind, names.get(int(name.rsplit('_', 1)[1], 16), name), addend)
                  for off, kind, name, addend in fn.relocs]
        rows.append({'name': names[fn.addr], 'addr': fn.addr, 'size': fn.size, 'relocs': relocs})
    start, end = rows[0]['addr'], max(r['addr'] + r['size'] for r in rows)
    target = tmp_path / 'target.o'
    target.write_bytes(delinker().elf64('unit', rows, image.read(start, end-start), start))
    for change, matched in [(body.replace('callee(v)', 'wrong(v)'), False),
                            (body.replace('+ 7', '+ 8'), False), (body, True)]:
        source.write_text(prefix + 'extern int wrong(int);\n' + change)
        compile_source()
        binder().normalize_plt(obj)
        report = run([toolchains.binary('objdiff-cli'), 'diff', '-1', target, '-2', obj,
                      '-o', '-', '-c', 'functionRelocDiffs=all', '-c', 'combineTextSections=true', 'caller'])
        percent = next(s['match_percent'] for s in json.loads(report.stdout)['left']['symbols'] if s['name'] == 'caller')
        assert (percent == 100) == matched

"""Reproducible independent synthetic compiler benchmark; no live project/database is read.

Run with PYTHONPATH=src python tests/benchmark_solver_extensions.py --clang /explicit/clang.
Before means the pre-extension empty/trap/constant/tail families, with the same compiler and strict judge.
"""
import argparse
import io
import json
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from elftools.elf.elffile import ELFFile

from decompnet import limits, tiny, toolchains
from decompnet.app.tiny_functions import processor
from decompnet.arch import x86
from decompnet.codeblocks import Instruction
from decompnet.forks import objdiff

STRICT = {'functionRelocDiffs': 'all', 'combineTextSections': True, 'combineDataSections': True}

FIXTURES = (
    'void f(void) {}', 'int f(void) { return 7; }', 'unsigned int f(void) { return 0; }',
    'void f(void) { __builtin_trap(); }',
    'int f(int value) { return value; }', 'unsigned int f(unsigned int value) { return value; }',
    'int * f(int * value) { return value; }', 'const int * f(const int * value) { return value; }',
    'int f(const int * value) { return *value; }',
    'unsigned int f(const unsigned int * value) { return *value; }',
    'float f(float value) { return value; }',
    'int f(int first, int second) { return first; }',
    'char f(char value) { return value; }',
    'int f(int * value) { return value[1]; }',
)
NEIGHBOR = '\nint neighbor(void) { return 314159; }\n'


def symbol_bytes(path, name):
    elf = ELFFile(io.BytesIO(path.read_bytes()))
    symbols = elf.get_section_by_name('.symtab').get_symbol_by_name(name)
    symbol = symbols[0]
    return elf.get_section(symbol['st_shndx']).data()[symbol['st_value']:symbol['st_value']+symbol['st_size']]


def benchmark(clang):
    if toolchains.installed('objdiff-cli') is None:
        raise RuntimeError('Install pinned objdiff before running this benchmark.')
    counts = {'fixtures': len(FIXTURES), 'before_exact': 0, 'after_exact': 0, 'after_skipped': 0,
              'errors': 0, 'regressions': 0, 'neighbor_checks': 0, 'independent_byte_checks': 0, 'builds': 0}
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='decomp-solvers-') as folder:
        root = Path(folder)
        for index, fixture in enumerate(FIXTURES):
            source, target, candidate = root/'input.c', root/'target.o', root/'candidate.o'
            def build(text, output, source=source):
                source.write_text(text+NEIGHBOR)
                subprocess.run(limits.capped([str(clang), '-O2', '-c', str(source), '-o', str(output)]),
                               check=True, capture_output=True, timeout=30)
                counts['builds'] += 1
            build(fixture, target)
            assert processor(None, SimpleNamespace(target=target), 'f', '') == 'x86_64'
            assert processor(None, SimpleNamespace(target=target), 'f', 'mips') == 'x86_64'
            rows = objdiff.diff(target, None, None, cwd=root, options=STRICT, blocks=True).block_code['f']
            seed = fixture[:fixture.index('{')]+'{ /* unavailable body */ }'
            generated, reason = tiny.candidate(seed, 'f', 'x86_64', [Instruction(*row) for row in rows])
            if generated is None:
                counts['after_skipped'] += 1
                continue
            build(generated, candidate)
            exact = objdiff.diff(target, candidate, 'f', cwd=root, options=STRICT).matched
            neighbor = objdiff.diff(target, candidate, 'neighbor', cwd=root, options=STRICT).matched
            counts['neighbor_checks'] += 1
            equal_bytes = symbol_bytes(target, 'f') == symbol_bytes(candidate, 'f')
            counts['independent_byte_checks'] += 1
            if not neighbor or not equal_bytes or not exact:
                counts['errors'] += 1
            counts['after_exact'] += bool(exact and equal_bytes and neighbor)
            if reason in {'empty', 'trap', 'constant', 'tail'}:
                counts['before_exact'] += bool(exact and equal_bytes and neighbor)
                counts['regressions'] += not (exact and equal_bytes and neighbor)
            if index >= 10:
                raise AssertionError('Negative fixture unexpectedly generated a template.')
        recipes = {'fixtures': 3, 'before_exact': 0, 'after_exact': 0, 'errors': 0,
                   'builds': 0, 'neighbor_checks': 0, 'independent_byte_checks': 0}
        for size in (16, 32, 64):
            fixture = ('void f(unsigned long * dst, const unsigned long * src) '
                       '{ __builtin_memcpy(dst, src, '+str(size)+'); }')
            # Controlled compiler-generated targets isolate copy lowering; these are not game-derived fixtures.
            source, target, candidate = root/'copy.c', root/'copy-target.o', root/'copy-candidate.o'
            def build_copy(text, output, source=source):
                source.write_text(text+NEIGHBOR)
                subprocess.run(limits.capped([str(clang), '-O2', '-c', str(source), '-o', str(output)]),
                               check=True, capture_output=True, timeout=30)
                recipes['builds'] += 1
            build_copy('__attribute__((target("no-sse"))) '+fixture, target)
            build_copy(fixture, candidate)
            difference = objdiff.diff(target, candidate, 'f', cwd=root, options=STRICT)
            recipes['before_exact'] += difference.matched
            suggestions = x86.recipe_suggestions(fixture, difference.rows)
            if len(suggestions) != 1 or suggestions[0]['name'] != 'no-sse':
                recipes['errors'] += 1
                continue
            build_copy(x86.compiler_recipe(suggestions[0]['name'], fixture), candidate)
            exact = objdiff.diff(target, candidate, 'f', cwd=root, options=STRICT).matched
            neighbor = objdiff.diff(target, candidate, 'neighbor', cwd=root, options=STRICT).matched
            recipes['neighbor_checks'] += 1
            equal_bytes = symbol_bytes(target, 'f') == symbol_bytes(candidate, 'f')
            recipes['independent_byte_checks'] += 1
            recipes['after_exact'] += bool(exact and equal_bytes and neighbor)
            recipes['errors'] += not (exact and equal_bytes and neighbor)
        counts['copy_recipe'] = recipes
    counts['seconds'] = round(time.monotonic()-start, 3)
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clang', type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(benchmark(options.clang), indent=2))

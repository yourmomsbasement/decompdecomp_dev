"""Compiler explanations run on copied inputs, never as match verdicts or edits to the project's build.

The console supplies the configured compiler invocation. Unsupported project recipes abstain. Diagnostic flags
apply only to an isolated temporary compilation; target objects, ordinary outputs and project flags stay intact.
"""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import consoles, limits
from ..hostenv import NO_WINDOW
from ..network import NetworkError
from . import compile_tools

MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
SECONDS = 45


def run(net, agent, slug, function, revision):
    engine = net.forks
    proj, fp, cfg = engine.get(slug)
    with tempfile.TemporaryDirectory(prefix='decomp-diagnostics-') as directory:
        scratch = Path(directory)
        with engine._tree(proj).exclusive():
            fn, _, current = compile_tools.snapshot(engine, slug, function)
            if current != revision:
                raise NetworkError('Stale revision: refresh function_brief before compiler diagnostics.')
            unit = fp.unit(fn['unit'])
            recipe = consoles.get(cfg.get('platform')).diagnostic_recipe(fp, unit)
            if recipe is None:
                return 'Compiler diagnostics unavailable for this project/compiler recipe. No build or judge settings changed.'
            compiler, flags = recipe
            total = 0
            # Preserve relative include paths without reading original binaries, targets or build products.
            paths = [fp.safe_path(unit.source)]
            for parent in ('include', 'src'):
                base = fp.root / parent
                if base.exists():
                    paths.extend(p for p in base.rglob('*') if p.suffix.lower() in {'.h', '.hpp', '.hh', '.inc'})
            for path in dict.fromkeys(paths):
                relative = path.relative_to(fp.root)
                if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != fp.root and fp.root in p.parents):
                    raise NetworkError('Diagnostics require ordinary project source/header files, not symbolic links.')
                total += path.stat().st_size
                if total > MAX_INPUT_BYTES:
                    raise NetworkError('Diagnostic inputs exceed16 MiB; no diagnostic compile started.')
                destination = scratch / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            symbol = engine._meta(fn).get('symbol') or fn['name']
        ir = scratch / 'diagnostic.ll'
        argv = [str(compiler), *flags, '-Iinclude', '-I.', '-S', '-emit-llvm', '-gline-tables-only',
                '-Rpass=loop-vectorize|loop-unroll|slp-vectorizer',
                '-Rpass-missed=loop-vectorize|loop-unroll|slp-vectorizer',
                '-Rpass-analysis=loop-vectorize|loop-unroll|slp-vectorizer', unit.source, '-o', str(ir)]
        with open(scratch/'compiler.log', 'w+b') as log:
            try:
                process = subprocess.run(limits.capped(argv), cwd=scratch, stdout=log, stderr=log,
                                         timeout=SECONDS, creationflags=NO_WINDOW)
            except subprocess.TimeoutExpired:
                raise NetworkError('Diagnostic compiler exceeded45 seconds; ordinary source/build outputs were untouched.') from None
            log.seek(0)
            remarks = log.read(24000).decode('utf-8', 'replace')
        out = ['Isolated compiler diagnostics; advisory only, not a match verdict.',
               f'Input revision: {revision}; copied source/header bytes: {total}; compiler: {compiler}.',
               'Diagnostic flags do not change project compiler flags or accepted objects.']
        if process.returncode:
            return '\n'.join(out + [f'Diagnostic compile failed (exit{process.returncode}):', remarks])
        with ir.open('rb') as stream:
            raw = stream.read(MAX_OUTPUT_BYTES+1)
        text = raw[:MAX_OUTPUT_BYTES].decode('utf-8', 'replace')
        match = re.search(r'(?ms)^define[^\n]*@(?:"' + re.escape(symbol) + r'"|' + re.escape(symbol) + r')\(.*?^}', text)
        if match:
            body = match.group(0)
            out += [f'Optimized IR for {symbol}; SHA256 {hashlib.sha256(body.encode()).hexdigest()}.',
                    body[:18000] + ('\n[function IR truncated]' if len(body)>18000 else '')]
        else:
            out.append('Selected function IR unavailable in the bounded output; no optimization conclusion inferred.')
        if len(raw)>MAX_OUTPUT_BYTES:
            out.append('IR file read capped at2 MiB.')
        out += ['Compiler remarks (may include neighboring definitions):', remarks or 'No remarks emitted. '
                'Absence of a remark is not evidence a pass did not run.']
        with engine._tree(proj).exclusive():
            if compile_tools.snapshot(engine, slug, function)[2] != revision:
                out.append('Source/build revision changed during diagnostics; findings describe the earlier copied inputs.')
        return '\n'.join(out)


def clang_recipe(fp, unit):
    """Recognize a configured compile.py wrapper without executing any project script or changing its output."""
    import os
    import shlex

    from ..forks.project import _tool

    ninja = _tool('ninja')
    if not ninja or unit.base is None or not unit.source:
        return None
    wrapper = fp.root/'tools/compile.py'
    if not wrapper.is_file():
        return None
    wrapper_text = wrapper.read_text(encoding='utf-8')
    # the template's wrapper before units had compilers of their own, and after (consoles/ps4/units.py)
    known_commands = ('command = [args.compiler, *flags, f"-I{args.include}", "-I.", "-c", args.source, "-o", args.output]',
                      'command = [clang, *flags, f"-I{args.include}", "-I.", "-c", args.source, "-o", args.output]')
    if not any(known in wrapper_text for known in known_commands):
        return None
    result = subprocess.run(limits.capped([ninja, '-t', 'commands', os.path.relpath(unit.base, fp.root)]),
                            cwd=fp.root, capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
    if result.returncode:
        return None
    for line in reversed(result.stdout.splitlines()):
        args = shlex.split(line)
        if len(args)<4 or args[1] != 'tools/compile.py' or '--compiler' not in args or '--' not in args:
            continue
        compiler = Path(args[args.index('--compiler')+1])
        if not compiler.is_absolute() or not compiler.is_file() or 'clang' not in compiler.name:
            return None
        flags = args[args.index('--')+1:]
        # Project templates append per-unit settings inside compile.py, and may give a unit its own compiler: explain
        # what that unit is really built with
        extras = fp.root/'tools/compiler_units.json'
        if extras.is_file() and 'compiler_units.json' in wrapper_text:
            entry = json.loads(extras.read_text()).get(Path(unit.source).stem, [])
            own, extra = (entry.get('compiler'), entry.get('flags', [])) if isinstance(entry, dict) else (None, entry)
            if not isinstance(extra, list):
                return None
            spec = {}
            if own:
                known = fp.root/'tools/compilers.json'
                spec = json.loads(known.read_text()).get(own) if known.is_file() else None
                if spec is None:
                    return None
            if spec.get('path'):
                compiler = Path(spec['path'])
                flags = [*spec.get('flags', []), *flags]
                if spec.get('default_march') and not any(f.startswith('-march=') for f in [*flags, *extra]):
                    extra = [*extra, f"-march={spec['default_march']}"]
            if '-fPIC' in extra:
                flags = [f for f in flags if f != '-fPIE']
            flags += extra
        if not all(isinstance(flag, str) for flag in flags):
            return None
        if any(flag.startswith(('@', '-fplugin', '-fpass-plugin', '-save-temps', '-fprofile', '-ftime-trace', '-fmodules', '-MF', '-MJ'))
               or flag in {'-load', '-o', '-c', '-Xclang', '-M', '-MD', '-MMD', '-MT', '-MQ'} for flag in flags):
            return None
        return compiler, flags
    return None

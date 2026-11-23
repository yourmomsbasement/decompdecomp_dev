"""Explanations use copied inputs and diagnostic flags, never the accepted build or a correctness verdict."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from decompnet.app import compiler_diagnostics as diagnostics
from decompnet.app.forkengine import _TreeLock
from decompnet.network import NetworkError


@pytest.fixture
def rig(tmp_path,monkeypatch):
    (tmp_path/'include').mkdir()
    (tmp_path/'src').mkdir()
    (tmp_path/'include/common.h').write_text('#define VALUE 7\n')
    (tmp_path/'src/unit.c').write_text('#include "common.h"\nint work(int x){return x+VALUE;}\n')
    (tmp_path/'ordinary.o').write_bytes(b'untouched-build-output')
    fn={'name':'work','unit':'unit'}
    unit=SimpleNamespace(source='src/unit.c',name='unit',base=tmp_path/'ordinary.o')
    fp=SimpleNamespace(root=tmp_path,unit=lambda name:unit,safe_path=lambda rel:tmp_path/rel)
    tree=_TreeLock()
    engine=SimpleNamespace(get=lambda slug:({'id':1},fp,{'platform':'synthetic'}),_tree=lambda proj:tree,_meta=lambda fn:{})
    net=SimpleNamespace(forks=engine)
    console=SimpleNamespace(diagnostic_recipe=lambda fp,unit:None)
    monkeypatch.setattr(diagnostics.consoles,'get',lambda platform:console)
    monkeypatch.setattr(diagnostics.compile_tools,'snapshot',lambda *args:(fn,'int work(int x){return x+VALUE;}','rev'))
    return SimpleNamespace(net=net,console=console,fp=fp,unit=unit,path=tmp_path)


def test_unsupported_compiler_abstains_without_touching_outputs(rig):
    text=diagnostics.run(rig.net,None,'demo','work','rev')
    assert 'unavailable' in text
    assert (rig.path/'ordinary.o').read_bytes()==b'untouched-build-output'


def test_stale_diagnostics_do_not_start_a_compiler(rig):
    with pytest.raises(NetworkError,match='Stale revision'):
        diagnostics.run(rig.net,None,'demo','work','old')


def test_symlinked_headers_are_rejected_before_diagnostic_compilation(rig):
    (rig.path/'include/link.h').symlink_to(rig.path/'include/common.h')
    rig.console.diagnostic_recipe=lambda *a:(Path('/not-executed/clang'),[])
    with pytest.raises(NetworkError,match='symbolic links'):
        diagnostics.run(rig.net,None,'demo','work','rev')


def test_real_clang_ir_uses_copied_headers_and_preserves_project(rig):
    compiler=os.environ.get('DECOMP_TEST_CLANG')
    if not compiler or not Path(compiler).is_file():
        pytest.skip('needs explicitly selected installed Clang')
    before={str(p.relative_to(rig.path)):p.read_bytes() for p in rig.path.rglob('*') if p.is_file()}
    rig.console.diagnostic_recipe=lambda *a:(Path(compiler),['-O2'])
    text=diagnostics.run(rig.net,None,'demo','work','rev')
    assert 'Optimized IR for work' in text and 'add nsw i32' in text and '7' in text
    assert 'not a match verdict' in text
    after={str(p.relative_to(rig.path)):p.read_bytes() for p in rig.path.rglob('*') if p.is_file()}
    assert before==after


def test_wrapper_recipe_includes_per_unit_flags_and_never_executes_build_scripts(rig,monkeypatch):
    from decompnet.forks import project

    compiler=rig.path/'clang';compiler.write_text('not run')
    (rig.path/'tools').mkdir()
    (rig.path/'tools/compile.py').write_text('command = [args.compiler, *flags, f\"-I{args.include}\", \"-I.\", \"-c\", args.source, \"-o\", args.output]\n# compiler_units.json')
    (rig.path/'tools/compiler_units.json').write_text('{"unit":["-mbmi"]}')
    calls=[]
    monkeypatch.setattr(project,'_tool',lambda name:'/pinned/ninja')
    monkeypatch.setattr(diagnostics.limits,'capped',lambda args:args)
    def invoke(args,**kw):
        calls.append(args)
        return SimpleNamespace(returncode=0,stdout=f'/configured/python tools/compile.py --compiler {compiler} --source src/unit.c --output ordinary.o -- -O2 -mavx\n')
    monkeypatch.setattr(diagnostics.subprocess,'run',invoke)
    path,flags=diagnostics.clang_recipe(rig.fp,rig.unit)
    assert path==compiler and flags==['-O2','-mavx','-mbmi']
    assert calls==[['/pinned/ninja','-t','commands','ordinary.o']]

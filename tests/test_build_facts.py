"""Editing a source rebuilds its object; changing the file inventory regenerates Ninja."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from decompnet import consoles
from decompnet.forks.project import ForkProject


@pytest.mark.parametrize('console', ['pc', 'ps4'])
def test_only_inventory_changes_regenerate_the_build(tmp_path, console):
    # Console modules are packages; find the template without importing configure's argument parser.
    import importlib
    tools = Path(importlib.import_module(consoles.get(console).__class__.__module__).__file__).parent / 'project_template/tools'
    shutil.copytree(tools, tmp_path / 'tools')
    cfg = tmp_path / 'config/TEST/functions.json'
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"exe": "orig/game.exe", "units": {"sample": []}}')
    (tmp_path / 'src/auto').mkdir(parents=True)
    source = tmp_path / 'src/auto/sample.c'
    source.write_text('int sample(void) { return 1; }\n')
    (tmp_path / 'include').mkdir()
    (tmp_path / 'tools/compile.py').write_text(
        'import sys\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index("--output")+1])\n'
        'p.parent.mkdir(parents=True,exist_ok=True)\np.write_text("compiled")\n')
    (tmp_path / 'compiler/6.0/Bin').mkdir(parents=True)
    (tmp_path / 'compiler/6.0/Bin/CL.EXE').touch()
    args = ['--msvc' if console == 'pc' else '--clang', str(tmp_path / 'compiler')]
    subprocess.run([sys.executable, str(tmp_path / 'tools/configure.py'), 'TEST', *args], check=True, capture_output=True)
    project = ForkProject(tmp_path)
    ok, output = project.run(['ninja', 'all_source'], log=lambda _: None, timeout=30)
    assert ok, output
    manifest = tmp_path / 'build.ninja'
    written = manifest.stat().st_mtime_ns
    source.write_text('int sample(void) { return 2; }\n')
    ok, output = project.run(['ninja', 'all_source'], log=lambda _: None, timeout=30)
    assert ok and manifest.stat().st_mtime_ns == written, output
    extra = source.with_name('extra.c')
    extra.write_text('int extra(void) { return 0; }\n')
    ok, output = project.run(['ninja', 'all_source'], log=lambda _: None, timeout=30)
    assert ok and manifest.stat().st_mtime_ns != written, output
    written = manifest.stat().st_mtime_ns
    extra.unlink()
    ok, output = project.run(['ninja', 'all_source'], log=lambda _: None, timeout=30)
    assert ok and manifest.stat().st_mtime_ns != written, output

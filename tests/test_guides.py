"""Guides and the up-front prompt carry only the project's platform's material. A PC helper's first prompt was
14 KB and taught GameCube's split_file, REL files, func_8001234 names, s32 types and GCC register variables."""

from decompnet.app import tools

TEXT = """common
<!-- only: ppc -->
gamecube only
<!-- end -->
<!-- only: !x86 -->
not on x86
<!-- end -->
<!-- only: x86 n64 -->
x86 or the n64 console
<!-- end -->
tail
"""


def test_only_blocks_follow_the_platform():
    assert tools.for_platform(TEXT, "gc") == "common\ngamecube only\nnot on x86\ntail\n"
    assert tools.for_platform(TEXT, "pc") == "common\nx86 or the n64 console\ntail\n"
    assert tools.for_platform(TEXT, "n64") == "common\nnot on x86\nx86 or the n64 console\ntail\n"
    # no platform (guide() outside a project): everything, without the markers
    assert tools.for_platform(TEXT, None) == "common\ngamecube only\nnot on x86\nx86 or the n64 console\ntail\n"


def test_a_pc_projects_up_front_guides_have_no_gamecube_material_and_gamecubes_no_x86():
    pc = tools.up_front("pc", "fork", "")
    for word in (".rodata:0x88", "func_8001234", "s32", "REL/", 'asm("r4")', "<!--"):
        assert word not in pc, word
    assert "D_00402034" in pc and "split_file(path, at, new_name)" in pc  # its own placeholder names, its own split
    gc = tools.up_front("gc", "fork", "")
    assert "split_file" in gc and "func_8001234" in gc
    for word in ("ret N", "__thiscall", "D_00402034", "<!--"):
        assert word not in gc, word


def test_the_guide_tool_reads_the_projects_platform():
    assert "split_file(path, at, new_name, data)" not in tools.guide_text("fork", "pc")  # (GameCube's, with data)
    assert "split_file(path, at, new_name, data)" in tools.guide_text("fork")  # no project: all of it
    assert "lwc1" not in tools.guide_text("cpp", "pc") and "lwc1" in tools.guide_text("cpp", "n64")



def test_quick_reference_replaces_bulk_guides_without_hiding_full_guide(monkeypatch):
    from types import SimpleNamespace

    full = '# Platform guide\n\n## Agent quick reference\nKeep strict destinations.\n\n## Historical experiments\nLong recipe history.'
    monkeypatch.setattr(tools.consoles, 'get', lambda platform: SimpleNamespace(up_front_guide=lambda tc: 'test'))
    monkeypatch.setattr(tools, 'guide_text', lambda topic, platform: full)
    quick = tools.up_front('test', 'fork', '')
    assert 'Keep strict destinations.' in quick and 'Long recipe history' not in quick
    assert 'guide("test")' in quick and 'matched verdict' in quick
    assert tools.guide_text('test', 'test') == full


def test_work_prompts_prefer_evidence_and_small_edits_without_maintenance_sweeps():
    from decompnet import network

    fork = tools.FORK_PROMPT.format(title='Test', what='a source-tree project', count=3,
                                   budget=network.stall_budget_text(), guides='guide("fork")')
    rom = tools.WORK_PROMPT.format(title='Test', count=3, budget=network.stall_budget_text(), compiler_guide='mips')
    assert '3 claims' in fork and 'compile_patch' in fork and 'stale revision' in fork
    for prompt in (fork, rom):
        assert network.stall_budget_text() in prompt
        assert 'one failing region' in prompt and 'verified dependency' in prompt
        assert 'pattern_index_report is' in prompt and 'autopilot' in prompt
        assert 'Never fetch or copy code from outside this project' in prompt
    assert 'ROM projects' in tools.INSTRUCTIONS and 'source-tree projects' in tools.INSTRUCTIONS
    assert 'including relocation and constant checks' in tools.INSTRUCTIONS
    assert 'a claim that gets its full compile budget' not in fork+rom

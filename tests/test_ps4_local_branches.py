"""Resolve only linker-local PLT branches corroborated by the original function."""
import importlib.util
import io
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from decompnet.consoles.ps4 import units

TOOLS = Path(units.__file__).with_name("project_template") / "tools"


def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def object_bytes(opcode, kind, destination="g", defined=True):
    functions = [{"name": "f", "addr": 0, "size": 5, "relocs": [(1, kind, destination, -4)]}]
    if defined:
        functions.append({"name": "g", "addr": 5, "size": 1, "relocs": []})
    return module("delink").elf64("unit", functions, bytes([opcode, 0, 0, 0, 0, 0xc3]), 0)


@pytest.mark.parametrize("opcode", [0xe8, 0xe9])
def test_local_plt_branch_becomes_pc32_without_instruction_changes(tmp_path, opcode):
    bind = module("bind")
    obj, target = tmp_path / "base.o", tmp_path / "target.o"
    obj.write_bytes(object_bytes(opcode, 4))
    target.write_bytes(object_bytes(opcode, 2))
    text = ELFFile(io.BytesIO(obj.read_bytes())).get_section_by_name(".text").data()
    assert bind.bind_local_branches(obj, target) == 1
    elf = ELFFile(io.BytesIO(obj.read_bytes()))
    assert elf.get_section_by_name(".text").data() == text
    assert next(elf.get_section_by_name(".rela.text").iter_relocations())["r_info_type"] == 2
    assert bind.bind_local_branches(obj, target) == 0


@pytest.mark.parametrize("defined,destination,target_opcode", [(False, "g", 0xe9),
                                                              (True, "other", 0xe9),
                                                              (True, "g", 0xe8)])
def test_external_wrong_symbol_or_wrong_branch_kind_is_untouched(tmp_path, defined, destination, target_opcode):
    obj, target = tmp_path / "base.o", tmp_path / "target.o"
    original = object_bytes(0xe9, 4, destination, defined)
    obj.write_bytes(original)
    target.write_bytes(object_bytes(target_opcode, 2))
    assert module("bind").bind_local_branches(obj, target) == 0
    assert obj.read_bytes() == original

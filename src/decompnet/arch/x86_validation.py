"""Independent decoding of original x86 references; never repairs target objects."""
import capstone

from .x86 import rip_data_references


def references(code: bytes, address: int, regions: list[tuple[int, int]]) -> tuple[dict, set]:
    """Return operand evidence and instruction boundaries, rejecting incomplete decoding."""
    decoder = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    decoder.detail = True
    instructions = list(decoder.disasm(code, address))
    if sum(i.size for i in instructions) != len(code):
        raise ValueError('Original function does not decode completely.')
    boundaries = {i.address for i in instructions} | {address + len(code)}
    refs = {off: (dest, addend, 4, 'data')
            for off, dest, addend in rip_data_references(code, address, regions)}
    for ins in instructions:
        if (ins.group(capstone.CS_GRP_JUMP) or ins.group(capstone.CS_GRP_CALL)) and ins.imm_size:
            operands = [o for o in ins.operands if o.type == capstone.x86.X86_OP_IMM]
            if operands:
                off = ins.address - address + ins.imm_offset
                refs[off] = (operands[0].imm, -(ins.size - ins.imm_offset), ins.imm_size, 'branch')
    return refs, boundaries

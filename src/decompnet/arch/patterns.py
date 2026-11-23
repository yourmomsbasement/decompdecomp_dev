"""Observable instruction families for advisory retrieval, not inferred source semantics."""

from .blocks import control


def families(processor, ops):
    """Name visible operation mixes without guessing constructors, types, or helper boundaries."""
    if not ops:
        return ()
    calls = {'x86': {'call', 'callq'}, 'x86_64': {'call', 'callq'}, 'mips': {'jal', 'jalr'},
             'ppc': {'bl', 'bctrl', 'bcl'}, 'arm': {'bl', 'blx'}, 'thumb': {'bl', 'blx'}}.get(processor, set())
    labels = []
    n_calls = sum(op in calls for op in ops)
    if n_calls:
        labels.append('contains calls')
    elif len(ops) <= 24:
        labels.append('short leaf candidate')
    if processor in {'x86', 'x86_64'} and sum(op.startswith('v') and op.endswith(('ps', 'pd', 'ss', 'sd')) for op in ops) >= 3:
        labels.append('AVX floating-point operations')
    elif processor == 'mips' and sum(op.endswith(('.s', '.d')) for op in ops) >= 3:
        labels.append('floating-point operations')
    elif processor == 'ppc' and sum(op.startswith(('fadd', 'fmul', 'fsub', 'fdiv', 'fmadd')) for op in ops) >= 3:
        labels.append('floating-point operations')
    if sum(control(processor, op) for op in ops) >= 4:
        labels.append('multiple control transfers')
    return tuple(labels)

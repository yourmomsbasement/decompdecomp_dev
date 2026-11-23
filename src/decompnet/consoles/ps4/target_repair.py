"""Plan missing-relocation repairs from verified original bytes, without writing project files.

This is an explicit migration planner, not a relaxed judge. Existing relocation identities, function spans and
unmasked bytes must remain correct. Callers review/deploy the returned proposal separately.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ...arch.x86_validation import references
from ...forks.project import ForkError
from ...forks.target_validation import MAX_FUNCTION, validate_target
from .scratch import read_elf


@dataclass
class RepairPlan:
    status: str
    config: dict | None = None
    objects: dict[str, bytes] = field(default_factory=dict)
    additions: list[dict] = field(default_factory=list)
    errors: tuple[str, ...] = ()


@lru_cache(maxsize=1)
def _writer():
    path = Path(__file__).with_name('project_template') / 'tools' / 'delink.py'
    spec = importlib.util.spec_from_file_location('decompnet_ps4_repair_delinker', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.elf64


def _object(unit, functions, image):
    start = functions[0]['addr']
    end = max(f['addr'] + f['size'] for f in functions)
    offset = image.offset(start)
    if offset is None or end - start > 16 * MAX_FUNCTION:
        raise ValueError('Selected unit has no bounded original file span.')
    for f in functions:
        if not 0 < f['size'] <= MAX_FUNCTION or image.offset(f['addr']) != f['offset']:
            raise ValueError('Function span or file offset is invalid.')
        segment = image.segment_at(f['addr'])
        if f['addr'] + f['size'] > segment.va + segment.filesz:
            raise ValueError('Function span extends beyond original file bytes.')
    blob = image.data[offset:offset + end - start]
    if len(blob) != end - start:
        raise ValueError('Selected unit exceeds original file bytes.')
    return _writer()(unit, functions, blob, start)


def plan_missing_relocations(config: dict, original: bytes, targets: dict[str, bytes],
                             selected_units: list[str] | None = None) -> RepairPlan:
    """Return a validated in-memory proposal, or invalid with no deployable output.

    The caller supplies a frozen original image, its untouched manifest and current target objects. At most 32
    selected units and 4096 new fields are considered. Stale target objects and wrong existing references reject
    the whole proposal; only absent, independently proven external PC-relative fields can be added.
    """
    try:
        if hashlib.sha1(original).hexdigest() != config.get('sha1'):
            raise ValueError('Original image SHA1 differs from the declared manifest.')
        image = read_elf(original)
        selected = list(targets) if selected_units is None else list(selected_units)
        if not selected or len(selected) > 32 or len(set(selected)) != len(selected):
            raise ValueError('Select between one and 32 distinct target units.')
        proposed = copy.deepcopy(config)
        names = {f['addr']: f['name'] for fs in config['units'].values() for f in fs}
        symbols = {f['name']: f['addr'] for fs in config['units'].values() for f in fs}
        regions = [(s.va, s.va + s.memsz) for s in image.segments]
        additions = []
        objects = {}
        for unit in selected:
            old = config['units'][unit]
            if not old or unit not in targets:
                raise ValueError('Selected unit has no original manifest or target object.')
            if _object(unit, old, image) != targets[unit]:
                raise ValueError('Current target object differs from the existing manifest; refusing stale repair.')
            functions = proposed['units'][unit]
            for fn in functions:
                address, size = fn['addr'], fn['size']
                code = image.read(address, size)
                decoded = {}
                ranges = fn.get('ranges') or fn.get('body_ranges') or [(address, address + size)]
                for start, end in ranges:
                    if not address <= start < end <= address + size:
                        raise ValueError('Original body range is outside its unchanged function span.')
                    found, _ = references(code[start-address:end-address], start, regions)
                    decoded.update({offset + start-address: evidence for offset, evidence in found.items()})
                existing = fn.setdefault('relocs', [])
                occupied = {r[0] for r in existing}
                for offset, (destination, addend, width, kind) in sorted(decoded.items()):
                    if address <= destination < address + size or offset in occupied:
                        continue
                    if width not in (1, 4) or kind not in ('branch', 'data'):
                        raise ValueError('Missing reference has no supported PC32/PC8 proof.')
                    name = names.get(destination, f'D_{destination:08X}')
                    relocation = [offset, 15 if width == 1 else 2, name, addend]
                    existing.append(relocation)
                    additions.append({'unit': unit, 'function': fn['name'], 'offset': offset,
                                      'destination': destination, 'kind': kind, 'relocation': relocation})
                    if len(additions) > 4096:
                        raise ValueError('Repair exceeds the bounded addition limit.')
                existing.sort(key=lambda r: r[0])
            obj = _object(unit, functions, image)
            for fn in functions:
                result = validate_target(image.read(fn['addr'], fn['size']), fn['addr'], obj, fn['name'], symbols,
                                         fn.get('ranges') or fn.get('body_ranges'), regions)
                if result.status != 'valid':
                    raise ValueError(f"Proposed unit {unit}, function {fn['name']} failed strict evidence: "
                                     + ' '.join(result.errors))
            objects[unit] = obj
        return RepairPlan('valid', proposed, objects, additions)
    except (ValueError, KeyError, TypeError, IndexError, ForkError, struct.error) as exc:
        return RepairPlan('invalid', errors=(str(exc),))

"""Read-only checks against original images, independent of objdiff's masked comparison.

These checks validate the target, not a candidate. A valid target still needs the normal strict judge,
including matched neighbours after compiler/dependency changes. Unsupported evidence is never a pass.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

from .. import consoles
from ..arch.x86_validation import references
from ..elfobj import canonical_symbol, extract_function

MAX_FILE = 512 * 1024 * 1024
# failures of the independent decoder itself (it cannot see an operand the delinker relocated), as opposed to
# evidence that contradicts the original: only these may be advisory when the project has not required evidence
ADVISORY = ('Target relocation has no independently decoded original operand',
            'Target contains an unsupported relocation type')
MAX_FUNCTION = 1024 * 1024
WIDTHS = {1: 8, 2: 4, 4: 4, 10: 4, 11: 4, 15: 1}


@dataclass(frozen=True)
class ValidationResult:
    status: str
    errors: tuple[str, ...] = ()
    digest: str = ''
    required: bool = False  # A supported declaration remains mandatory even when its first check fails.


def fingerprint_function(target: bytes, name: str) -> str | None:
    """Canonical compiled code, symbolic relocations and allocated non-code local data.

    File symbols, debug records and ELF source filenames do not affect the fingerprint.
    Local data is conservative: all allocated non-executable sections participate.
    """
    try:
        fn = extract_function(target, name)
        if fn is None:
            return None
        elf = ELFFile(BytesIO(target))
        h = hashlib.sha256(fn.arch.encode() + fn.code)
        h.update(repr(sorted(r.key() for r in fn.relocs)).encode())
        for index, sec in enumerate(elf.iter_sections()):
            if sec['sh_flags'] & 2 and not sec['sh_flags'] & 4:
                h.update(sec.name.encode() + sec.data())
                for rsec in elf.iter_sections():
                    if not isinstance(rsec, RelocationSection) or rsec['sh_info'] != index:
                        continue
                    symtab = elf.get_section(rsec['sh_link'])
                    for reloc in rsec.iter_relocations():
                        symbol = symtab.get_symbol(reloc['r_info_sym'])
                        identity = canonical_symbol(symbol.name, symbol['st_info']['bind'])
                        if symbol['st_info']['type'] == 'STT_SECTION':
                            identity = elf.get_section(symbol['st_shndx']).name
                        h.update(repr((reloc['r_offset'], reloc['r_info_type'], identity,
                                       reloc['r_addend'] if reloc.is_RELA() else None)).encode())
        return h.hexdigest()
    except (ELFError, ValueError, KeyError, IndexError):
        return None


def validate_target(original: bytes, address: int, target: bytes, name: str,
                    symbols: dict[str, int], ranges=None, regions=None) -> ValidationResult:
    """Check one exact original function span against its generated target object."""
    digest = hashlib.sha256(original + target + repr((address, name, ranges, regions)).encode()).hexdigest()
    try:
        if len(target) > MAX_FILE:
            raise ValueError('Target object exceeds the validation size limit.')
        fn = extract_function(target, name)
        if fn is None:
            return ValidationResult('invalid', ('Target function is missing.',), digest)
        # This proof reads only symbols named by this function's relocations. The project-level digest below
        # still includes the complete original manifest, so unrelated symbol edits invalidate that evidence too.
        used_symbols = sorted((name, symbols.get(name)) for name in {r.symbol for r in fn.relocs})
        digest = hashlib.sha256(bytes.fromhex(digest) + repr(used_symbols).encode()).hexdigest()
        if fn.arch != 'x86_64':
            return ValidationResult('unavailable', ('Independent target validation supports ELF x86-64 only.',), digest)
        if not original or len(original) > MAX_FUNCTION or len(fn.code) != len(original):
            raise ValueError('Target function span differs from the original span.')
        elf = ELFFile(BytesIO(target))
        symtab = elf.get_section_by_name('.symtab')
        named_symbols = [s for s in symtab.iter_symbols() if s.name == name]
        if len(named_symbols) != 1 or named_symbols[0]['st_size'] != len(original):
            raise ValueError('Target function symbol has an ambiguous or incorrect declared span.')
        selected = ranges or [(address, address + len(original))]
        if len(selected) > 65536 or selected[0][0] != address or selected[-1][1] != address + len(original):
            raise ValueError('Original body ranges do not establish the complete function span.')
        refs, boundaries = {}, set()
        # The target object holds the function's whole span, gaps included (the delinker copies addr..addr+size);
        # `ranges` names the blocks the function map attributes to it, and decoding only those left every
        # reference inside a gap without evidence ("no independently decoded original operand").
        previous = address
        for start, end in [(address, address + len(original))]:
            if not address <= start < end <= address + len(original) or start < previous:
                raise ValueError('Original body ranges overlap or leave the declared span.')
            found, points = references(original[start-address:end-address], start,
                                       regions or [(0, 1 << 64)])
            refs.update({off + start - address: value for off, value in found.items()})
            boundaries.update(points)
            previous = end
        covered = set()
        for r in fn.relocs:
            width = WIDTHS.get(r.type)
            if width is None:
                raise ValueError('Target contains an unsupported relocation type.')
            fields = set(range(r.offset, r.offset + width))
            if r.offset < 0 or r.offset + width > len(original) or covered & fields:
                raise ValueError('Target relocation is out of bounds or overlaps another operand.')
            evidence = refs.get(r.offset)
            if evidence is None or r.type not in (2, 4, 15):
                raise ValueError('Target relocation has no independently decoded original operand.')
            dest, addend, expected_width, kind = evidence
            if width != expected_width or r.addend != addend:
                raise ValueError('Target relocation operand width or addend differs from original evidence.')
            named = symbols.get(r.symbol)
            match = re.fullmatch(r'(?:D_|func_)([0-9A-Fa-f]+)', r.symbol)
            if named is None and match:
                named = int(match[1], 16)
            if named != dest:
                raise ValueError('Target relocation symbol identifies the wrong original destination.')
            if kind == 'branch' and address <= dest < address + len(original) and dest not in boundaries:
                raise ValueError('Original branch destination is not an instruction boundary.')
            covered.update(fields)
        relocated = {r.offset for r in fn.relocs}
        for off, (dest, _, _, kind) in refs.items():
            if kind == 'branch' and address <= dest < address + len(original) and dest not in boundaries:
                raise ValueError('Original branch destination is not an instruction boundary.')
            # An external reference the delinker left unrelocated is not an error: the field is then compared as
            # literal bytes, and the check below still requires them to equal the original's. Only a *relocated*
            # field is exempt from the byte comparison, so a missing relocation cannot launder a difference.
            if not address <= dest < address + len(original) and off not in relocated:
                continue
        if any(a != b for i, (a, b) in enumerate(zip(original, fn.code)) if i not in covered):
            raise ValueError('Unrelocated target bytes differ from original bytes.')
        return ValidationResult('valid', (), digest)
    except ELFError:
        return ValidationResult('unavailable', ('Target object format is not supported by independent validation.',), digest)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        return ValidationResult('invalid', (str(exc),), digest)


def _read(root: Path, relative: str) -> bytes:
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Evidence paths must stay inside the project.')
    if any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
        raise ValueError('Evidence paths must not use symlinks.')
    path.resolve().relative_to(root.resolve())
    if path.stat().st_size > MAX_FILE:
        raise ValueError('Evidence file exceeds the validation size limit.')
    return path.read_bytes()


@lru_cache(maxsize=1)
def _evidence_config(raw: bytes):
    """Parse exact current manifest content once; validator callers treat the result as read-only.

    Every call still reads and hashes the file bytes. Source timestamps never authorize a cached proof.
    """
    return json.loads(raw)


@lru_cache(maxsize=2)
def _project_check(config_bytes: bytes, original: bytes, targets: tuple, load_types: tuple) -> ValidationResult:
    config = _evidence_config(config_bytes)
    if hashlib.sha1(original).hexdigest() != config.get('sha1'):
        return ValidationResult('invalid', ('Original image hash differs from the declared evidence.',))
    try:
        elf = ELFFile(BytesIO(original))
        if elf['e_machine'] != 'EM_X86_64':
            return ValidationResult('unavailable', ('Independent original validation supports ELF x86-64 only.',))
        loads = [s for s in elf.iter_segments() if s['p_type'] in load_types]
        for segment in loads:
            if not (0 <= segment['p_filesz'] <= segment['p_memsz']
                    and segment['p_offset'] + segment['p_filesz'] <= len(original)
                    and segment['p_vaddr'] + segment['p_memsz'] <= 1 << 64):
                raise ValueError('Original load segment has invalid file or memory bounds.')
        segments = [(s['p_vaddr'], s['p_vaddr'] + s['p_filesz'], s['p_offset'])
                    for s in loads]
        mapped_regions = [(s['p_vaddr'], s['p_vaddr'] + s['p_memsz'])
                          for s in loads]
        rows = config['units']
        if sum(len(fs) for fs in rows.values()) > 200000 or len(targets) > 10000:
            raise ValueError('Original manifest exceeds the validation work limit.')
        names = [f['name'] for fs in rows.values() for f in fs]
        if len(set(names)) != len(names):
            raise ValueError('Original function symbols are ambiguous.')
        symbols = {f['name']: f['addr'] for fs in rows.values() for f in fs}
        result_hash = hashlib.sha256(config_bytes + original + repr(load_types).encode())
        for unit, target in targets:
            if unit not in rows or not rows[unit]:
                raise ValueError('Target unit has no original function evidence.')
            result_hash.update(target)
            for f in rows[unit]:
                address, size = f['addr'], f['size']
                segment = next((s for s in segments if s[0] <= address and address + size <= s[1]), None)
                if segment is None:
                    raise ValueError('Original function span is outside mapped file bytes.')
                offset = address - segment[0] + segment[2]
                if f.get('offset', offset) != offset:
                    raise ValueError('Manifest function file offset differs from original ELF mapping.')
                result = validate_target(original[offset:offset+size], address, target, f['name'], symbols,
                                         f.get('ranges') or f.get('body_ranges'),
                                         mapped_regions)
                if result.status != 'valid':
                    return ValidationResult('invalid', result.errors, result.digest)
        return ValidationResult('valid', (), result_hash.hexdigest())
    except (ELFError, ValueError, KeyError, TypeError, IndexError) as exc:
        return ValidationResult('invalid', (f'Original evidence could not be verified: {exc}',))


def validate_project_targets(root: Path, targets: dict[str, bytes | Path | None], manifest=None) -> ValidationResult:
    """Validate supplied units; callers include every affected matched neighbour before keeping a build.

    Content-keyed caching deliberately rereads evidence on every call: timestamps cannot establish freshness.
    Projects without an ELF evidence declaration report unavailable; declared corrupt evidence fails closed.
    """
    declared = False
    required = False
    try:
        manifest = manifest if manifest is not None else json.loads(_read(root, '.decomp.wtf.json'))
        required = bool((manifest.get('target_validation') or {}).get('required'))
        version = str(manifest.get('version', ''))
        if not version or '/' in version or '\\' in version or version in ('.', '..'):
            if required:
                raise ValueError('Required original evidence has no safe version declaration.')
            return ValidationResult('unavailable', ('No versioned original evidence is declared.',))
        config_path = f'config/{version}/functions.json'
        if not (root / config_path).exists():
            if required:
                raise ValueError('Required original evidence manifest is missing.')
            return ValidationResult('unavailable', ('No original target evidence manifest is available.',))
        raw = _read(root, config_path)
        config = _evidence_config(raw)
        if 'elf' not in config:
            if required:
                raise ValueError('Required original evidence has no supported ELF declaration.')
            return ValidationResult('unavailable', ('This original evidence format has no ELF validator.',))
        declared = True
        original = _read(root, config['elf'])
        if hashlib.sha1(original).hexdigest() != config.get('sha1'):
            raise ValueError('Original image hash differs from the declared evidence.')
        try:
            original_elf = ELFFile(BytesIO(original))
        except ELFError as exc:
            raise ValueError(f'Declared original ELF evidence is corrupt: {exc}') from exc
        if original_elf['e_machine'] != 'EM_X86_64':
            if required:
                raise ValueError('Required original evidence has no supported processor validator.')
            return ValidationResult('unavailable', ('Independent original validation supports ELF x86-64 only.',))
        if not targets:
            raise ValueError('No target units were supplied for declared original evidence.')
        contents = {}
        for name, target in targets.items():
            if target is None:
                raise ValueError(f'Target unit {name} has no original object path.')
            contents[name] = _read(root, str(target.relative_to(root))) if isinstance(target, Path) else target
        load_types = consoles.get(manifest.get('platform')).original_load_types(original_elf)
        result = _project_check(raw, original, tuple(sorted(contents.items())), load_types)
        return replace(result, required=required or result.status != 'unavailable')
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return ValidationResult('invalid', (f'Original target evidence is unavailable or corrupt: {exc}',),
                                required=declared or required)

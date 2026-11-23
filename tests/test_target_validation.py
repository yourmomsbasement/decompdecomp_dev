"""Independent checks use homemade ELF objects and instruction bytes only."""
import struct

import pytest

from decompnet.forks.target_validation import fingerprint_function, validate_project_targets, validate_target


def object_for(code, relocs=(), filename='a.c'):
    names = ['demo', *dict.fromkeys(r[2] for r in relocs), filename]
    strings = b'\0'
    indices = {}
    for name in names:
        indices[name] = len(strings)
        strings += name.encode() + b'\0'
    symbols = bytes(24) + struct.pack('<IBBHQQ', indices['demo'], 0x12, 0, 1, 0, len(code))
    external = list(dict.fromkeys(r[2] for r in relocs))
    for name in external:
        symbols += struct.pack('<IBBHQQ', indices[name], 0x10, 0, 0, 0, 0)
    symbols += struct.pack('<IBBHQQ', indices[filename], 4, 0, 0xfff1, 0, 0)
    rela = b''.join(struct.pack('<QQq', off, ((external.index(name)+2) << 32) | typ, addend)
                    for off, typ, name, addend in relocs)
    shstr = b'\0.text\0.symtab\0.strtab\0.rela.text\0.shstrtab\0'
    data = [b'', code, symbols, strings, rela, shstr]
    offsets, body = [], b''
    for chunk in data:
        offsets.append(64 + len(body))
        body += chunk
    headers = bytes(64)
    for i, (name, typ, flags, link, info, align, entsize) in enumerate([
            (1, 1, 6, 0, 0, 1, 0), (7, 2, 0, 3, 1, 8, 24), (15, 3, 0, 0, 0, 1, 0),
            (23, 4, 0, 2, 1, 8, 24), (34, 3, 0, 0, 0, 1, 0)], 1):
        headers += struct.pack('<IIQQQQIIQQ', name, typ, flags, 0, offsets[i], len(data[i]),
                               link, info, align, entsize)
    ident = b'\x7fELF\x02\x01\x01' + bytes(9)
    return ident + struct.pack('<HHIQQQIHHHHHH', 1, 62, 1, 0, 0, 64+len(body), 0,
                               64, 0, 0, 64, 6, 5) + body + headers


def test_original_call_identity_and_symbol_table_index():
    original = bytes.fromhex('e8fb0f0000c3')
    target = object_for(bytes.fromhex('e800000000c3'), [(1, 2, 'callee', -4)])
    assert validate_target(original, 0x1000, target, 'demo', {'callee': 0x2000}).status == 'valid'
    assert validate_target(original, 0x1000, target, 'demo', {'callee': 0x3000}).status == 'invalid'


def test_short_branch_masks_one_byte_only():
    original = bytes.fromhex('750ec3')
    target = object_for(bytes.fromhex('7500c3'), [(1, 15, 'dest', -1)])
    assert validate_target(original, 0x1000, target, 'demo', {'dest': 0x1010}).status == 'valid'
    bad = object_for(bytes.fromhex('750090'), [(1, 15, 'dest', -1)])
    assert validate_target(original, 0x1000, bad, 'demo', {'dest': 0x1010}).status == 'invalid'


def test_trailing_immediate_data_addend():
    original = bytes.fromhex('c7050100000007000000c3')
    target = object_for(bytes.fromhex('c7050000000007000000c3'), [(2, 2, 'data', -8)])
    assert validate_target(original, 0x1000, target, 'demo', {'data': 0x100b}).status == 'valid'
    bad = object_for(bytes.fromhex('c7050000000007000000c3'), [(2, 2, 'data', -4)])
    assert validate_target(original, 0x1000, bad, 'demo', {'data': 0x100b}).status == 'invalid'


@pytest.mark.parametrize('relocs', [[(1, 2, 'callee', -4), (1, 2, 'callee', -4)],
                                    [(1, 1, 'callee', 0)], [(1, 99, 'callee', -4)]])
def test_bad_relocations_fail_closed(relocs):
    target = object_for(bytes.fromhex('e800000000c3'), relocs)
    assert validate_target(bytes.fromhex('e8fb0f0000c3'), 0x1000, target, 'demo',
                           {'callee': 0x2000}).status == 'invalid'


def test_body_count_cannot_replace_span():
    target = object_for(bytes.fromhex('c39090c3'))
    assert validate_target(bytes.fromhex('c39090c3'), 0x1000, target, 'demo', {},
                           [(0x1000, 0x1001), (0x1003, 0x1004)]).status == 'valid'
    assert validate_target(b'\xc3\xc3', 0x1000, target, 'demo', {}).status == 'invalid'


def test_fingerprint_ignores_source_filename():
    assert fingerprint_function(object_for(b'\xc3', filename='a.c'), 'demo') == fingerprint_function(
        object_for(b'\xc3', filename='b.c'), 'demo')
    assert fingerprint_function(object_for(b'\x90\xc3'), 'demo') != fingerprint_function(
        object_for(b'\xc3'), 'demo')


def test_declared_missing_evidence_fails_closed(tmp_path):
    config = tmp_path / 'config/v'
    config.mkdir(parents=True)
    (config / 'functions.json').write_text('{"elf":"orig/missing.elf", "units":{}}')
    assert validate_project_targets(tmp_path, {'unit': b''}, {'version': 'v'}).status == 'invalid'


def test_unknown_object_explicitly_unavailable():
    assert validate_target(b'\xc3', 0, b'unknown', 'demo', {}).status == 'unavailable'


def original_image(code):
    ident = b'\x7fELF\x02\x01\x01' + bytes(9)
    header = struct.pack('<HHIQQQIHHHHHH', 2, 62, 1, 0x1000, 64, 0, 0,
                         64, 56, 1, 64, 0, 0)
    segment = struct.pack('<IIQQQQQQ', 1, 5, 120, 0x1000, 0x1000, len(code), len(code), 1)
    return ident + header + segment + code


def test_project_content_cache_rechecks_original_and_neighbors(tmp_path):
    import hashlib
    import json

    config = tmp_path / 'config/v'
    config.mkdir(parents=True)
    (tmp_path / 'orig').mkdir()
    image = original_image(b'\xc3\xc3')
    original = tmp_path / 'orig/image.elf'
    original.write_bytes(image)
    manifest = {'elf': 'orig/image.elf', 'sha1': hashlib.sha1(image).hexdigest(), 'units': {
        'owner': [{'name': 'demo', 'addr': 0x1000, 'size': 1, 'offset': 120}],
        'neighbor': [{'name': 'peer', 'addr': 0x1001, 'size': 1, 'offset': 121}]}}
    path = config / 'functions.json'
    path.write_text(json.dumps(manifest))
    targets = {'owner': object_for(b'\xc3'), 'neighbor': object_for(b'\xc3').replace(b'demo', b'peer')}
    assert validate_project_targets(tmp_path, targets, {'version': 'v'}).status == 'valid'
    # The integration hook supplies affected matched neighbours too: a changed neighbour cannot pass.
    targets['neighbor'] = object_for(b'\x90').replace(b'demo', b'peer')
    assert validate_project_targets(tmp_path, targets, {'version': 'v'}).status == 'invalid'
    targets['neighbor'] = object_for(b'\xc3').replace(b'demo', b'peer')
    original.write_bytes(image[:-1] + b'\x90')
    assert validate_project_targets(tmp_path, targets, {'version': 'v'}).status == 'invalid'
    original.write_bytes(image)
    manifest['units']['owner'][0]['offset'] = 121
    path.write_text(json.dumps(manifest))
    assert validate_project_targets(tmp_path, targets, {'version': 'v'}).status == 'invalid'


def test_original_internal_branch_requires_instruction_boundary():
    # jmp into the middle of its own two-byte instruction.
    code = bytes.fromhex('ebffc3')
    assert validate_target(code, 0x1000, object_for(code), 'demo', {}).status == 'invalid'


def test_evidence_symlink_is_rejected(tmp_path):
    config = tmp_path / 'config/v'
    config.mkdir(parents=True)
    evidence = tmp_path / 'evidence.json'
    evidence.write_text('{"elf":"orig/image.elf"}')
    (config / 'functions.json').symlink_to(evidence)
    assert validate_project_targets(tmp_path, {'unit': b''}, {'version': 'v'}).status == 'invalid'


def test_project_rip_data_reference_into_zero_initialized_memory(tmp_path):
    import hashlib
    import json

    # BSS occupies mapped memory beyond the bytes stored in the original ELF file.
    code = bytes.fromhex('488b0500010000c3')
    image = bytearray(original_image(code))
    struct.pack_into('<Q', image, 104, 0x200)  # PT_LOAD p_memsz; p_filesz stays len(code).
    (tmp_path / 'image.elf').write_bytes(image)
    config = tmp_path / 'config/v'
    config.mkdir(parents=True)
    (config / 'functions.json').write_text(json.dumps({'elf': 'image.elf',
        'sha1': hashlib.sha1(image).hexdigest(), 'units': {'unit': [
            {'name': 'demo', 'addr': 0x1000, 'offset': 120, 'size': len(code)}]}}))
    target = object_for(bytes.fromhex('488b0500000000c3'), [(3, 2, 'D_00001107', -4)])
    assert validate_project_targets(tmp_path, {'unit': target}, {'version': 'v'}).status == 'valid'


def extended_image(code, segment_type=0x61000010, image_type=0xfe10, mem_size=16, file_size=8):
    """Homemade executable with code and one console-defined mapped read-only segment."""
    ident = b'\x7fELF\x02\x01\x01' + bytes(9)
    header = struct.pack('<HHIQQQIHHHHHH', image_type, 62, 1, 0x1000, 64, 0, 0,
                         64, 56, 2, 64, 0, 0)
    text = struct.pack('<IIQQQQQQ', 1, 5, 176, 0x1000, 0x1000, len(code), len(code), 1)
    data = struct.pack('<IIQQQQQQ', segment_type, 4, 176+len(code), 0x3000, 0x3000,
                       file_size, mem_size, 1)
    return ident + header + text + data + code + bytes(8)


def extended_project(tmp_path, image):
    import hashlib
    import json
    (tmp_path / 'image.elf').write_bytes(image)
    config = tmp_path / 'config/v'
    config.mkdir(exist_ok=True, parents=True)
    (config / 'functions.json').write_text(json.dumps({'elf': 'image.elf',
        'sha1': hashlib.sha1(image).hexdigest(), 'units': {'unit': [
            {'name': 'demo', 'addr': 0x1000, 'offset': 176, 'size': 8}]}}))


@pytest.mark.parametrize('destination', [0x3000, 0x300f])
def test_console_readonly_segment_and_zero_fill_are_independent_operand_evidence(tmp_path, destination):
    code = b'\x48\x8d\x05' + struct.pack('<i', destination-0x1007) + b'\xc3'
    extended_project(tmp_path, extended_image(code))
    target = object_for(bytes.fromhex('488d0500000000c3'), [(3, 2, f'D_{destination:08X}', -4)])
    manifest = {'platform': 'ps4', 'version': 'v'}
    assert validate_project_targets(tmp_path, {'unit': target}, manifest).status == 'valid'
    # Console load policy participates in cache identity: unknown projects cannot inherit this verdict.
    assert validate_project_targets(tmp_path, {'unit': target}, {'version': 'v'}).status == 'invalid'
    assert validate_project_targets(tmp_path, {'unit': target}, manifest).status == 'valid'


@pytest.mark.parametrize('change', ['symbol', 'addend', 'width', 'offset', 'missing', 'byte',
                                     'unmapped', 'metadata', 'ordinary_elf', 'file_bounds', 'memory_bounds'])
def test_console_mapping_preserves_all_operand_and_original_bounds_checks(tmp_path, change):
    destination = 0x3010 if change == 'unmapped' else 0x3000
    code = b'\x48\x8d\x05' + struct.pack('<i', destination-0x1007) + b'\xc3'
    image = extended_image(code, segment_type=7 if change == 'metadata' else 0x61000010,
                           image_type=2 if change == 'ordinary_elf' else 0xfe10,
                           file_size=80 if change == 'file_bounds' else 8,
                           mem_size=4 if change == 'memory_bounds' else 128 if change == 'file_bounds' else 16)
    extended_project(tmp_path, image)
    body = bytes.fromhex('488d050000000090' if change == 'byte' else '488d0500000000c3')
    relocs = [] if change == 'missing' else [(2 if change == 'offset' else 3,
        15 if change == 'width' else 2, 'D_00003001' if change == 'symbol' else f'D_{destination:08X}',
        -8 if change == 'addend' else -4)]
    target = object_for(body, relocs)
    assert validate_project_targets(tmp_path, {'unit': target}, {'platform': 'ps4', 'version': 'v'}).status == 'invalid'


def test_function_evidence_tracks_every_referenced_symbol_without_serializing_unrelated_symbols():
    original = bytes.fromhex('e8fb0f0000c3')
    target = object_for(bytes.fromhex('e800000000c3'), [(1, 2, 'callee', -4)])
    symbols = {'callee': 0x2000, **{f'unused_{n}': n for n in range(10000)}}
    valid = validate_target(original, 0x1000, target, 'demo', symbols)
    assert valid.status == 'valid'
    symbols['callee'] = 0x3000
    invalid = validate_target(original, 0x1000, target, 'demo', symbols)
    assert invalid.status == 'invalid' and invalid.digest != valid.digest


def test_original_manifest_parse_cache_uses_content_not_timestamps(tmp_path):
    import hashlib
    import json
    import os
    code = bytes.fromhex('e8fb0f0000c3')
    image = original_image(code)
    (tmp_path / 'image.elf').write_bytes(image)
    config_dir = tmp_path / 'config/v'
    config_dir.mkdir(parents=True)
    config = {'elf': 'image.elf', 'sha1': hashlib.sha1(image).hexdigest(), 'units': {'unit': [
        {'name': 'demo', 'addr': 0x1000, 'offset': 120, 'size': len(code)}]}}
    # Address-style names resolve against the independently decoded original operand.
    target = object_for(bytes.fromhex('e800000000c3'), [(1, 2, 'D_00002000', -4)])
    path = config_dir / 'functions.json'
    path.write_text(json.dumps(config)); stamp = path.stat()
    assert validate_project_targets(tmp_path, {'unit': target}, {'version': 'v'}).status == 'valid'
    config['units']['unit'][0]['offset'] = 121
    path.write_text(json.dumps(config)); os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert validate_project_targets(tmp_path, {'unit': target}, {'version': 'v'}).status == 'invalid'

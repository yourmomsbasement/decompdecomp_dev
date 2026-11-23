"""Advisory index digests avoid repeated object reads while detecting replacement and racy writes."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

from decompnet.app import target_stamps


def test_unchanged_targets_are_read_once_and_shared_across_callers(tmp_path, monkeypatch):
    path = tmp_path / 'target.o'
    path.write_bytes(b'original target')
    real_time = target_stamps.time.time_ns()
    monkeypatch.setattr(target_stamps.time, 'time_ns', lambda: real_time + 3_000_000_000)
    reads = []
    original = Path.open
    def opened(file, *args, **kwargs):
        if file == path:
            reads.append(1)
        return original(file, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', opened)
    net = SimpleNamespace()
    units = [SimpleNamespace(name=name, target=path) for name in ('a', 'b')]
    expected = hashlib.sha256(b'original target').hexdigest()
    assert target_stamps.target_hashes(net, units) == {'a': expected, 'b': expected}
    assert target_stamps.target_hashes(net, units) == {'a': expected, 'b': expected}
    assert len(reads) == 1


def test_same_size_replacement_and_deleted_targets_invalidate_cache(tmp_path, monkeypatch):
    path = tmp_path / 'target.o'
    path.write_bytes(b'original')
    now = target_stamps.time.time_ns()
    monkeypatch.setattr(target_stamps.time, 'time_ns', lambda: now + 3_000_000_000)
    cache = target_stamps.TargetStamps()
    first = cache.digest(path)
    replacement = tmp_path / 'replacement.o'
    replacement.write_bytes(b'replaced')
    replacement.replace(path)
    assert cache.digest(path) == hashlib.sha256(b'replaced').hexdigest() != first
    path.unlink()
    assert cache.digest(path) is None and not cache.entries


def test_recent_files_with_restored_mtime_are_not_reused(tmp_path):
    import os
    path = tmp_path / 'target.o'
    path.write_bytes(b'original')
    cache = target_stamps.TargetStamps()
    first, stamp = cache.digest(path), path.stat()
    path.write_bytes(b'replaced')
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert cache.digest(path) != first
    assert not cache.entries


def test_cache_has_a_maximum_age_even_if_metadata_cannot_show_changes(tmp_path, monkeypatch):
    path = tmp_path / 'target.o'
    path.write_bytes(b'original')
    clock = [100]
    now = target_stamps.time.time_ns()
    monkeypatch.setattr(target_stamps.time, 'time_ns', lambda: now + 3_000_000_000)
    monkeypatch.setattr(target_stamps.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(target_stamps, '_stamp', lambda info: 'coarse metadata')
    cache = target_stamps.TargetStamps()
    first = cache.digest(path)
    path.write_bytes(b'replaced')
    clock[0] += target_stamps.MAX_AGE + 1
    assert cache.digest(path) != first


def test_changing_target_during_read_is_not_added_to_cache(tmp_path, monkeypatch):
    path = tmp_path / 'target.o'
    path.write_bytes(b'original')
    original = target_stamps._stamp
    calls = []
    def changed(info):
        calls.append(1)
        return (original(info), len(calls))
    monkeypatch.setattr(target_stamps, '_stamp', changed)
    cache = target_stamps.TargetStamps()
    assert cache.digest(path) is None and not cache.entries


def test_digest_cache_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(target_stamps, 'MAX_ENTRIES', 2)
    now = target_stamps.time.time_ns()
    monkeypatch.setattr(target_stamps.time, 'time_ns', lambda: now + 3_000_000_000)
    cache = target_stamps.TargetStamps()
    for i in range(3):
        path = tmp_path / f'{i}.o'
        path.write_bytes(b'target')
        assert cache.digest(path)
    assert len(cache.entries) == 2

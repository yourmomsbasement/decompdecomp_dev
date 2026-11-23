"""Reuse stable target digests for advisory donor indexes, never as a matching verdict.

Every lookup checks file identity, size and change times. A short maximum age also covers filesystems whose
metadata can be restored or has coarse resolution. The ordinary judge still reads and compares its own inputs.
"""

import hashlib
import threading
import time
from collections import OrderedDict
from pathlib import Path
from stat import S_ISREG

MAX_ENTRIES = 8192
MAX_AGE = 30
CHUNK_BYTES = 1024 * 1024
RACY_NS = 2_000_000_000
_factory_lock = threading.Lock()


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class TargetStamps:
    """Keep bounded digest metadata, not object contents; skip targets replaced while being read."""
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = OrderedDict()

    def digest(self, path):
        path = Path(path)
        key = str(path.absolute())
        with self.lock:
            try:
                info = path.stat()
                if not S_ISREG(info.st_mode):
                    raise OSError('not a regular target file')
                stamp, now = _stamp(info), time.monotonic()
                # A write and an immediate mtime restore can share a filesystem clock tick. As with git's racy
                # files, re-read recently changed objects until their metadata can distinguish later writes.
                stable = max(info.st_mtime_ns, info.st_ctime_ns) < time.time_ns() - RACY_NS
                old = self.entries.get(key)
                if stable and old and old[0] == stamp and now - old[1] < MAX_AGE:
                    self.entries.move_to_end(key)
                    return old[2]
                digest = hashlib.sha256()
                remaining = info.st_size
                with path.open('rb') as stream:
                    while remaining:
                        data = stream.read(min(remaining, CHUNK_BYTES))
                        if not data:
                            raise OSError('target shrank while reading')
                        remaining -= len(data)
                        digest.update(data)
                    if stream.read(1):
                        raise OSError('target grew while reading')
                if _stamp(path.stat()) != stamp:
                    raise OSError('target changed while reading')
            except OSError:
                self.entries.pop(key, None)
                return None
            value = digest.hexdigest()
            if not stable:
                self.entries.pop(key, None)
                return value
            self.entries[key] = stamp, now, value
            self.entries.move_to_end(key)
            while len(self.entries) > MAX_ENTRIES:
                self.entries.popitem(last=False)
            return value


def target_hashes(net, units):
    """Share digest work between pattern and block hints on the same network."""
    cache = getattr(net, '_target_stamps', None)
    if cache is None:
        with _factory_lock:
            cache = getattr(net, '_target_stamps', None)
            if cache is None:
                cache = net._target_stamps = TargetStamps()
    return {u.name: digest for u in units if u.target is not None and (digest := cache.digest(u.target)) is not None}

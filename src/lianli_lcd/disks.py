"""Read-only local filesystem usage, isolated from the CPU/GPU sampler."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import threading
import time

# Exclude network, pseudo and container filesystems. No directory traversal or file reads.
LOCAL_FILESYSTEMS = frozenset({
    'ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'f2fs', 'bcachefs', 'jfs', 'reiserfs',
    'vfat', 'exfat', 'ntfs', 'ntfs3', 'fuseblk', 'zfs',
})
POLL_SECONDS = 5
STALE_SECONDS = 15


def unescape_mount(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


@dataclass(frozen=True)
class Mount:
    path: str
    device: int
    root: str
    filesystem: str
    source: str

    @property
    def key(self):
        return 'disk_' + hashlib.sha256(self.path.encode()).hexdigest()[:12] + '_used'


def parse_mounts(text):
    mounts = {}
    for line in text.splitlines():
        try:
            before, after = line.split(' - ', 1)
            fields, fs = before.split(), after.split()
            major, minor = map(int, fields[2].split(':'))
            path, root, source = map(unescape_mount, (fields[4], fields[3], fs[1]))
            # Bind-mounted subdirectories and application/container mounts are not partitions.
            if (fs[0] not in LOCAL_FILESYSTEMS or not path.startswith('/')
                    or not (source.startswith('/dev/') or fs[0] == 'zfs')
                    or (root != '/' and fs[0] != 'btrfs')
                    or path.startswith(('/proc/', '/sys/', '/dev/', '/var/lib/docker/',
                                        '/var/lib/containers/', '/snap/'))):
                continue
            mounts[path] = Mount(path, os.makedev(major, minor), root, fs[0], source)
        except (IndexError, ValueError, OverflowError):
            continue
    return mounts


def mounted_filesystems():
    return parse_mounts(Path('/proc/self/mountinfo').read_text())


def used_percent(stats):
    # Match df: reserved blocks unavailable to ordinary users are not counted as free space.
    used = stats.f_blocks - stats.f_bfree
    available = max(0, stats.f_bavail)
    if stats.f_blocks <= 0 or not 0 <= stats.f_bfree <= stats.f_blocks:
        raise ValueError('Invalid filesystem block counters')
    if available > stats.f_bfree or used + available <= 0:
        raise ValueError('Invalid filesystem available space')
    return 100 * used / (used + available)


def measure(mount, current):
    # A missing mount must never silently become a reading of its parent filesystem.
    if current.get(mount.path) != mount:
        raise OSError('Filesystem is no longer mounted at the selected path')
    if os.stat(mount.path).st_dev != mount.device:
        raise OSError('Filesystem changed before sampling')
    value = used_percent(os.statvfs(mount.path))
    if os.stat(mount.path).st_dev != mount.device:
        raise OSError('Filesystem changed during sampling')
    return value


class DiskTelemetry:
    def __init__(self, simulate=False):
        self.simulate = simulate
        if simulate:
            self.mounts = [Mount('/', 0, '/', 'ext4', '/dev/sim-root'),
                           Mount('/data', 1, '/', 'ext4', '/dev/sim-data')]
        else:
            try:
                self.mounts = sorted(mounted_filesystems().values(), key=lambda mount: mount.path)
            except OSError:
                self.mounts = []
        names = [Path(m.path).name or '/' for m in self.mounts]
        self.labels = {m.key: f'Disk {name if names.count(name) == 1 else m.path}'
                       for m, name in zip(self.mounts, names)}
        self.values = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None

    def sample(self):
        try:
            current = {} if self.simulate else mounted_filesystems()
        except OSError:
            current = {}
        for index, mount in enumerate(self.mounts):
            if self.stop.is_set():
                break
            try:
                value = (37.5 + index * 20) if self.simulate else measure(mount, current)
                with self.lock:
                    self.values[mount.key] = (value, time.monotonic())
            except (OSError, ValueError):
                with self.lock:
                    self.values.pop(mount.key, None)

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            return {key: value for key, (value, updated) in self.values.items()
                    if now - updated < STALE_SECONDS}

    def start(self):
        def run():
            while not self.stop.is_set():
                self.sample()
                if self.stop.wait(POLL_SECONDS):
                    break
        self.thread = threading.Thread(target=run, name='disk-space', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)

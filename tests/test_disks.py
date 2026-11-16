from dataclasses import replace
import os
import threading
import time
from types import SimpleNamespace

import pytest

from lianli_lcd import disks
from lianli_lcd.telemetry import Telemetry

MOUNTS = r'''30 1 259:2 / / rw - ext4 /dev/nvme0n1p2 rw
31 1 259:4 / /home/user/Red rw - ext4 /dev/nvme2n1 rw
32 1 8:1 / /media/user/blue rw - ext4 /dev/sdb1 rw
33 1 8:2 / /media/user/My\040Disk rw - ntfs3 /dev/sdc1 rw
34 1 0:30 / /run/user rw - tmpfs tmpfs rw
35 1 0:31 / /mnt/network rw - nfs server:/share rw
36 1 7:1 / /snap/app/1 ro - squashfs /dev/loop1 ro
37 1 259:2 /home/user /bind rw - ext4 /dev/nvme0n1p2 rw
38 1 0:42 /@home /home rw - btrfs /dev/nvme1n1p2 rw
39 1 259:2 / /var/lib/docker/volumes/example rw - ext4 /dev/nvme0n1p2 rw
malformed
'''


def stats(total=1000, free=300, available=250):
    return SimpleNamespace(f_blocks=total, f_bfree=free, f_bavail=available)


def test_local_partitions_names_escapes_btrfs_and_stable_ids():
    mounts = disks.parse_mounts(MOUNTS)
    assert set(mounts) == {'/', '/home/user/Red', '/media/user/blue', '/media/user/My Disk', '/home'}
    assert mounts['/media/user/My Disk'].source == '/dev/sdc1'
    assert mounts == disks.parse_mounts('\n'.join(reversed(MOUNTS.splitlines())))
    assert len({m.key for m in mounts.values()}) == 5
    root = mounts['/']
    assert root.key == replace(root, device=123, source='/dev/renamed').key
    assert root.key != replace(root, path='/other').key
    removable = disks.parse_mounts('40 1 8:3 / /run/media/user/USB rw - exfat /dev/sdd1 rw')
    assert '/run/media/user/USB' in removable


def test_used_percentage_matches_df_reserved_blocks_and_edge_values():
    assert disks.used_percent(stats()) == pytest.approx(100 * 700 / 950)
    assert disks.used_percent(stats(100, 0, 0)) == 100
    assert disks.used_percent(stats(100, 100, 100)) == 0
    assert disks.used_percent(stats(100, 10, -10)) == 100
    for bad in [stats(0, 0, 0), stats(100, 101, 0), stats(100, 10, 20), stats(100, 100, 0)]:
        with pytest.raises(ValueError):
            disks.used_percent(bad)


def test_unmounted_partition_does_not_fall_back_to_parent(monkeypatch):
    mounts = disks.parse_mounts(MOUNTS)
    target = mounts['/media/user/blue']
    reads = []
    monkeypatch.setattr(disks.os, 'statvfs', lambda path: reads.append(path) or stats())
    monkeypatch.setattr(disks.os, 'stat', lambda _: SimpleNamespace(st_dev=target.device))
    assert disks.measure(target, mounts) == pytest.approx(100 * 700 / 950)
    assert reads == [target.path]
    reads.clear()
    with pytest.raises(OSError):
        disks.measure(target, {})
    monkeypatch.setattr(disks.os, 'stat', lambda _: SimpleNamespace(st_dev=mounts['/'].device))
    with pytest.raises(OSError):
        disks.measure(target, mounts)
    assert reads == []
    # An unmount between statvfs and the final stat must also discard the result.
    devices = iter([target.device, mounts['/'].device])
    monkeypatch.setattr(disks.os, 'stat', lambda _: SimpleNamespace(st_dev=next(devices)))
    with pytest.raises(OSError):
        disks.measure(target, mounts)


def test_mount_failures_are_independent_and_remount_restores_same_sensor(monkeypatch):
    mounts = disks.parse_mounts(MOUNTS)
    monkeypatch.setattr(disks, 'mounted_filesystems', lambda: mounts.copy())
    sampler = disks.DiskTelemetry()
    def read(mount, current):
        if mount.path not in current:
            raise OSError('unmounted')
        return 42.0
    monkeypatch.setattr(disks, 'measure', read)
    sampler.sample()
    assert len(sampler.snapshot()) == 5
    removed = mounts.pop('/media/user/blue')
    sampler.sample()
    assert removed.key not in sampler.snapshot()
    assert len(sampler.snapshot()) == 4
    assert removed.key in sampler.labels
    mounts[removed.path] = removed
    sampler.sample()
    assert sampler.snapshot()[removed.key] == 42
    with sampler.lock:
        sampler.values[removed.key] = (42, time.monotonic() - 16)
    assert removed.key not in sampler.snapshot()
    assert len(sampler.snapshot()) == 4
    sampler.close()


def test_slow_disk_does_not_block_cpu_ram_or_status(monkeypatch):
    root = disks.parse_mounts(MOUNTS)['/']
    monkeypatch.setattr(disks, 'mounted_filesystems', lambda: {'/': root})
    monkeypatch.setattr('lianli_lcd.telemetry.nvidia_query', lambda: [])
    entered, release = threading.Event(), threading.Event()
    def blocked(*_):
        entered.set()
        release.wait(3)
        return 50
    monkeypatch.setattr(disks, 'measure', blocked)
    telemetry = Telemetry()
    telemetry.disks.start()
    try:
        assert entered.wait(1)
        start = time.monotonic()
        telemetry.sample()
        status = telemetry.status()
        assert time.monotonic() - start < 1
        assert any(s.id == 'ram_load' for s in status)
        assert all(not s.id.startswith('disk_') for s in status)
    finally:
        release.set()
        telemetry.close()


def test_real_statvfs_uses_metadata_only():
    # Exercise the actual OS calls without assuming the CI runner's filesystem type.
    mount = disks.Mount('/', os.stat('/').st_dev, '/', 'test', 'test')
    assert 0 <= disks.measure(mount, {'/': mount}) <= 100


def test_simulated_disks_are_independent_read_only_percent_sources():
    telemetry = Telemetry(simulate=True)
    telemetry.sample()
    channels = telemetry.descriptor().info.channels
    values = {s.id: s.speed.duty for s in telemetry.status() if s.id.startswith('disk_')}
    assert sorted(values.values()) == [37.5, 57.5]
    assert {channels[key].label for key in values} == {'Disk /', 'Disk data'}
    assert all(channels[key].WhichOneof('options') is None for key in values)
    telemetry.close()

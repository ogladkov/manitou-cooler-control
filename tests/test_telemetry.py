import subprocess
import time

import pytest

from lianli_lcd.telemetry import Telemetry, cpu_counters, cpu_usage, gpu_values, ram_usage
from lianli_lcd.rgb_protocol import tl_rpms, wireless_groups
from test_rgb_protocol import record, response


def test_cpu_load_deltas_exclude_double_counted_guest_and_iowait():
    first = cpu_counters('cpu  100 10 30 800 20 3 7 30 50 0\ncpu0 1 2 3 4\n')
    second = cpu_counters('cpu  120 10 40 850 30 3 7 40 60 0\n')
    assert cpu_usage(first, second) == 40
    assert cpu_usage(second, second) is None
    assert cpu_usage(second, first) is None
    assert cpu_usage((0, 0), (100, 100)) == 0


def test_ram_uses_available_not_free_and_rejects_missing_sample():
    assert ram_usage('MemTotal: 1000 kB\nMemAvailable: 400 kB\nMemFree: 100 kB') == 60
    with pytest.raises(KeyError):
        ram_usage('MemTotal: 1000 kB')
    with pytest.raises(ValueError):
        ram_usage('MemTotal: 0 kB\nMemAvailable: 0 kB')


def test_gpu_memory_occupancy_is_distinct_from_gpu_load_and_ids_survive_reorder():
    first = ['GPU-a', 'GPU A', '15', '6000', '10000']
    second = ['GPU-b', 'GPU B', '0', '1000', '10000']
    values = gpu_values([first, second])
    assert sorted(values.values()) == [0, 10, 15, 60]
    assert values == gpu_values([second, first])
    assert not gpu_values([['GPU-a', 'GPU A', '[N/A]', '[N/A]', '10000']])
    assert not gpu_values([['GPU-a', 'GPU A', 'NaN', '1', 'inf']])


def test_wireless_rpm_masks_flags_and_preserves_stopped_fans():
    r = record(count=3)
    r[28:34] = bytes.fromhex('64b005dc0000')  # MB/PWM flags + 1200, 1500, stopped
    group = wireless_groups(response(r))[0]
    assert group.rpms == (1200, 1500, 0)
    assert group.mb_sync
    assert tl_rpms(bytes.fromhex('01a1000000068004b0a00000')) == {
        'port-1-fan-1': 1200, 'port-3-fan-1': 0}


def test_sampler_is_read_only_and_omits_stale_or_failed_values(monkeypatch):
    telemetry = Telemetry(simulate=True)
    telemetry.sample()
    descriptor = telemetry.descriptor()
    assert len(descriptor.info.channels) == 5
    assert all(c.WhichOneof('options') is None for c in descriptor.info.channels.values())
    assert len(telemetry.status()) == 5
    with telemetry.lock:
        telemetry.updated = time.monotonic() - 6
    assert telemetry.status() == []
    telemetry.simulate = False
    def fail():
        raise subprocess.TimeoutExpired('nvidia-smi', 2)
    monkeypatch.setattr('lianli_lcd.telemetry.nvidia_query', fail)
    telemetry.sample()
    assert not any(s.id.startswith(('gpu_', 'vram_')) for s in telemetry.status())
    telemetry.close()

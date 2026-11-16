"""Read-only system telemetry, sampled once per second outside gRPC handlers."""

import csv
import hashlib
import math
from pathlib import Path
import subprocess
import threading
import time

from coolercontrol.models.v1 import device_pb2, status_pb2
from .openai_limits import DEFAULT_CHANNEL, DEFAULT_LIMITS_FILE, cached_limits

DEVICE_ID = "tl-system-metrics"


def cpu_counters(text):
    fields = next(line.split()[1:] for line in text.splitlines() if line.startswith("cpu "))
    values = [int(value) for value in fields[:8]]  # guest counters already included in user/nice
    if len(values) < 4 or any(value < 0 for value in values):
        raise ValueError("Invalid /proc/stat CPU counters")
    return sum(values), values[3] + (values[4] if len(values) > 4 else 0)


def cpu_usage(previous, current):
    total, idle = current[0] - previous[0], current[1] - previous[1]
    if total <= 0 or not 0 <= idle <= total:
        return None
    return 100 * (total - idle) / total


def ram_usage(text):
    fields = {key: int(value.split()[0]) for key, value in
              (line.split(":", 1) for line in text.splitlines() if ":" in line)}
    total, available = fields["MemTotal"], fields["MemAvailable"]
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("Invalid memory counters")
    return 100 * (total - available) / total


def nvidia_query():
    result = subprocess.run([
        "nvidia-smi", "--query-gpu=uuid,name,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
    ], capture_output=True, text=True, timeout=2, check=True)
    return list(csv.reader(result.stdout.splitlines(), skipinitialspace=True))


def gpu_key(uuid):
    return hashlib.sha256(uuid.encode()).hexdigest()[:12]


def gpu_values(rows):
    values = {}
    for row in rows:
        if len(row) != 5:
            continue
        uuid, _, load, used, total = (v.strip() for v in row)
        key = gpu_key(uuid)
        try:
            load = float(load)
            if math.isfinite(load) and 0 <= load <= 100:
                values[f"gpu_{key}_load"] = load
        except ValueError:
            pass
        try:
            used, total = float(used), float(total)
            if math.isfinite(total) and total > 0 and 0 <= used <= total:
                values[f"vram_{key}_load"] = 100 * used / total
        except ValueError:
            pass
    return values


class Telemetry:
    def __init__(self, simulate=False, limits_file=DEFAULT_LIMITS_FILE):
        self.simulate = simulate
        self.previous = None
        self.values = {}
        self.updated = 0.0
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.labels = {"cpu_load": "CPU load", "ram_load": "RAM used"}
        self.limits_file = limits_file
        self.labels[DEFAULT_CHANNEL] = "Codex weekly left"
        if not simulate:
            self.labels.update({row["id"]: row["label"] for row in cached_limits(limits_file)})
        self.gpus = []
        if simulate:
            self.gpus = [["sim-gpu", "GPU", "42", "4096", "8192"]]
        else:
            try:
                self.previous = cpu_counters(Path("/proc/stat").read_text())
            except (OSError, ValueError, StopIteration):
                pass
            try:
                self.gpus = nvidia_query()
            except (OSError, subprocess.SubprocessError):
                pass  # A missing driver does not disable CPU/RAM or LCDs.
        for i, row in enumerate(self.gpus, 1):
            if len(row) == 5:
                key = gpu_key(row[0].strip())
                self.labels[f"gpu_{key}_load"] = f"GPU {i} load"
                self.labels[f"vram_{key}_load"] = f"GPU {i} VRAM used"

    def descriptor(self):
        device = device_pb2.Device(id=DEVICE_ID, uid_info=DEVICE_ID, name="Lian Li System Metrics")
        device.info.model = "CPU / RAM / GPU / VRAM / Codex weekly limits"
        for key, label in self.labels.items():
            device.info.channels[key].label = label  # telemetry only, no control options
        return device

    def sample(self):
        values = {}
        if self.simulate:
            values = {"cpu_load": 36.5, "ram_load": 62.5, DEFAULT_CHANNEL: 82.0,
                      **gpu_values(self.gpus)}
        else:
            try:
                current = cpu_counters(Path("/proc/stat").read_text())
                load = cpu_usage(self.previous, current) if self.previous else None
                self.previous = current
                if load is not None:
                    values["cpu_load"] = load
            except (OSError, ValueError, StopIteration):
                pass
            try:
                values["ram_load"] = ram_usage(Path("/proc/meminfo").read_text())
            except (OSError, ValueError, KeyError):
                pass
            if self.gpus:
                try:
                    values.update(gpu_values(nvidia_query()))
                except (OSError, subprocess.SubprocessError):
                    pass
            values.update({row["id"]: row["remaining_percent"]
                           for row in cached_limits(self.limits_file)})
        with self.lock:
            self.values = {key: value for key, value in values.items() if key in self.labels}
            self.updated = time.monotonic()

    def status(self):
        with self.lock:
            values = self.values.copy() if time.monotonic() - self.updated < 5 else {}
        return [status_pb2.Status(id=key, speed=status_pb2.Status.FanSpeed(duty=value))
                for key, value in values.items()]

    def start(self):
        self.sample()
        def run():
            while not self.stop.wait(1):
                self.sample()
        self.thread = threading.Thread(target=run, name="system-metrics", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=3)

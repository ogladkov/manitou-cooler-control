"""Codex account quota reader. No prompts, token files or account mutations.

The exporter runs as the logged-in desktop user; the root USB plugin only reads
the small, sanitized snapshot. See https://learn.chatgpt.com/docs/app-server.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time

from . import __version__

DEFAULT_LIMITS_FILE = Path("/var/lib/cc-lianli-lcd-limits/usage.json")
MAX_AGE = 180
WEEK_MINUTES = 7 * 24 * 60
DEFAULT_CHANNEL = "openai_weekly_remaining"


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def channel_id(bucket):
    if bucket == "codex":
        return DEFAULT_CHANNEL
    return f"openai_weekly_{hashlib.sha256(bucket.encode()).hexdigest()[:12]}_remaining"


def weekly_limits(result):
    """Select by window length, never by primary/secondary position."""
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        fallback = result.get("rateLimits")
        buckets = {fallback.get("limitId") or "codex": fallback} if isinstance(fallback, dict) else {}
    rows = []
    for bucket, snapshot in buckets.items():
        if not isinstance(bucket, str) or not isinstance(snapshot, dict):
            continue
        windows = [snapshot.get("primary"), snapshot.get("secondary")]
        weekly = [w for w in windows if isinstance(w, dict)
                  and w.get("windowDurationMins") == WEEK_MINUTES]
        if len(weekly) != 1:
            continue  # ambiguous/missing window is unavailable, not 100% remaining
        window = weekly[0]
        used, reset = window.get("usedPercent"), window.get("resetsAt")
        if not numeric(used) or used < 0:
            continue
        if reset is not None and (not numeric(reset) or reset <= 0):
            continue
        label = snapshot.get("limitName") or ("Codex" if bucket == "codex" else bucket)
        if not isinstance(label, str):
            label = "Codex"
        label = "".join(c for c in label if c.isprintable())[:40]
        rows.append({"id": channel_id(bucket), "label": f"{label} weekly left",
                     "remaining_percent": max(0.0, 100.0 - used), "resets_at": reset})
    return rows


def read_account_limits(codex="codex", timeout=15):
    """Only initialize + account/rateLimits/read on a dedicated stdio connection."""
    process = subprocess.Popen([str(codex), "app-server", "--listen", "stdio://"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b""
    deadline = time.monotonic() + timeout

    def send(message):
        process.stdin.write(json.dumps(message).encode() + b"\n")
        process.stdin.flush()

    def receive(request_id):
        nonlocal buffer
        while time.monotonic() < deadline:
            if b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict) or "method" in message:
                    continue
                if message.get("id") == request_id:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise OSError("Codex account rate limits unavailable; check Codex login")
                    return message["result"]
            elif selector.select(min(.25, max(0, deadline - time.monotonic()))):
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise OSError("Codex app-server closed before returning rate limits")
                buffer += chunk
                if len(buffer) > 2 * 1024 * 1024:
                    raise OSError("Codex app-server response exceeded size limit")
        raise TimeoutError("Codex account rate limits timed out")

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "lianli_lcd_limits", "title": "Lian Li LCD Limits",
                           "version": __version__}}})
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read"})
        return receive(2)
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()


def export_limits(output, codex="codex"):
    output = Path(output)
    if os.geteuid() == 0:
        raise PermissionError("Run the quota exporter as the desktop user, not root")
    snapshot = {"version": 1, "fetched_at": time.time(), "status": "unavailable", "weekly": []}
    try:
        snapshot["weekly"] = weekly_limits(read_account_limits(codex))
        snapshot["status"] = "ok" if snapshot["weekly"] else "unavailable"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # No raw account responses, auth data or server error strings in the cache/log.
    snapshot["fetched_at"] = time.time()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".usage-", dir=output.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(snapshot, file, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return snapshot


def cached_limits(path=DEFAULT_LIMITS_FILE, now=None):
    now = time.time() if now is None else now
    try:
        with Path(path).open("rb") as file:
            raw = file.read(65537)
        if len(raw) > 65536:
            return []
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1 or data.get("status") != "ok":
            return []
        fetched = data.get("fetched_at")
        if not numeric(fetched) or not -5 <= now - fetched <= MAX_AGE:
            return []
        rows = data.get("weekly")
        if not isinstance(rows, list) or len(rows) > 32:
            return []
        valid = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            key, value, reset = row.get("id"), row.get("remaining_percent"), row.get("resets_at")
            if (not isinstance(key, str) or not key.startswith("openai_weekly_")
                    or not key.endswith("remaining") or len(key) > 80
                    or not numeric(value) or not 0 <= value <= 100
                    or (reset is not None and (not numeric(reset) or now >= reset))):
                continue
            label = row.get("label")
            if not isinstance(label, str):
                continue
            valid.append({"id": key, "label": "".join(c for c in label if c.isprintable())[:60],
                          "remaining_percent": value, "resets_at": reset})
        return valid
    except (OSError, ValueError, TypeError):
        return []

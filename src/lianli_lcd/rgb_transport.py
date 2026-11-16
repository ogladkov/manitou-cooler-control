"""Short, serialized transactions with the TL hub and V1 RF dongles."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import select
import time

import usb.core
import usb.util

from .rgb_protocol import rf_usb_packets, tl_packet, wireless_groups


RGB_IDS = {"wired": (0x0416, 0x7372), "tx": (0x0416, 0x8040), "rx": (0x0416, 0x8041)}


@dataclass(frozen=True)
class Endpoint:
    kind: str
    topology: str
    bus: int
    address: int
    hidraw: str | None = None


def discover_rgb(root=Path("/sys/bus/usb/devices"), hid_root=Path("/sys/class/hidraw")):
    found = []
    for path in sorted(root.iterdir()):
        try:
            pair = (int((path / "idVendor").read_text(), 16),
                    int((path / "idProduct").read_text(), 16))
            kind = next((k for k, value in RGB_IDS.items() if pair == value), None)
            if kind is None:
                continue
            hidraw = next(("/dev/" + h.name for h in sorted(hid_root.glob("hidraw*"))
                           if path.resolve() in (h / "device").resolve().parents), None)
            found.append(Endpoint(kind, path.name, int((path / "busnum").read_text()),
                                  int((path / "devnum").read_text()), hidraw))
        except (OSError, ValueError):
            continue
    return found


class Hid:
    def __init__(self, fd):
        self.fd = fd

    def exchange(self, packet, required=True):
        # Bounded flush: don't let unsolicited/stale responses satisfy this request.
        for _ in range(16):
            if not select.select([self.fd], [], [], 0)[0]:
                break
            os.read(self.fd, 64)
        if not select.select([], [self.fd], [], .5)[1]:
            raise TimeoutError("TL RGB write timed out")
        if os.write(self.fd, packet) != len(packet):
            raise OSError("Short TL RGB write")
        deadline = time.monotonic() + (.8 if required else .1)
        while time.monotonic() < deadline:
            if not select.select([self.fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            reply = os.read(self.fd, 64)
            if reply[:2] == packet[:2]:
                return reply
        if required:
            raise TimeoutError("No TL controller response")
        return b""  # Firmware does not always acknowledge lighting writes.


class Bulk:
    def __init__(self, device, pacer=None):
        self.device = device
        self.pacer = pacer if pacer is not None else [None]

    def write(self, packet):
        if self.device.write(0x01, packet, timeout=500) != len(packet):
            raise OSError("Short Wireless RGB USB write")

    def query(self, packet, size, timeout=150):
        for _ in range(16):
            try:
                self.device.read(0x81, size, timeout=5)
            except usb.core.USBTimeoutError:
                break
        self.write(packet)
        # V1 dongles can return a page as multiple 64-byte interrupt transfers.
        reply = bytearray()
        while len(reply) < size:
            try:
                chunk = self.device.read(0x81, min(64, size - len(reply)),
                                         timeout=timeout if not reply else 10)
            except usb.core.USBTimeoutError:
                if not reply:
                    raise
                break
            if not chunk:
                break
            reply.extend(chunk)
        return bytes(reply)

    def groups(self):
        reply = self.query(b"\x10\x01" + bytes(62), 512)
        groups = wireless_groups(reply)
        if reply[1] > 10:
            pages = (reply[1] + 9) // 10
            reply = self.query(bytes((0x10, pages)) + bytes(62), pages * 512)
            groups = wireless_groups(reply, pages)
        return groups

    def master(self, channels):
        for channel in dict.fromkeys([*channels, 8]):
            try:
                reply = self.query(bytes((0x11, channel)) + bytes(62), 64, timeout=500)
            except usb.core.USBTimeoutError:
                continue
            if (len(reply) >= 13 and reply[0] == 0x11 and any(reply[1:7])
                    and int.from_bytes(reply[7:11], "big") > 1):
                return reply[1:7]
        raise OSError("Wireless transmitter did not report a valid MAC")

    def send(self, group, packet):
        identity = (group.mac, packet[1])
        if self.pacer[0]:
            previous, sent_at = self.pacer[0]
            if previous != identity:
                gap = .010 if previous[1] == 0x20 else .005
                time.sleep(max(0, gap - (time.monotonic() - sent_at)))
        # Do not retry partially transmitted RF frames.
        chunks = rf_usb_packets(group, packet)
        try:
            for chunk in chunks:
                self.write(chunk)
                time.sleep(.001)
        finally:
            self.pacer[0] = (identity, time.monotonic())


class RgbTransport:
    def __init__(self, lock_dir=Path("/run/cc-lianli-lcd/devices")):
        self.lock_dir = lock_dir
        self.pacers = {}

    @contextmanager
    def open(self, endpoint):
        current = next((e for e in discover_rgb() if e.kind == endpoint.kind
                        and e.topology == endpoint.topology), None)
        if current is None:
            raise OSError(f"RGB controller disconnected: {endpoint.topology}")
        self.lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with ExitStack() as stack:
            lock = stack.enter_context((self.lock_dir / f"rgb-{current.topology}.lock").open("a"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if current.kind == "wired":
                if not current.hidraw:
                    raise OSError("TL controller hidraw node not found")
                fd = os.open(current.hidraw, os.O_RDWR | os.O_NONBLOCK)
                stack.callback(os.close, fd)
                yield Hid(fd)
                return
            vid, pid = RGB_IDS[current.kind]
            dev = usb.core.find(idVendor=vid, idProduct=pid,
                                bus=current.bus, address=current.address)
            if dev is None:
                raise OSError("Wireless RGB dongle disconnected")
            stack.callback(usb.util.dispose_resources, dev)
            # Never reset the device or change its configuration: this could
            # disturb another function. Only the RGB dongle's interface is claimed.
            if dev.get_active_configuration().bConfigurationValue != 1:
                raise OSError("Wireless dongle configuration is not active")
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
                stack.callback(dev.attach_kernel_driver, 0)
            usb.util.claim_interface(dev, 0)
            stack.callback(usb.util.release_interface, dev, 0)
            yield Bulk(dev, self.pacers.setdefault(current.topology, [None]))

    def wired_fans(self, endpoint):
        from .rgb_protocol import tl_fans
        with self.open(endpoint) as hid:
            return tl_fans(hid.exchange(tl_packet(0xA1)))

    @contextmanager
    def wireless(self, tx, rx):
        # Keep TX pacing state for the entire query/MB-sync/upload transaction.
        with self.open(tx) as transmitter, self.open(rx) as receiver:
            yield transmitter, receiver

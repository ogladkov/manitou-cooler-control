"""Exclusive, bounded I/O to LCD endpoints only."""

import fcntl
import os
from pathlib import Path
import select
import threading
import time

import usb.core
import usb.util

from .discovery import Screen, discover
from .protocol import (
    WIRELESS_ID, WirelessPackets, wired_frame, wired_settings, wireless_brightness,
)

# Wired panels in the same chain must not receive interleaved frames.
CHAIN_LOCK = threading.Lock()


class LcdTransport:
    def __init__(self, screen: Screen, lock_dir: Path):
        self.screen = screen
        self.fd = None
        self.usb = None
        self.detached = False
        self.claimed = False
        self.lock_file = None
        self.packets = WirelessPackets()
        self.lock_dir = lock_dir

    def open(self):
        if self.fd is not None or self.usb is not None:
            return
        # Resolve current bus address and hidraw path after resume/reconnect.
        current = next((s for s in discover() if s.id == self.screen.id), None)
        if current is None:
            raise OSError(f"LCD disconnected: {self.screen.id}")
        self.screen = current
        self.lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_file = (self.lock_dir / f"{current.id}.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if current.kind == "wired":
                if current.hidraw is None:
                    raise OSError("TL LCD hidraw node not found")
                self.fd = os.open(current.hidraw, os.O_RDWR | os.O_NONBLOCK)
            else:
                self.usb = usb.core.find(idVendor=WIRELESS_ID[0], idProduct=WIRELESS_ID[1],
                                         bus=current.bus, address=current.address)
                if self.usb is None:
                    raise OSError("Wireless LCD disconnected")
                if self.usb.is_kernel_driver_active(0):
                    self.usb.detach_kernel_driver(0)
                    self.detached = True
                if self.usb.get_active_configuration().bConfigurationValue != 1:
                    self.usb.set_configuration(1)
                usb.util.claim_interface(self.usb, 0)
                self.claimed = True
                self._exchange(self.packets.header(0x0F, bytes([120])))
                self._exchange(self.packets.header(0x0D, bytes([0])))
        except Exception:
            self.close()
            raise

    def _hid_write(self, packet: bytes):
        if not select.select([], [self.fd], [], 0.5)[1]:
            raise TimeoutError("TL LCD write timed out")
        if os.write(self.fd, packet) != len(packet):
            raise OSError("Short TL LCD HID write")

    def _exchange(self, packet: bytes):
        if self.usb.write(0x01, packet, timeout=500) != len(packet):
            raise OSError("Short Wireless LCD USB write")
        # The reference driver allows missing replies; writes still must succeed.
        try:
            self.usb.read(0x81, 512, timeout=100)
        except usb.core.USBTimeoutError:
            pass

    def settings(self, brightness: int):
        self.open()
        if self.screen.kind == "wired":
            with CHAIN_LOCK:
                # Discard stale acknowledgements before issuing settings.
                for _ in range(32):
                    if not select.select([self.fd], [], [], 0)[0]:
                        break
                    os.read(self.fd, 64)
                self._hid_write(wired_settings(brightness))
                if not select.select([self.fd], [], [], 0.5)[0]:
                    raise TimeoutError("TL LCD settings acknowledgement timed out")
                ack = os.read(self.fd, 64)
                if len(ack) < 2 or ack[:2] != b"\x02\x40":
                    raise OSError("Invalid TL LCD settings acknowledgement")
        else:
            self._exchange(self.packets.header(0x0E, bytes([wireless_brightness(brightness)])))

    def frame(self, jpeg: bytes):
        self.open()
        if self.screen.kind == "wired":
            with CHAIN_LOCK:
                deadline = time.monotonic() + 3
                for packet in wired_frame(jpeg):
                    if time.monotonic() > deadline:
                        raise TimeoutError("TL LCD frame transfer exceeded 3 seconds")
                    self._hid_write(packet)
        else:
            self._exchange(self.packets.frame(jpeg))

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.usb is not None:
            try:
                if self.claimed:
                    usb.util.release_interface(self.usb, 0)
                if self.detached:
                    self.usb.attach_kernel_driver(0)
            except usb.core.USBError:
                pass  # already disconnected
            finally:
                usb.util.dispose_resources(self.usb)
                self.usb = None
                self.claimed = self.detached = False
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None


class SimulatedTransport:
    """Exercise both wire encoders without opening USB devices."""

    def __init__(self, screen: Screen):
        self.screen = screen
        self.packets = WirelessPackets()
        self.frames = 0
        self.last_frame = None
        self.brightness = None

    def settings(self, brightness):
        self.brightness = brightness

    def frame(self, jpeg):
        if self.screen.kind == "wired":
            list(wired_frame(jpeg))
        else:
            self.packets.frame(jpeg)
        self.last_frame = jpeg
        self.frames += 1

    def close(self):
        pass

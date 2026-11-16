"""Native CoolerControl RGB devices; LCD identifiers remain unchanged."""

from dataclasses import dataclass, field
import logging
import threading
import time

from coolercontrol.models.v1 import device_pb2, status_pb2

from . import __version__
from .aura import AuraDevice
from .rgb_protocol import (
    MODES, SPEEDS, WIRELESS_MODES, WirelessGroup, disable_mb_rgb, tl_lighting,
    validate, wireless_upload, tl_packet, tl_rpms,
)
from .rgb_transport import Endpoint, RgbTransport, discover_rgb


@dataclass
class RgbDevice:
    id: str
    name: str
    endpoint: Endpoint
    ports: dict = field(default_factory=dict)
    group: WirelessGroup | None = None
    error: str | None = None

    @property
    def channels(self):
        if self.group:
            return {"rgb": f"RGB · {self.group.fans} fans"}
        return {f"port-{p + 1}": f"RGB · Port {p + 1} · {len(fans)} fan(s)"
                for p, fans in self.ports.items()}

    def descriptor(self):
        device = device_pb2.Device(id=self.id, uid_info=self.id, name=self.name)
        device.info.model = self.name
        device.info.driver_info.name = "coolercontrol-lianli-lcd"
        device.info.driver_info.version = __version__
        device.info.driver_info.locations.append(f"/sys/bus/usb/devices/{self.endpoint.topology}")
        device.info.lighting_speeds.extend(SPEEDS)
        for key, label in self.rpm_channels.items():
            device.info.channels[key].label = label
        for key, label in self.channels.items():
            channel = device.info.channels[key]
            channel.label = label
            for name, title, low, high, speed, direction, _ in (
                    WIRELESS_MODES if self.group else MODES):
                channel.lighting_modes.lighting_mode.add(
                    name=name, frontend_name=title, min_colors=low, max_colors=high,
                    speed_enabled=speed, backward_enabled=direction)
        return device

    @property
    def rpm_channels(self):
        if self.group:
            return {f"fan-{i + 1}": f"Fan {i + 1} RPM" for i in range(self.group.fans)}
        return {f"port-{p + 1}-fan-{f + 1}": f"Port {p + 1} Fan {f + 1} RPM"
                for p, fans in self.ports.items() for f in fans}


class Lighting:
    def __init__(self, transport=None, simulate=False):
        self.transport = transport or RgbTransport()
        self.simulate = simulate
        self.devices = {}
        self.discovery_errors = []
        self.tx = self.rx = None
        self.lock = threading.Lock()
        self.last_packets = {}

    def discover(self, endpoints=None):
        if self.simulate:
            wired = Endpoint("wired", "sim-hub", 0, 0)
            self.tx = Endpoint("tx", "sim-tx", 0, 0)
            self.rx = Endpoint("rx", "sim-rx", 0, 0)
            device = RgbDevice("tl-rgb-wired-sim-hub", "Lian Li TL RGB (sim-hub)",
                               wired, {0: (0,)})
            self.devices[device.id] = device
            for index in (1, 2):
                group = WirelessGroup(bytes((index,)) * 6, bytes((9,)) * 6,
                                      8, index, 3, bytes(4), False, 0, index - 1)
                self._add_group(group)
            return
        endpoints = discover_rgb() if endpoints is None else endpoints
        for endpoint in endpoints:
            if endpoint.kind == "aura":
                try:
                    with self.transport.open(endpoint) as hid:
                        device = AuraDevice(endpoint, hid.layout())
                    self.devices[device.id] = device
                except Exception as exc:
                    self._discovery_error(f"ASUS Aura {endpoint.topology}: {exc}")
                continue
            if endpoint.kind != "wired":
                continue
            try:
                ports = self.transport.wired_fans(endpoint)
                if ports:
                    device = RgbDevice(f"tl-rgb-wired-{endpoint.topology}",
                                       f"Lian Li TL RGB ({endpoint.topology})", endpoint, ports)
                    self.devices[device.id] = device
            except Exception as exc:
                self._discovery_error(f"TL RGB {endpoint.topology}: {exc}")
        txs = [e for e in endpoints if e.kind == "tx"]
        rxs = [e for e in endpoints if e.kind == "rx"]
        if not txs and not rxs:
            return
        if len(txs) != 1 or len(rxs) != 1:
            self._discovery_error("RGB requires one V1 TX/RX pair; ambiguous or missing dongles")
            return
        self.tx, self.rx = txs[0], rxs[0]
        try:
            with self.transport.wireless(self.tx, self.rx) as (tx, rx):
                first = rx.groups()
                master = tx.master(g.channel for g in first)
                # Require repeated matching identity/address records. Never repair
                # a suspicious RF address by rebinding the fans.
                previous = {g.mac: self._identity(g) for g in first}
                for _ in range(2):
                    time.sleep(.05)
                    groups = rx.groups()
                    current = {g.mac: self._identity(g) for g in groups}
                    previous = {mac: identity for mac, identity in previous.items()
                                if current.get(mac) == identity}
                owned = [g for g in groups if g.master == master and g.mac in previous]
                addresses = [(g.channel, g.rx) for g in owned]
                for group in owned:
                    if addresses.count((group.channel, group.rx)) == 1:
                        self._add_group(group)
                if not any(isinstance(d, RgbDevice) and d.group for d in self.devices.values()):
                    self._discovery_error("No stable, bound TL LCD Wireless RGB groups found")
        except Exception as exc:
            self._discovery_error(f"Wireless RGB: {exc}")

    @staticmethod
    def _identity(group):
        return group.master, group.channel, group.rx, group.fans

    def _discovery_error(self, message):
        self.discovery_errors.append(message)
        logging.warning(message)

    def _add_group(self, group):
        self.devices[group.id] = RgbDevice(
            group.id, f"Lian Li TL LCD Wireless RGB ({group.mac.hex(':')})", self.tx, group=group)

    def apply(self, device, channel, setting):
        if isinstance(device, AuraDevice):
            return self._aura(device, channel, setting)
        # Validate before any USB access, even on disconnected hardware.
        validate(setting, wireless=device.group is not None)
        if channel not in device.channels:
            raise ValueError("Unknown RGB channel")
        with self.lock:
            try:
                if device.group:
                    self._wireless(device, setting)
                else:
                    port = int(channel.removeprefix("port-")) - 1
                    packets = [tl_lighting(port, fan, setting) for fan in device.ports[port]]
                    if self.simulate:
                        self.last_packets[(device.id, channel)] = packets
                    else:
                        # Recheck topology/fan presence before addressing a port.
                        if self.transport.wired_fans(device.endpoint).get(port) != device.ports[port]:
                            raise OSError("TL port layout changed; restart the plugin")
                        with self.transport.open(device.endpoint) as hid:
                            for packet in packets:
                                hid.exchange(packet, required=False)
                device.error = None
            except Exception as exc:
                device.error = str(exc)
                raise

    def _aura(self, device, channel, setting):
        packets = device.packets(channel, setting)
        with self.lock:
            try:
                if self.simulate:
                    self.last_packets[(device.id, channel)] = packets
                else:
                    with self.transport.open(device.endpoint) as hid:
                        if hid.layout() != device.layout:
                            raise OSError("ASUS Aura layout changed; restart the plugin")
                        for packet in packets:
                            hid.write(packet)
                device.error = None
            except Exception as exc:
                device.error = str(exc)
                raise

    def _wireless(self, device, setting):
        if self.simulate:
            _, packets = wireless_upload(device.group, setting)
            self.last_packets[(device.id, "rgb")] = packets
            return
        with self.transport.wireless(self.tx, self.rx) as (tx, rx):
            def current():
                groups = rx.groups()
                group = next((g for g in groups if g.mac == device.group.mac), None)
                if group is None or self._identity(group) != self._identity(device.group):
                    raise OSError("Wireless group disconnected or binding/layout changed")
                if sum((g.channel, g.rx) == (group.channel, group.rx) for g in groups) != 1:
                    raise OSError("Wireless group has an ambiguous RF address")
                return group

            group = current()
            if tx.master([group.channel]) != group.master:
                raise OSError("Wireless group belongs to another transmitter")
            if group.mb_sync:
                tx.send(group, disable_mb_rgb(group))
                expected_sequence = (group.sequence + 1) & 255
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    time.sleep(.1)
                    group = current()
                    if not group.mb_sync and group.sequence == expected_sequence:
                        break
                else:
                    raise TimeoutError("Motherboard RGB sync did not switch off")
            effect, packets = wireless_upload(group, setting)
            deadline = time.monotonic() + 3
            for index, packet in enumerate(packets):
                for _ in range(2 if index == 0 else 1):
                    if time.monotonic() > deadline:
                        raise TimeoutError("Wireless RGB upload timed out")
                    tx.send(group, packet)
                    if index == 0:
                        time.sleep(.002)
            # USB success alone is not RF success. Read the device's running effect.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                time.sleep(.1)
                if current().effect == effect:
                    return
            raise TimeoutError("Wireless fan did not acknowledge the RGB effect")

    def status(self, device):
        if isinstance(device, AuraDevice):
            return []  # Aura exposes no fan-speed or lighting readback telemetry.
        with self.lock:
            if self.simulate:
                values = {key: 1200 + i * 100 for i, key in enumerate(device.rpm_channels)}
            elif device.group:
                # RX-only query. It neither transmits RF nor changes PWM/binding.
                with self.transport.open(self.rx) as receiver:
                    group = next((g for g in receiver.groups() if g.mac == device.group.mac), None)
                    if group is None or self._identity(group) != self._identity(device.group):
                        raise OSError("Wireless RPM source disconnected or changed")
                    values = {f"fan-{i + 1}": rpm for i, rpm in enumerate(group.rpms)}
            else:
                with self.transport.open(device.endpoint) as hid:
                    values = tl_rpms(hid.exchange(tl_packet(0xA1)))
            return [status_pb2.Status(id=key, speed=status_pb2.Status.FanSpeed(rpm=rpm))
                    for key, rpm in values.items() if key in device.rpm_channels]

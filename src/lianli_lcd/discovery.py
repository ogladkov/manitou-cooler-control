"""Read-only sysfs discovery. USB topology IDs survive reboot/address changes."""

from dataclasses import asdict, dataclass
from pathlib import Path

from .protocol import WIRED_ID, WIRELESS_ID


@dataclass(frozen=True)
class Screen:
    id: str
    kind: str
    topology: str
    bus: int
    address: int
    serial: str
    hidraw: str | None = None

    def as_dict(self):
        return asdict(self)


def discover(root: Path = Path("/sys/bus/usb/devices"),
             hid_root: Path = Path("/sys/class/hidraw")) -> list[Screen]:
    screens = []
    for path in sorted(root.iterdir()):
        try:
            pair = (int((path / "idVendor").read_text(), 16),
                    int((path / "idProduct").read_text(), 16))
            if pair not in (WIRED_ID, WIRELESS_ID):
                continue
            kind = "wired" if pair == WIRED_ID else "wireless"
            hidraw = None
            if kind == "wired":
                candidates = sorted(hid_root.glob("hidraw*"))
                for candidate in candidates:
                    if path.resolve() in (candidate / "device").resolve().parents:
                        hidraw = "/dev/" + candidate.name
                        break
            serial_path = path / "serial"
            screens.append(Screen(
                id=f"tl-{kind}-{path.name}", kind=kind, topology=path.name,
                bus=int((path / "busnum").read_text()),
                address=int((path / "devnum").read_text()),
                serial=serial_path.read_text().strip() if serial_path.exists() else "",
                hidraw=hidraw,
            ))
        except (OSError, ValueError):
            continue  # device disconnected during enumeration
    return screens


def simulated_screens() -> list[Screen]:
    return [Screen(f"sim-{i}", "wired" if i == 1 else "wireless", f"sim-{i}",
                   0, i, "simulation") for i in range(1, 8)]

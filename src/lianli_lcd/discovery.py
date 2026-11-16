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

    tty: str | None = None
    width: int = 400
    height: int = 400

    def as_dict(self):
        return asdict(self)


def discover(root: Path = Path("/sys/bus/usb/devices"),
             hid_root: Path = Path("/sys/class/hidraw"),
             tty_root: Path = Path("/sys/class/tty")) -> list[Screen]:
    screens = []
    for path in sorted(root.iterdir()):
        try:
            pair = (int((path / "idVendor").read_text(), 16),
                    int((path / "idProduct").read_text(), 16))
            if pair == (0x18D1, 0x2D04):
                # This VID/PID is shared with Android accessories. Require the
                # exact TRYX model AND its CDC endpoint; never claim phones.
                if ((path / "manufacturer").read_text().strip() != "rockchip"
                        or (path / "product").read_text().strip() != "cm01_se"):
                    continue
                ports = [p for p in sorted(tty_root.glob("ttyACM*"))
                         if path.resolve() in (p / "device").resolve().parents]
                if not ports:
                    continue
                screens.append(Screen(
                    id=f"tryx-panorama-{path.name}", kind="tryx", topology=path.name,
                    bus=int((path / "busnum").read_text()),
                    address=int((path / "devnum").read_text()),
                    serial=(path / "serial").read_text().strip(),
                    tty="/dev/" + ports[0].name, width=2240, height=1080,
                ))
                continue
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


def simulated_screens(include_tryx=False) -> list[Screen]:
    screens = [Screen(f"sim-{i}", "wired" if i == 1 else "wireless", f"sim-{i}",
                   0, i, "simulation") for i in range(1, 8)]
    if include_tryx:
        screens.append(Screen("sim-tryx", "tryx", "sim-tryx", 0, 8, "simulation",
                              tty="/dev/sim-tryx", width=2240, height=1080))
    return screens

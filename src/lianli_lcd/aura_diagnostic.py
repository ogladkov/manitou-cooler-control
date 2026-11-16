"""Interactive, volatile direct-color test for the reported AR60 layout.

Uses OpenRGB's SetMode(DIRECT) and AuraUSBController::SendDirect framing.
Does not install anything, save controller flash or modify cooling settings.
"""

import argparse
from dataclasses import asdict
import json

from .aura import Layout, packet
from .rgb_transport import RgbTransport, discover_rgb


COLORS = ((80, 0, 0), (0, 80, 0), (0, 0, 80), (60, 60, 60))


def direct_packets(layout, offset, led_count=120):
    # This is a diagnostic for a known layout, not a heuristic to apply to
    # every ASUS board. Four streams may include the board's Polymo lighting.
    if layout != Layout("AULA3-AR60-0112", 4, 0):
        raise ValueError("Test requires AULA3-AR60-0112, four streams, zero onboard slots")
    if offset not in (0, 1) or not 1 <= led_count <= 120:
        raise ValueError("Invalid direct-color test parameters")
    gen1 = packet(0x52)
    gen1[2:5] = (0x53, 0, 1)
    packets = [bytes(gen1)]
    for channel, color in enumerate(COLORS):
        mode = packet(0x35)
        mode[2], mode[5] = channel + offset, 0xFF
        packets.append(bytes(mode))
        for start in range(0, led_count, 20):
            count = min(20, led_count - start)
            data = packet(0x40)
            data[2] = channel | (0x80 if start + count == led_count else 0)
            data[3:5] = (start, count)
            data[5:5 + count * 3] = bytes(color) * count
            packets.append(bytes(data))
    return packets


def inspect(hid):
    version = hid.query(0x82, 0x02)
    config = hid.query(0xB0, 0x30)
    layout = Layout.parse(version, config)
    return layout, {"layout": asdict(layout), "firmware_reply": version.hex(),
                    "config_reply": config.hex()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true",
                        help="Interactively write two direct-color patterns; otherwise query only")
    args = parser.parse_args()
    endpoints = [e for e in discover_rgb() if e.kind == "aura"]
    if len(endpoints) != 1:
        parser.error("Expected exactly one ASUS Aura USB controller")
    endpoint = endpoints[0]
    transport = RgbTransport()
    with transport.open(endpoint) as hid:
        layout, report = inspect(hid)
    print(json.dumps(report, indent=2), flush=True)
    if not args.test:
        return
    # Validate all experiments before any lighting write.
    experiments = [direct_packets(layout, offset) for offset in (0, 1)]
    print("Тест затронет четыре потока Aura, включая возможную подсветку платы.\n"
          "Цвета потоков 0–3: красный, зелёный, синий, белый.\n"
          "Не меняйте настройки подсветки в CoolerControl во время теста.\n"
          "Цвета останутся до следующего применения настроек; запись в память не выполняется.",
          flush=True)
    results = []
    try:
        for offset, packets in enumerate(experiments):
            input(f"Enter — начать вариант {offset + 1} (смещение режима {offset}); Ctrl+C — выход: ")
            with transport.open(endpoint) as hid:
                if hid.layout() != layout:
                    raise OSError("Aura layout changed; test stopped")
                for data in packets:
                    hid.write(data)
            observation = input("Что загорелось и каким цветом (вентиляторы TRYX / плата)? ")
            results.append({"effect_offset": offset, "observation": observation})
    except (EOFError, KeyboardInterrupt):
        print("\nТест остановлен.")
    finally:
        print(json.dumps({"layout": asdict(layout), "results": results}, ensure_ascii=False, indent=2))
        print("Для возврата сохранённых настроек: sudo systemctl restart coolercontrold")


if __name__ == "__main__":
    main()

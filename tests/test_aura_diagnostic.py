import pytest

from lianli_lcd.aura import Layout
from lianli_lcd.aura_diagnostic import COLORS, direct_packets, inspect


AR60 = Layout("AULA3-AR60-0112", 4, 0)


@pytest.mark.parametrize("offset", [0, 1])
@pytest.mark.parametrize("count", [1, 20, 21, 120])
def test_direct_stream_reconstruction(offset, count):
    packets = direct_packets(AR60, offset, count)
    assert packets.pop(0) == b"\xec\x52\x53\0\1" + bytes(60)
    streams = {channel: [] for channel in range(4)}
    modes, applies = [], []
    for data in packets:
        assert len(data) == 65 and data[0] == 0xEC
        if data[1] == 0x35:
            assert data[3:6] == b"\0\0\xff"
            modes.append(data[2])
        else:
            assert data[1] == 0x40  # Never commit or send persistent configuration.
            channel = data[2] & 0x7F
            assert data[3] == len(streams[channel]) // 3
            assert 1 <= data[4] <= 20
            streams[channel].extend(data[5:5 + data[4] * 3])
            if data[2] & 0x80:
                applies.append(channel)
                assert len(streams[channel]) == count * 3
    assert modes == list(range(offset, offset + 4))
    assert applies == [0, 1, 2, 3]
    for channel, color in enumerate(COLORS):
        assert bytes(streams[channel]) == bytes(color) * count


@pytest.mark.parametrize("layout,offset,count", [
    (Layout("AULA3-AR32-0201", 4, 0), 0, 120),
    (Layout("AULA3-AR60-0112", 3, 0), 0, 120),
    (AR60, 2, 120), (AR60, 0, 0), (AR60, 0, 121),
])
def test_diagnostic_rejects_unknown_layout_and_parameters(layout, offset, count):
    with pytest.raises(ValueError):
        direct_packets(layout, offset, count)


def test_inspection_only_uses_read_queries():
    requests = []
    class Hid:
        def query(self, command, response):
            requests.append((command, response))
            if command == 0x82:
                return b"\xec\x02AULA3-AR60-0112" + bytes(49)
            data = bytearray(65)
            data[:2] = b"\xec\x30"
            data[6] = 4
            return bytes(data)
    layout, report = inspect(Hid())
    assert layout == AR60
    assert requests == [(0x82, 0x02), (0xB0, 0x30)]
    assert report["config_reply"].startswith("ec30")

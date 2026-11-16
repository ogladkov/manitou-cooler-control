"""TRYX legacy Android/CDC protocol; see THIRD_PARTY.md (reed-tpse, MIT)."""

import json

MARKER = 0x5A
MAX_FRAME = 65535


def encode_command(command, content=None, sequence=1):
    if command not in {"conn", "brightness", "waterBlockScreenId"}:
        raise ValueError("Unsupported TRYX display command")
    body = b"" if content is None else json.dumps(
        content, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    message = (f"POST {command} 1\r\nContentType=json\r\nContentLength={len(body)}"
               f"\r\nAckNumber={sequence}\r\n\r\n").encode() + body
    if len(message) + 5 > MAX_FRAME:
        raise ValueError("TRYX command exceeds frame limit")
    payload = (len(message) + 5).to_bytes(2, "big") + message
    payload += bytes([sum(payload) & 255])
    payload = payload.replace(b"\x5b", b"\x5b\x02").replace(b"\x5a", b"\x5b\x01")
    return b"\x5a" + payload + b"\x5a"


def decode_response(frame):
    if len(frame) < 5 or frame[0] != MARKER or frame[-1] != MARKER:
        raise ValueError("Invalid TRYX frame markers")
    escaped = frame[1:-1]
    payload = bytearray()
    index = 0
    while index < len(escaped):
        value = escaped[index]
        if value == 0x5B:
            index += 1
            if index >= len(escaped) or escaped[index] not in (1, 2):
                raise ValueError("Invalid TRYX escape")
            value = 0x59 + escaped[index]
        payload.append(value)
        index += 1
    if len(payload) < 3 or len(payload) > MAX_FRAME:
        raise ValueError("Invalid TRYX response length")
    if int.from_bytes(payload[:2], "big") != len(payload) + 2:
        raise ValueError("TRYX response length mismatch")
    if sum(payload[:-1]) & 255 != payload[-1]:
        raise ValueError("TRYX response checksum mismatch")
    headers, separator, body = bytes(payload[2:-1]).partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("TRYX response headers missing")
    # Legacy firmware returns literal backslash-r/backslash-n within headers.
    lines = headers.replace(b"\\r\\n", b"\r\n").split(b"\r\n")
    first = lines[0].split()
    if len(first) != 2 or first[0] != b"1" or not first[1].isdigit():
        raise ValueError("Invalid TRYX response status")
    fields = dict(line.split(b"=", 1) for line in lines[1:] if b"=" in line)
    if int(fields.get(b"ContentLength", -1)) != len(body):
        raise ValueError("TRYX response content length mismatch")
    if first[1] != b"200":
        raise OSError(f"TRYX display command rejected ({first[1].decode()})")
    result = json.loads(body) if body else {}
    if not isinstance(result, dict):
        raise ValueError("TRYX response must be a JSON object")
    return result


def screen_config(filename):
    return {"Type": "Custom", "id": "Customization", "screenMode": "Full Screen",
            "ratio": "2:1", "playMode": "single", "media": [filename],
            "settings": {"position": "Top", "color": "#FFFFFF", "align": "Center",
                         "badges": [], "filter": {"value": "", "opacity": 0}},
            "sysinfoDisplay": []}

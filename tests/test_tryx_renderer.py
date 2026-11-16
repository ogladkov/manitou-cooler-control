"""Exercise the frame stream over real pipes with a disposable protocol peer."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lianli_lcd.tryx_renderer import TryxRenderer

PEER = '''
import hashlib,json,struct,sys
from pathlib import Path
out=Path(sys.argv[1]);events=[]
sys.stdout.buffer.write(b'MTX1');sys.stdout.buffer.flush()
while True:
 header=sys.stdin.buffer.read(4)
 if not header: break
 n=struct.unpack('>i',header)[0]
 data=sys.stdin.buffer.read(n) if n>0 else b''
 events.append([n,hashlib.sha256(data).hexdigest()])
 sys.stdout.buffer.write(b'\\1');sys.stdout.buffer.flush()
out.write_text(json.dumps(events))
'''


def test_renderer_stream_handshake_large_frames_hide_ping_and_eof(tmp_path, monkeypatch):
    artifact = tmp_path / 'data' / 'tryx-frame-server.dex'
    artifact.parent.mkdir()
    artifact.write_bytes(b'test dex')
    monkeypatch.setattr('lianli_lcd.tryx_renderer.files', lambda _: tmp_path)
    pushes, launches = [], []
    output = tmp_path / 'events.json'
    def command(*args):
        launches.append(args)
        return [sys.executable, '-c', PEER, str(output)]
    transport = SimpleNamespace(_adb=lambda *a: pushes.append(a), adb_command=command)
    renderer = TryxRenderer(transport)
    payload = b'JPEG fixture' * 100_000  # exceeds pipe capacity; requires partial writes
    try:
        renderer.frame(payload)
        renderer.frame(b'next')
        renderer.ping()
        renderer.hide()
        process = renderer.process
    finally:
        renderer.close()
    assert process.returncode == 0
    assert [n for n, digest in json.loads(output.read_text())] == [len(payload), 4, 0, -1]
    assert len(pushes) == len(launches) == 1
    assert pushes[0][0] == 'push' and pushes[0][1] == str(artifact)
    assert pushes[0][2].startswith('/data/local/tmp/manitou-tryx-')
    assert launches[0][:2] == ('shell', '-T')
    assert renderer.process is None
    renderer.close()  # idempotent


def test_renderer_missing_artifact_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr('lianli_lcd.tryx_renderer.files', lambda _: tmp_path)
    renderer = TryxRenderer(None)
    with pytest.raises(OSError, match='build-tryx-renderer'):
        renderer.frame(b'jpeg')
    assert renderer.process is None


def test_renderer_failed_start_cleans_up_child(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'tryx-frame-server.dex').write_bytes(b'test')
    monkeypatch.setattr('lianli_lcd.tryx_renderer.files', lambda _: tmp_path)
    transport = SimpleNamespace(_adb=lambda *a: None, adb_command=lambda *a: [
        sys.executable, '-c', "import sys;sys.stdout.write('BAD!');sys.stdout.flush()"])
    renderer = TryxRenderer(transport)
    with pytest.raises(OSError, match='protocol'):
        renderer.frame(b'jpeg')
    assert renderer.process is None


def test_dead_renderer_does_not_acknowledge_frame(monkeypatch):
    renderer = TryxRenderer(None)
    renderer.process = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, bufsize=0)
    renderer.process.wait(timeout=5)
    with pytest.raises(OSError):
        renderer.ping()
    renderer.close()

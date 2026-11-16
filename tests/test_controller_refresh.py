"""Reproduce long gaps between CoolerControl updates without real USB or sleeps."""
from types import SimpleNamespace
import threading

import pytest

from coolercontrol.device_service.v1.lcd_pb2 import LcdSetting
from lianli_lcd import controller as module
from lianli_lcd.controller import Controller
from lianli_lcd.media import Frame


class ClockStop:
    def __init__(self, until):
        self.now = 0.0
        self.until = until

    def wait(self, seconds):
        self.now += seconds
        return self.now > self.until


@pytest.mark.parametrize('kind', ['wired', 'wireless'])
def test_long_gif_frames_refresh_without_advancing_animation_early(monkeypatch, kind):
    stop = ClockStop(7)
    calls = []
    transport = SimpleNamespace(screen=SimpleNamespace(kind=kind),
                                frame=lambda data: calls.append((stop.now, data)))
    controller = Controller(transport, [])
    controller.frames = [Frame(b'first', 3), Frame(b'second', 2)]
    controller.stop_event = stop
    monkeypatch.setattr(module.time, 'monotonic', lambda: stop.now)
    controller._animate()
    if kind == 'wired':
        assert calls == [(1, b'first'), (2, b'first'), (3, b'second'), (4, b'second'),
                         (5, b'first'), (6, b'first'), (7, b'first')]
    else:
        assert calls == [(3, b'second'), (5, b'first')]


def test_static_wired_frame_repeats_during_minutes_without_source_changes(monkeypatch):
    stop = ClockStop(180)
    calls = []
    transport = SimpleNamespace(screen=SimpleNamespace(kind='wired'),
                                frame=lambda data: calls.append((stop.now, data)))
    controller = Controller(transport, [])
    controller.frames = [Frame(b'unchanged-ring', .1)]
    controller.stop_event = stop
    monkeypatch.setattr(module.time, 'monotonic', lambda: stop.now)
    controller._animate()
    assert len(calls) == 180
    assert calls == [(i, b'unchanged-ring') for i in range(1, 181)]


def test_static_refresh_lifecycle_off_replacement_and_failure(monkeypatch):
    class Transport:
        screen = SimpleNamespace(kind='wired', id='wired-test')

        def __init__(self):
            self.calls = []
            self.brightness = None
            self.fail = False
            self.refreshed = threading.Event()
            self.closed = threading.Event()

        def settings(self, value):
            self.brightness = value

        def frame(self, data):
            if self.fail:
                raise OSError('disconnected')
            self.calls.append(data)
            if len(self.calls) >= 2:
                self.refreshed.set()

        def close(self):
            self.closed.set()

    monkeypatch.setattr(module, 'WIRED_REFRESH_SECONDS', .01)
    monkeypatch.setattr(module, 'decode_media', lambda source, _: [Frame(source, .1)])
    transport = Transport()
    controller = Controller(transport, [])
    controller.source = b'first'
    try:
        controller.apply(LcdSetting(mode='image', brightness=72))
        assert transport.refreshed.wait(1)
        first_worker = controller.worker
        controller.source = b'replacement'
        controller.apply(LcdSetting(mode='image'))
        assert not first_worker.is_alive()
        assert transport.calls[-1] == b'replacement'
        second_worker = controller.worker
        controller.apply(LcdSetting(mode='none'))
        assert transport.brightness == 0
        assert controller.worker is None and not second_worker.is_alive()
        controller.apply(LcdSetting(mode='image'))
        assert transport.brightness == 72
        transport.fail = True
        assert transport.closed.wait(1)
        controller.worker.join(1)
        assert controller.error == 'disconnected'
        transport.fail = False
        controller.apply(LcdSetting(mode='image'))
        assert controller.error is None
    finally:
        worker = controller.worker
        controller.close()
        assert worker is None or not worker.is_alive()


def test_wireless_static_image_does_not_start_refresh_worker():
    controller = Controller(SimpleNamespace(screen=SimpleNamespace(kind='wireless')), [])
    controller.frames = [Frame(b'static', .1)]
    controller._start()
    assert controller.worker is None

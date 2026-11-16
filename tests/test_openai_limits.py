import json
from pathlib import Path
import runpy
import stat
import sys
import time
from types import SimpleNamespace

import pytest

from lianli_lcd import openai_limits as limits
from lianli_lcd.telemetry import Telemetry


def window(used=18, minutes=10080, reset=2000):
    return dict(usedPercent=used, windowDurationMins=minutes, resetsAt=reset)


@pytest.mark.parametrize('slot', ['primary', 'secondary'])
def test_selects_week_by_duration_not_slot_and_supports_multiple_buckets(slot):
    snapshot = {slot: window(), 'limitName': 'Codex'}
    other = 'secondary' if slot == 'primary' else 'primary'
    snapshot[other] = window(75, 300)
    result = limits.weekly_limits({'rateLimitsByLimitId': {
        'codex': snapshot, 'review': {'primary': window(100), 'limitName': 'Review'},
        'five-hour-only': {'primary': window(10, 300)},
    }})
    assert [row['remaining_percent'] for row in result] == [82, 0]
    assert result[0]['id'] == limits.DEFAULT_CHANNEL
    assert result[1]['id'] == limits.channel_id('review')
    assert result[1]['label'] == 'Review weekly left'
    assert limits.weekly_limits({'rateLimits': snapshot}) == result[:1]


@pytest.mark.parametrize('used', [None, True, -1, '18', float('nan'), float('inf')])
def test_missing_or_invalid_used_is_not_a_full_quota(used):
    assert limits.weekly_limits({'rateLimits': {'primary': window(used)}}) == []


def test_no_guessed_weekly_window_and_overrun_clamps_to_empty():
    assert limits.weekly_limits({}) == []
    assert limits.weekly_limits({'rateLimits': {'primary': window(18, None)}}) == []
    assert limits.weekly_limits({'rateLimits': {'primary': window(), 'secondary': window()}}) == []
    assert limits.weekly_limits({'rateLimits': {'primary': window(105)}})[0]['remaining_percent'] == 0


def write_cache(path, fetched=1000, reset=2000, value=82):
    row = dict(id=limits.DEFAULT_CHANNEL, label='Codex weekly left',
               remaining_percent=value, resets_at=reset)
    path.write_text(json.dumps(dict(version=1, fetched_at=fetched, status='ok', weekly=[row])))


def test_cache_expires_and_never_guesses_reset_to_full(tmp_path):
    path = tmp_path / 'usage.json'
    write_cache(path, value=0)
    assert limits.cached_limits(path, now=1100)[0]['remaining_percent'] == 0
    assert limits.cached_limits(path, now=1181) == []
    assert limits.cached_limits(path, now=900) == []
    write_cache(path, reset=1100)
    assert limits.cached_limits(path, now=1100) == []
    assert limits.cached_limits(path, now=1099)[0]['remaining_percent'] == 82
    for raw in ['broken', '[]', '{"weekly":null}', ' ' * 65537]:
        path.write_text(raw)
        assert limits.cached_limits(path, now=1000) == []


def test_export_atomic_private_and_failed_read_clears_previous_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(limits.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(limits, 'read_account_limits', lambda _: {
        'rateLimits': {'primary': window(reset=None)}, 'accountSecret': 'must-not-be-written'})
    path = tmp_path / 'state' / 'usage.json'
    assert limits.export_limits(path)['status'] == 'ok'
    assert limits.cached_limits(path)[0]['remaining_percent'] == 82
    assert 'must-not-be-written' not in path.read_text()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]
    def fail(_):
        raise TimeoutError('must-not-be-written')
    monkeypatch.setattr(limits, 'read_account_limits', fail)
    assert limits.export_limits(path)['status'] == 'unavailable'
    assert limits.cached_limits(path) == []
    assert 'must-not-be-written' not in path.read_text()
    monkeypatch.setattr(limits.os, 'geteuid', lambda: 0)
    with pytest.raises(PermissionError):
        limits.export_limits(path)


def fake_codex(tmp_path, body):
    path = tmp_path / 'codex'
    path.write_text(f'#!{sys.executable}\n' + body)
    path.chmod(0o700)
    return path


def test_app_server_protocol_only_initializes_and_reads_quota(tmp_path):
    trace = tmp_path / 'methods.json'
    body = f'''import json, sys
assert sys.argv[1:] == ['app-server', '--listen', 'stdio://']
methods = []
for line in sys.stdin:
    request = json.loads(line)
    methods.append(request['method'])
    if request['method'] == 'initialize':
        print(json.dumps({{'id': 1, 'result': {{}}}}), flush=True)
    elif request['method'] == 'account/rateLimits/read':
        with open({str(trace)!r}, 'w') as file:
            json.dump(methods, file)
        print(json.dumps({{'method': 'account/rateLimits/updated', 'params': {{}}}}), flush=True)
        print(json.dumps({{'id': 2, 'result': {{'rateLimits': {{'primary': {window()!r}}}}}}}), flush=True)
'''
    assert limits.weekly_limits(limits.read_account_limits(fake_codex(tmp_path, body)))[0][
        'remaining_percent'] == 82
    assert json.loads(trace.read_text()) == ['initialize', 'initialized', 'account/rateLimits/read']


def test_app_server_timeout_is_bounded(tmp_path):
    path = fake_codex(tmp_path, 'import time\ntime.sleep(30)\n')
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        limits.read_account_limits(path, timeout=.1)
    assert time.monotonic() - start < 4


def test_telemetry_exposes_quota_as_read_only_and_drops_expired_cache(tmp_path, monkeypatch):
    path = tmp_path / 'usage.json'
    write_cache(path, fetched=time.time(), reset=time.time()+1000, value=0)
    monkeypatch.setattr('lianli_lcd.telemetry.nvidia_query', lambda: [])
    telemetry = Telemetry(limits_file=path)
    telemetry.sample()
    assert telemetry.descriptor().info.channels[limits.DEFAULT_CHANNEL].WhichOneof('options') is None
    assert next(s.speed.duty for s in telemetry.status() if s.id == limits.DEFAULT_CHANNEL) == 0
    write_cache(path, fetched=0)
    telemetry.sample()
    assert all(s.id != limits.DEFAULT_CHANNEL for s in telemetry.status())
    telemetry.close()


def test_systemd_reader_uses_desktop_identity_and_private_state():
    helper = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/install-openai-limits.py'))
    service, timer = helper['units'](SimpleNamespace(pw_uid=1000, pw_gid=1001, pw_dir='/home/test'),
                                      '/home/test/.local/bin/codex')
    assert 'User=1000\nGroup=1001' in service
    assert 'StateDirectoryMode=0700' in service
    assert 'CODEX_HOME=/home/test/.codex' in service
    assert 'export-openai-limits' in service
    assert 'OnUnitInactiveSec=60s' in timer
    assert helper['quote']('/home/a%/"b') == '"/home/a%%/\\"b"'
    with pytest.raises(ValueError):
        helper['quote']('injected\nExecStart=bad')

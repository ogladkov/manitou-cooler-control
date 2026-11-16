#!/usr/bin/env python3
"""Install a quota reader under the sudo caller's identity, never root's account."""
import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess

NETWORK_KEYS = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
NETWORK_FILE = Path(__file__).resolve().parents[1] / 'build/openai-limits-network.json'


def network_environment():
    if not NETWORK_FILE.exists():
        return {key: os.environ[key] for key in NETWORK_KEYS if key in os.environ}
    with NETWORK_FILE.open() as file:
        data = json.loads(file.read(65536))
    if not isinstance(data, dict) or any(key not in NETWORK_KEYS or not isinstance(value, str)
                                         for key, value in data.items()):
        raise ValueError('Invalid quota reader network configuration')
    return data


def quote(value):
    if any(c in str(value) for c in '\r\n\x00'):
        raise ValueError('Invalid systemd argument')
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def units(account, codex):
    home = Path(account.pw_dir)
    service = f'''[Unit]
Description=Read Codex weekly quota for Lian Li LCD
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User={account.pw_uid}
Group={account.pw_gid}
Environment={quote('HOME=' + str(home))}
Environment={quote('CODEX_HOME=' + str(home / '.codex'))}
EnvironmentFile=-/opt/cc-lianli-lcd/openai-limits.env
WorkingDirectory={str(home).replace('%', '%%')}
StateDirectory=cc-lianli-lcd-limits
StateDirectoryMode=0700
UMask=0077
ExecStart=/opt/cc-lianli-lcd/venv/bin/cc-lianli-lcd export-openai-limits --codex {quote(codex)} --output /var/lib/cc-lianli-lcd-limits/usage.json
TimeoutStartSec=25
KillMode=control-group
NoNewPrivileges=yes
StandardOutput=null
'''
    timer = '''[Unit]
Description=Refresh Codex weekly quota every minute

[Timer]
OnBootSec=20s
OnUnitInactiveSec=60s
AccuracySec=5s
Unit=cc-lianli-openai-limits.service

[Install]
WantedBy=timers.target
'''
    return service, timer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--prepare', action='store_true',
                        help='Save only proxy variables from the desktop shell for installation')
    parser.add_argument('--user', default=os.environ.get('SUDO_USER'))
    args = parser.parse_args()
    if args.prepare:
        if os.geteuid() == 0:
            parser.error('Run --prepare without sudo, from the desktop shell')
        NETWORK_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(NETWORK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as file:
            os.fchmod(file.fileno(), 0o600)
            json.dump({key: os.environ[key] for key in NETWORK_KEYS if key in os.environ}, file)
        print('Saved quota reader proxy configuration (no account credentials)')
        return
    if not args.user:
        parser.error('Run sudo from the desktop account, or specify --user NAME')
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        parser.error('The quota reader must run as the desktop user, not root')
    codex = next((path for path in [Path(account.pw_dir) / '.local/bin/codex',
                                   Path('/usr/local/bin/codex'), Path('/usr/bin/codex')]
                  if path.is_file() and os.access(path, os.X_OK)), None)
    if codex is None:
        parser.error('Codex CLI not found; install it and log in as the desktop user first')
    service, timer = units(account, codex)
    network = network_environment()
    # EnvironmentFile does not expand systemd % specifiers or shell variables.
    environment = ''.join(f'{key}={quote(value).replace("%%", "%")}\n'
                          for key, value in sorted(network.items()))
    if args.check:
        print(f'Quota reader: user={account.pw_name}, codex={codex}')
        print(f'Proxy configuration: {"present" if network else "none"}')
        return
    if os.geteuid() != 0:
        parser.error('Installation requires sudo')
    network_path = Path('/opt/cc-lianli-lcd/openai-limits.env')
    fd = os.open(network_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(environment)
    for extension, body in [('service', service), ('timer', timer)]:
        path = Path('/etc/systemd/system') / f'cc-lianli-openai-limits.{extension}'
        path.write_text(body)
        path.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'cc-lianli-openai-limits.timer'], check=True)
    subprocess.run(['systemctl', 'start', 'cc-lianli-openai-limits.service'], check=True)


if __name__ == '__main__':
    main()

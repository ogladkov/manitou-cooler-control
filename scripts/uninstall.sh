#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
systemctl stop cc-plugin-lianli-lcd.service 2>/dev/null || true
systemctl disable --now cc-lianli-openai-limits.timer 2>/dev/null || true
systemctl stop cc-lianli-openai-limits.service 2>/dev/null || true
rm -f /etc/systemd/system/cc-lianli-openai-limits.{service,timer}
rm -f /var/lib/coolercontrol/plugins/lianli-lcd/manifest.toml
rm -f /etc/systemd/system/cc-plugin-lianli-lcd.service
rm -f /etc/systemd/system/coolercontrold.service.d/90-lianli-lcd-compat.conf
systemctl daemon-reload
echo 'Registration and compatibility override removed; files in /opt retained.'
echo 'Return to packaged CoolerControl: sudo systemctl restart coolercontrold'

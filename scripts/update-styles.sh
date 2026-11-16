#!/usr/bin/env bash
# Update an existing compatibility installation without reinstalling the USB plugin.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID == 0 ]] || { echo 'Run: sudo bash scripts/update-styles.sh' >&2; exit 1; }
[[ $(/usr/bin/coolercontrold --version) == 'coolercontrold 4.3.1' ]] || {
    echo 'This build requires CoolerControl 4.3.1' >&2; exit 1;
}
[[ -f /etc/systemd/system/coolercontrold.service.d/90-lianli-lcd-compat.conf &&
   -x /opt/cc-lianli-lcd/coolercontrold ]] || {
    echo 'Install first with: sudo bash scripts/install.sh --with-compat' >&2; exit 1;
}
[[ -x "$project_dir/artifacts/coolercontrold-4.3.1-lcd" ]] || {
    echo 'Build the compatibility binary first' >&2; exit 1;
}
cp -p /opt/cc-lianli-lcd/coolercontrold /opt/cc-lianli-lcd/coolercontrold.previous
install -m755 "$project_dir/artifacts/coolercontrold-4.3.1-lcd" /opt/cc-lianli-lcd/coolercontrold.new
mv -f /opt/cc-lianli-lcd/coolercontrold.new /opt/cc-lianli-lcd/coolercontrold
echo 'Updated. Activate with: sudo systemctl restart coolercontrold'
echo 'Previous binary: /opt/cc-lianli-lcd/coolercontrold.previous'

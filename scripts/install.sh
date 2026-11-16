#!/usr/bin/env bash
# Explicit local installation; the --with-compat flag opts into the daemon override.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
[[ $EUID == 0 ]] || { echo 'Run: sudo bash scripts/install.sh --with-compat' >&2; exit 1; }
compat=false
openai_limits=false
for option in "$@"; do
    case "$option" in
        --with-compat) compat=true ;;
        --with-openai-limits) openai_limits=true ;;
        *) echo 'Usage: install.sh [--with-compat] [--with-openai-limits]' >&2; exit 1 ;;
    esac
done
if $openai_limits; then
    /usr/bin/python3 "$project_dir/scripts/install-openai-limits.py" --check
fi
if $compat; then
    [[ -x "$project_dir/artifacts/coolercontrold-4.3.1-lcd" ]] || {
        echo 'Build artifacts/coolercontrold-4.3.1-lcd first (scripts/build-compat.sh)' >&2; exit 1;
    }
    [[ $(/usr/bin/coolercontrold --version) == 'coolercontrold 4.3.1' ]] || {
        echo 'Compatibility override applies only to CoolerControl 4.3.1' >&2; exit 1;
    }
fi
[[ -d "$project_dir/artifacts/wheels" ]] || {
    echo 'Prepare wheels first: .venv/bin/pip wheel . -w artifacts/wheels' >&2; exit 1;
}
# Check before changing the installed plugin when this TRYX revision is present.
for product_file in /sys/bus/usb/devices/*/product; do
    if [[ -r "$product_file" && $(< "$product_file") == cm01_se ]]; then
        for dependency in adb ffmpeg; do
            command -v "$dependency" >/dev/null || {
                echo 'TRYX requires: sudo apt install adb ffmpeg' >&2; exit 1;
            }
        done
        break
    fi
done
install -d -m755 /opt/cc-lianli-lcd /var/lib/coolercontrol/plugins/lianli-lcd
/usr/bin/python3 -m venv /opt/cc-lianli-lcd/venv
/opt/cc-lianli-lcd/venv/bin/pip install --no-index --upgrade --force-reinstall \
    --find-links "$project_dir/artifacts/wheels" coolercontrol-lianli-lcd==0.5.1
install -m644 "$project_dir/packaging/manifest.toml" \
    /var/lib/coolercontrol/plugins/lianli-lcd/manifest.toml
if $compat; then
    # Replace the inode atomically: the currently running binary may be in use.
    if [[ -x /opt/cc-lianli-lcd/coolercontrold ]]; then
        cp -p /opt/cc-lianli-lcd/coolercontrold /opt/cc-lianli-lcd/coolercontrold.previous
    fi
    install -m755 "$project_dir/artifacts/coolercontrold-4.3.1-lcd" /opt/cc-lianli-lcd/coolercontrold.new
    mv -f /opt/cc-lianli-lcd/coolercontrold.new /opt/cc-lianli-lcd/coolercontrold
    install -d -m755 /etc/systemd/system/coolercontrold.service.d
    install -m644 "$project_dir/packaging/90-lianli-lcd-compat.conf" \
        /etc/systemd/system/coolercontrold.service.d/90-lianli-lcd-compat.conf
fi
systemctl daemon-reload
if $openai_limits; then
    /usr/bin/python3 "$project_dir/scripts/install-openai-limits.py"
fi
echo 'Installed. Activate with: sudo systemctl restart coolercontrold'
echo 'This restarts cooling control; existing CoolerControl settings are preserved.'

#!/usr/bin/env bash
# Build a separate 4.3.1 binary. Does not install or restart any service.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_dir=${1:-"$project_dir/build/coolercontrol-4.3.1"}
if [[ ! -d "$source_dir/.git" ]]; then
    git clone --depth 1 --branch 4.3.1 https://github.com/codifryed/coolercontrol.git "$source_dir"
fi
source_dir=$(cd "$source_dir" && pwd)
[[ $(git -C "$source_dir" rev-parse HEAD) == b255e1e768eba4cec374acb0b463c942ee82d645 ]] || {
    echo 'Expected exact CoolerControl 4.3.1 source commit' >&2; exit 1;
}
patch_file="$project_dir/patches/coolercontrol-4.3.1-plugin-lcd.patch"
if git -C "$source_dir" apply --check "$patch_file" 2>/dev/null; then
    git -C "$source_dir" apply "$patch_file"
else
    git -C "$source_dir" apply --reverse --check "$patch_file"
fi
(
    cd "$source_dir/coolercontrold"
    cargo run --locked --release -p cc-image --example sensor_style_previews -- \
        "$source_dir/coolercontrol-ui/public/lcd-styles"
)
(
    cd "$source_dir/coolercontrol-ui"
    npm ci --ignore-scripts
    npm run type-check
    npx vitest run src/components/__tests__/SensorStylePicker.spec.ts src/components/__tests__/GraphThresholdEditor.spec.ts src/utils/__tests__/lcdSensors.spec.ts
    npm run build-only -- --outDir ../coolercontrold/daemon/resources/app
)
if ! command -v protoc >/dev/null; then
    export PROTOC="$project_dir/scripts/protoc"
fi
(
    cd "$source_dir/coolercontrold"
    cargo test --locked --release -p coolercontrold -p cc-image -j "${BUILD_JOBS:-4}" \
        lianli_
    cargo build --locked --release -p coolercontrold -j "${BUILD_JOBS:-4}"
)
mkdir -p "$project_dir/artifacts"
install -m755 "$source_dir/coolercontrold/target/release/coolercontrold" \
    "$project_dir/artifacts/coolercontrold-4.3.1-lcd"
"$project_dir/artifacts/coolercontrold-4.3.1-lcd" --version

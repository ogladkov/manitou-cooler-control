#!/usr/bin/env bash
# Compile our small Android renderer; no APK/firmware or vendor code is bundled.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build_dir="$project_dir/build/tryx-renderer"
mkdir -p "$build_dir/classes" "$project_dir/src/lianli_lcd/data"
r8="$build_dir/r8-8.5.35.jar"
if [[ ! -f "$r8" ]]; then
    curl --fail --location --max-time 120 \
        https://dl.google.com/dl/android/maven2/com/android/tools/r8/8.5.35/r8-8.5.35.jar \
        -o "$r8.download"
    mv "$r8.download" "$r8"
fi
printf '%s  %s\n' 4733945987ee0a840fafc34080b135259e01678412e07212b23f706334290294 "$r8" | sha256sum --check --status
# Invoke javac's module so distributions with a compiler module but no javac
# launcher also work. Our Java code uses only Java 8 APIs and Android reflection.
java -m jdk.compiler/com.sun.tools.javac.Main -source 8 -target 8 -Xlint:-options \
    -d "$build_dir/classes" "$project_dir/android/tryx/FrameServer.java"
java -cp "$r8" com.android.tools.r8.D8 --release --min-api 30 --output "$build_dir" \
    "$build_dir/classes/com/manitou/tryx/FrameServer.class" \
    "$build_dir/classes/com/manitou/tryx/FrameServer\$1.class"
install -m644 "$build_dir/classes.dex" "$project_dir/src/lianli_lcd/data/tryx-frame-server.dex"
echo 'Built TRYX frame renderer'

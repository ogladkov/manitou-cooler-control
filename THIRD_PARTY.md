# Sources and licenses

This project is distributed under GPL-3.0-or-later (`LICENSE`).

* `proto/coolercontrol/**` and generated `src/coolercontrol/**`: CoolerControl
  **4.3.1**, commit `b255e1e768eba4cec374acb0b463c942ee82d645`,
  <https://github.com/codifryed/coolercontrol/tree/4.3.1/coolercontrold/daemon/resources/proto>.
  Copyright Guy Boldon, Eren Simsek and contributors; GPL-3.0-or-later.
* Protocol implementation adapted from **lian-li-linux**, commit
  `c5db973d3717d806f278cc2da8c56ffa8accdd35`:
  `crates/lianli-devices/src/{tl_lcd,slv3_lcd,crypto}.rs`,
  <https://github.com/sgtaziz/lian-li-linux>.
  Copyright (c) 2026 sgtaziz; MIT notice in `vendor/lian-li-linux-LICENSE`.
  RGB formats additionally use `tl_fan/controller.rs` and
  `wireless/{controller,discovery,rgb,transport,mb_sync,fan_type}.rs` at the
  same pinned commit. Only lighting and state-query commands are implemented.
* `tests/vendor/tinyuz/`: unmodified upstream C decoder and headers from
  <https://github.com/sisong/tinyuz>, commit
  `1d74ffa4d453796df352df470733f45dfa099bb1`.
  Copyright (c) 2012–2022 HouSisong; MIT, included in that directory and
  `vendor/tinyuz-LICENSE`. Used only for independent round-trip tests.
  `rgb_protocol.py` encodes a literal-only subset of this format in Python;
  no native compression library is installed at runtime. The 32-frame limit
  keeps the uncompressed encoding below the firmware's 12,288-byte limit.

The optional compatibility binary is a build of CoolerControl 4.3.1 with
`patches/coolercontrol-4.3.1-plugin-lcd.patch` applied. The patch changes only
LCD mode classification for device plugins and isolates temperature image
files per LCD channel to prevent concurrent updates overwriting each other.
It also adds original Digital, Ring, Dial and Trend temperature layouts,
a native UI picker, and per-channel style persistence. These layouts use
CoolerControl's existing bundled Roboto fonts, not L-Connect binary assets.
The Mono Minimal, Mono Dial, Mono Segment and Mono Paper layouts are also
original code-rendered designs; the seven-segment display uses vector geometry.
Alien Orbit, Alien Relic and Alien Signal also use original vector geometry,
including invented decorative glyphs; they require no additional fonts or assets.
The LCD patch now supports typed RPM and percent sources, correct units/ranges,
and missing-data frames in all fifteen styles. Typed source IDs remain inside
LCD settings and are not added to the cooling profile temperature selector.
System utilization uses Linux procfs and NVIDIA's installed `nvidia-smi` utility;
no NVIDIA binaries or libraries are redistributed with this plugin.
Mono Trace is an original graph-only layout, with per-screen threshold rules
persisted by CoolerControl. It uses the existing vector renderer and color picker;
no additional runtime libraries or image assets are required.
Mono Reserve is an original vector ring whose arc length represents the remaining
quota. Mono Reverse combines that ring with the existing trace renderer, fitted
inside the ring, and uses no additional assets. The optional quota exporter uses the installed OpenAI Codex CLI's official
[`account/rateLimits/read` app-server API](https://learn.chatgpt.com/docs/app-server).
No Codex executable, authentication files or OpenAI UI assets are redistributed.
Rebuild instructions are in
`scripts/build-compat.sh`; upstream source is pinned by commit. Do not
redistribute the binary without providing corresponding GPL source and notices.

`scripts/protoc` invokes the development dependency grpcio-tools. To regenerate:

```bash
.venv/bin/python -m grpc_tools.protoc -I proto --python_out=src --grpc_python_out=src \
  $(find proto -name '*.proto' | sort)
```

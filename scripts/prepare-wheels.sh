#!/usr/bin/env bash
# Match the Python interpreter used by the system service, not Conda's Python.
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
/usr/bin/python3 -m venv "$project_dir/build/wheel-env"
"$project_dir/build/wheel-env/bin/pip" wheel "$project_dir" -w "$project_dir/artifacts/wheels"

#!/usr/bin/env bash
# Run the app locally against the fake Slurm commands in tests/bin (no
# cluster needed). Open http://127.0.0.1:8080/ afterwards.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
export PATH="$PWD/tests/bin:$PATH"   # fake sacct/sreport/sinfo first on PATH
exec python3 server.py --host 127.0.0.1 --port 8080 "$@"

#!/usr/bin/env bash
# POSIX compatibility wrapper. The Python launcher is the cross-platform source
# of truth; Windows users can run collab-watch.cmd or collab-watch.py directly.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$HERE/collab-watch.py" "$@"

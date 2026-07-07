#!/usr/bin/env bash
# Adapter for running Cursor Agent SDK under `collab watch --exec`.
#
# The collab watcher feeds the claimed message on STDIN as JSON (same path as
# `codex exec`). This wrapper invokes cursor-exec.py, which calls
# `cursor_sdk.Agent.prompt` locally and writes the review to stdout.
#
# Usage:
#   collab watch --project <P> --agent cursor-1 \
#     --exec /path/to/cursor-exec.sh
#
# Or via the launcher:
#   collab-watch.sh cursor <project> [repo-dir]
#
# Requires: pip install cursor-sdk, CURSOR_API_KEY (unless your local bridge
# allows env fallback). Read-only by default (CURSOR_READONLY=1 → plan mode).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export COLLAB_CWD="${COLLAB_CWD:-$PWD}"
exec python3 "$HERE/cursor-exec.py" "$@"

#!/usr/bin/env bash
# Adapter for running Antigravity CLI (agy) under `collab watch --exec`.
#
# The collab watcher feeds the claimed message on STDIN as JSON. Antigravity CLI
# wants the prompt as the `-p/--print` argument, so this bridges the two: read
# stdin -> pass it as -p.
#
# Usage:
#   collab watch --project <P> --agent antigravity-1 \
#     --exec /path/to/antigravity-exec.sh
#
# Or via the launcher:
#   collab-watch.sh antigravity <project> [repo-dir]
#   collab-watch.sh agy <project> [repo-dir]
#
# Any extra args (e.g. `--add-dir <dir>`) are forwarded to agy ahead of the prompt.
#
# Model override: ANTIGRAVITY_MODEL or AGY_MODEL (default: unset, agy picks).
# Read-only by default: ANTIGRAVITY_READONLY=1 → --mode plan. Set to 0 for
# accept-edits mode. Non-interactive runs need --dangerously-skip-permissions.
set -euo pipefail
AGY_BIN="${AGY_BIN:-agy}"
MODEL="${ANTIGRAVITY_MODEL:-${AGY_MODEL:-}}"

readonly_args=()
if [ "${ANTIGRAVITY_READONLY:-${AGY_READONLY:-1}}" != "0" ]; then
  readonly_args=(--mode plan)
fi

model_args=()
if [ -n "$MODEL" ]; then
  model_args=(--model "$MODEL")
fi

prompt="$(cat)"
# ${arr[@]+"${arr[@]}"} = bash-3.2-safe expansion of a possibly-empty array under set -u.
exec "$AGY_BIN" --print --dangerously-skip-permissions \
  ${readonly_args[@]+"${readonly_args[@]}"} \
  ${model_args[@]+"${model_args[@]}"} \
  "$@" -p "$prompt"

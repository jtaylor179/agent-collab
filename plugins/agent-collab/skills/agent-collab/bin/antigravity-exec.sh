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
# Print timeout: ANTIGRAVITY_PRINT_TIMEOUT or AGY_PRINT_TIMEOUT (default 20m; agy's
# own default of 5m silently truncates tool-using reviews to an empty answer).
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

# agy's --print-timeout defaults to 5m, and on expiry it returns EMPTY stdout with
# exit 0 and "print timeout ... returning partial output" on stderr -- the same
# fail-open shape as the permission denial below. A review that has to run tools
# routinely needs longer than 5m, so raise the ceiling and let the operator tune it.
# Keep this at or below `collab watch --agent-timeout`, which kills the whole run.
timeout_args=(--print-timeout
  "${ANTIGRAVITY_PRINT_TIMEOUT:-${AGY_PRINT_TIMEOUT:-20m}}")

prompt="$(cat)"
if [ -z "$prompt" ]; then
  echo "antigravity-exec: empty stdin" >&2
  exit 2
fi

# Deliberately not exec: agy exits 0 with an EMPTY stdout when a tool permission is
# auto-denied in headless mode -- it explains itself on stderr ("no output produced --
# a tool required the ... permission"), which the watcher never reads as the answer.
# Measured: rc=0, stdout 0 bytes, stderr 301 bytes, and it happens with or without
# --mode plan, so the mode is not the cause. Left alone, the watcher posts an empty
# review and acks the task: the work is silently lost while the queue looks healthy.
# Capture the answer and fail closed so the message is released for redelivery instead.
# ${arr[@]+"${arr[@]}"} = bash-3.2-safe expansion of a possibly-empty array under set -u.
set +e
answer="$("$AGY_BIN" --print --dangerously-skip-permissions \
  ${timeout_args[@]+"${timeout_args[@]}"} \
  ${readonly_args[@]+"${readonly_args[@]}"} \
  ${model_args[@]+"${model_args[@]}"} \
  "$@" -p "$prompt")"
status=$?
set -e

if [ -z "$(printf '%s' "$answer" | tr -d '[:space:]')" ]; then
  echo "antigravity-exec: agy produced no answer (exit $status). In headless mode a tool" >&2
  echo "permission can be auto-denied even with --dangerously-skip-permissions; see agy's" >&2
  echo "own message above and add an allow-rule under permissions.allow in settings.json." >&2
  echo "No collab message was claimed." >&2
  exit 1
fi

printf '%s\n' "$answer"
exit "$status"

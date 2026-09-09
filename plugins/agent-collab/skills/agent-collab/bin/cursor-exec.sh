#!/usr/bin/env bash
# Adapter for running Cursor CLI (`agent` / `cursor-agent`) under `collab watch --exec`.
#
# The collab watcher feeds the claimed message on STDIN as JSON. Cursor CLI wants
# that prompt as a positional argument with `-p/--print`, so this bridges the two:
# read stdin -> pass it as the print-mode prompt.
#
# Usage:
#   collab watch --project <P> --agent cursor-1 \
#     --exec /path/to/cursor-exec.sh
#
#   cursor-exec.sh --preflight   # resolve binary + auth; used by collab-watch.sh
#
# Or via the launcher:
#   collab-watch.sh cursor <project> [repo-dir]
#
# Requires: Cursor CLI on PATH (`agent` or `cursor-agent`), or CURSOR_BIN.
# Auth: `agent login` or CURSOR_API_KEY. Read-only by default
# (CURSOR_READONLY=1 → --mode plan). Override model with CURSOR_MODEL
# (default: composer-2.5). Extra args are forwarded ahead of the prompt.
set -euo pipefail

resolve_cursor_agent() {
  if [ -n "${CURSOR_BIN:-}" ]; then
    printf '%s\n' "$CURSOR_BIN"
    return 0
  fi
  if command -v agent >/dev/null 2>&1; then
    command -v agent
    return 0
  fi
  if command -v cursor-agent >/dev/null 2>&1; then
    command -v cursor-agent
    return 0
  fi
  for p in \
    "$HOME/.local/bin/agent" \
    "$HOME/.local/bin/cursor-agent"
  do
    if [ -x "$p" ]; then
      printf '%s\n' "$p"
      return 0
    fi
  done
  return 1
}

cursor_authenticated() {
  # Cursor CLI `status --format json` exits 0 even when logged out, so parse the
  # body. A CURSOR_API_KEY is sufficient without a login session.
  if [ -n "${CURSOR_API_KEY:-}" ]; then
    return 0
  fi
  local status_json
  if ! status_json="$("$1" status --format json 2>&1)"; then
    printf '%s\n' "$status_json" >&2
    return 1
  fi
  if printf '%s' "$status_json" | grep -q '"isAuthenticated"[[:space:]]*:[[:space:]]*true'; then
    return 0
  fi
  printf '%s\n' "$status_json" >&2
  return 1
}

preflight() {
  local bin
  if ! bin="$(resolve_cursor_agent)"; then
    echo "collab-watch: Cursor CLI is unavailable on PATH." >&2
    echo "Install Cursor CLI (curl https://cursor.com/install -fsS | bash), or set CURSOR_BIN to the agent binary. No collab message was claimed." >&2
    exit 1
  fi
  if ! cursor_authenticated "$bin"; then
    echo "collab-watch: Cursor authentication is unavailable in this execution context." >&2
    echo "Run 'agent login' in this context, or set CURSOR_API_KEY. No collab message was claimed." >&2
    exit 1
  fi
}

if [ "${1:-}" = "--preflight" ]; then
  preflight
  exit 0
fi

if ! CURSOR_AGENT="$(resolve_cursor_agent)"; then
  echo "cursor-exec: Cursor CLI not found (install agent / cursor-agent, or set CURSOR_BIN)" >&2
  exit 1
fi

MODEL="${CURSOR_MODEL:-composer-2.5}"
CWD="${COLLAB_CWD:-$PWD}"

readonly_args=()
force_args=()
if [ "${CURSOR_READONLY:-1}" != "0" ]; then
  readonly_args=(--mode plan)
else
  force_args=(--force)
fi

mode_override=()
if [ -n "${CURSOR_AGENT_MODE:-}" ]; then
  readonly_args=()
  case "$CURSOR_AGENT_MODE" in
    plan|ask) mode_override=(--mode "$CURSOR_AGENT_MODE");;
    agent) ;;
    *)
      echo "CURSOR_AGENT_MODE must be plan, ask, or agent" >&2
      exit 2
      ;;
  esac
fi

model_args=()
if [ -n "$MODEL" ]; then
  model_args=(--model "$MODEL")
fi

prompt="$(cat)"
if [ -z "$prompt" ]; then
  echo "cursor-exec: empty stdin" >&2
  exit 2
fi

# ${arr[@]+"${arr[@]}"} = bash-3.2-safe expansion of a possibly-empty array under set -u.
exec "$CURSOR_AGENT" --print --output-format text --trust --workspace "$CWD" \
  ${readonly_args[@]+"${readonly_args[@]}"} \
  ${mode_override[@]+"${mode_override[@]}"} \
  ${force_args[@]+"${force_args[@]}"} \
  ${model_args[@]+"${model_args[@]}"} \
  "$@" "$prompt"

#!/usr/bin/env bash
# Simple launcher for the collab watcher — wraps all the path/env boilerplate so you
# (or the /collab-watch slash command) can start a hands-off reviewer in one line:
#
#   collab-watch.sh copilot      <project> [repo-dir]
#   collab-watch.sh codex        <project> [repo-dir]
#   collab-watch.sh claude       <project> [repo-dir]
#   collab-watch.sh cursor       <project> [repo-dir]
#   collab-watch.sh antigravity  <project> [repo-dir]
#   collab-watch.sh agy          <project> [repo-dir]
#
# It resolves collab.py + the copilot adapter next to itself, defaults COLLAB_ROOT to
# $HOME/.collab (the shared bus root), runs in [repo-dir] (default: current dir), and
# picks the right --exec for the agent (Copilot needs the prompt-as-arg + perms +
# model; Codex and Claude read stdin). Extra watcher flags can be passed via COLLAB_WATCH_ARGS
# (e.g. COLLAB_WATCH_ARGS="--idle-exit" to stop once the queue is empty). Extra Codex
# exec flags can be passed via COLLAB_CODEX_EXEC_ARGS; by default we request the fast
# service tier because codex-cli 0.125 rejects the priority tier in this watcher path.
# Extra Claude flags can be passed via COLLAB_CLAUDE_EXEC_ARGS; Claude always runs in
# print mode, with non-interactive permissions, no browser, and no saved sessions.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HERE/collab.py"
WRAP="$HERE/copilot-exec.sh"

agent_arg="${1:-}"; project="${2:-}"; repo="${3:-$PWD}"
if [ -z "$agent_arg" ] || [ -z "$project" ]; then
  echo "usage: collab-watch.sh <copilot|codex|claude|cursor|antigravity|agy> <project> [repo-dir]" >&2
  exit 2
fi

export COLLAB_ROOT="${COLLAB_ROOT:-$HOME/.collab}"
cd "$repo"

case "$agent_arg" in
  copilot|copilot-1) agent="copilot-1"; exec_argv=("$WRAP");;
  claude|claude-1)
    agent="claude-1"
    claude_args="${COLLAB_CLAUDE_EXEC_ARGS-}"
    claude_exec_args=()
    if [ -n "$claude_args" ]; then
      # shellcheck disable=SC2206  # intentional simple word-splitting for env-provided flags
      claude_exec_args=($claude_args)
    fi
    exec_argv=(claude --print --permission-mode dontAsk --no-chrome \
      --no-session-persistence ${claude_exec_args[@]+"${claude_exec_args[@]}"})
    ;;
  cursor|cursor-1) agent="cursor-1"; exec_argv=("$HERE/cursor-exec.sh");;
  antigravity|antigravity-1|agy|agy-1) agent="antigravity-1"; exec_argv=("$HERE/antigravity-exec.sh");;
  codex|codex-1)
    agent="codex-1"
    codex_args="${COLLAB_CODEX_EXEC_ARGS--c service_tier=fast}"
    codex_exec_args=()
    if [ -n "$codex_args" ]; then
      # shellcheck disable=SC2206  # intentional simple word-splitting for env-provided flags
      codex_exec_args=($codex_args)
    fi
    exec_argv=(codex exec ${codex_exec_args[@]+"${codex_exec_args[@]}"})
    ;;
  *) echo "unknown agent '$agent_arg' (use 'copilot', 'codex', 'claude', 'cursor', 'antigravity', or 'agy')" >&2; exit 2;;
esac

echo "collab-watch: agent=$agent project=$project root=$COLLAB_ROOT repo=$PWD exec=${exec_argv[*]}" >&2

# COLLAB_WATCH_DETACH=1 makes the watcher daemonize (double-fork + setsid) so it OUTLIVES
# the shell that launched it. Set this whenever an AGENT launches the watcher from its
# transient per-turn shell (Cursor/Claude/etc.) — otherwise the watcher is a child of
# that shell's process group and is killed when the turn ends, orphaning its claim every
# round. Interactive/terminal use can leave it unset to keep the watcher in the
# foreground. The detach flag is added to the watcher args below.
detach_args=()
if [ "${COLLAB_WATCH_DETACH:-0}" = "1" ]; then
  detach_args=(--detach)
  [ -n "${COLLAB_WATCH_LOG:-}" ] && detach_args+=(--log "$COLLAB_WATCH_LOG")
fi

# shellcheck disable=SC2086  # COLLAB_WATCH_ARGS is intentionally word-split
exec python3 "$BIN" watch --project "$project" --agent "$agent" \
  ${detach_args[@]+"${detach_args[@]}"} ${COLLAB_WATCH_ARGS:-} --exec "${exec_argv[@]}"

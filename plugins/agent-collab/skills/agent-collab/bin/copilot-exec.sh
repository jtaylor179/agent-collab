#!/usr/bin/env bash
# Adapter for running GitHub Copilot CLI under `collab watch --exec`.
#
# Why this exists: the collab watcher feeds the claimed message to the agent on
# STDIN (great for `codex exec`, which reads stdin). GitHub Copilot CLI instead
# wants the prompt as the `-p/--prompt` ARGUMENT, and requires `--allow-all-tools`
# for non-interactive mode (without it, it blocks on a permission prompt and the
# watcher times out). This bridges the two: read stdin -> pass it as -p.
#
# Usage:
#   collab watch --project <P> --agent copilot-1 \
#     --exec /path/to/copilot-exec.sh -C /path/to/repo
#
# Any extra args (e.g. `-C <dir>` to set the working directory, or
# `--add-dir <dir>`) are forwarded to copilot ahead of the prompt.
#
# Model is pinned to Claude Opus 4.6 (claude-opus-4.6) by default — Copilot reviews should
# run on Opus 4.6. Override per-run with COPILOT_MODEL (e.g. `COPILOT_MODEL=auto ... watch
# ...`) or by passing your own --model in "$@" (a later --model wins).
set -euo pipefail
COPILOT_MODEL="${COPILOT_MODEL:-claude-opus-4.6}"

# Read-only by default. A watcher review should read files and run git/build/tests but
# never edit the repo, so we deny the file-mutation tools. Per GitHub Copilot's docs,
# "denial rules always take precedence" — so these win even under --allow-all-tools.
# NOTE: `bash` stays available (a reviewer needs `git diff` / build / tests), so a shell
# command could still technically write. This blocks the agent's dedicated edit tools;
# it is NOT a hermetic sandbox. For a hard guarantee, run against a read-only checkout.
# Disable (full read/write) with COPILOT_READONLY=0.
readonly_args=()
if [ "${COPILOT_READONLY:-1}" != "0" ]; then
  readonly_args=(--deny-tool write edit create apply_patch)
fi

prompt="$(cat)"
# ${arr[@]+"${arr[@]}"} = bash-3.2-safe expansion of a possibly-empty array under set -u.
exec copilot --allow-all-tools ${readonly_args[@]+"${readonly_args[@]}"} \
  --model "$COPILOT_MODEL" --silent "$@" -p "$prompt"

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
# Model is pinned to gpt-5.4 by default; override per-run with COPILOT_MODEL
# (e.g. `COPILOT_MODEL=auto ... watch ...`) or by passing your own --model in "$@"
# (a later --model wins, so an explicit flag overrides this default).
set -euo pipefail
COPILOT_MODEL="${COPILOT_MODEL:-gpt-5.4}"
prompt="$(cat)"
exec copilot --allow-all-tools --model "$COPILOT_MODEL" "$@" -p "$prompt"

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
# Model defaults to Claude Opus 4.8. GPT-5.6 Terra is the recommended OpenAI
# alternative. Override per-run with COPILOT_MODEL, or pass your own --model in "$@"
# (a later --model wins).
#
# Reasoning effort defaults to high. Override per-run with
# COPILOT_REASONING_EFFORT (none|minimal|low|medium|high|xhigh|max), or pass your own
# --reasoning-effort in "$@" (again, the later explicit flag wins).
#
# Copilot's text stdout is not a safe framing protocol for exact or long output.
# The adapter requests non-streaming JSONL, validates every transport envelope, and
# emits only the single final assistant.message data.content. That content stays
# opaque: it is never parsed, normalized, trimmed, or repaired.
#
# Repository custom instructions remain enabled by default: code tasks need their
# AGENTS.md constraints. For exact-output jobs whose complete contract is already in
# the watcher payload, set COPILOT_CUSTOM_INSTRUCTIONS=0 to add
# `--no-custom-instructions` and prevent unrelated repository instructions from
# contaminating a machine-readable response.
set -euo pipefail
COPILOT_MODEL="${COPILOT_MODEL:-claude-opus-4.8}"
COPILOT_REASONING_EFFORT="${COPILOT_REASONING_EFFORT:-high}"

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

custom_instruction_args=()
case "${COPILOT_CUSTOM_INSTRUCTIONS:-1}" in
  1|true|on)
    ;;
  0|false|off)
    custom_instruction_args=(--no-custom-instructions)
    ;;
  *)
    echo "COPILOT_CUSTOM_INSTRUCTIONS must be 0/1, false/true, or off/on" >&2
    exit 2
    ;;
esac

prompt="$(cat)"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
transport_file="$(mktemp "${TMPDIR:-/tmp}/agent-collab-copilot.XXXXXX")"
trap 'rm -f "$transport_file"' EXIT

# Capture first and check Copilot's status before releasing any assistant content.
# This prevents a partial-but-plausible response from escaping when Copilot fails.
set +e
copilot --allow-all-tools ${readonly_args[@]+"${readonly_args[@]}"} \
  ${custom_instruction_args[@]+"${custom_instruction_args[@]}"} \
  --model "$COPILOT_MODEL" --reasoning-effort "$COPILOT_REASONING_EFFORT" \
  "$@" --silent --output-format json --stream off -p "$prompt" >"$transport_file"
copilot_status=$?
set -e
if [ "$copilot_status" -ne 0 ]; then
  echo "copilot exited with status $copilot_status; response withheld" >&2
  exit "$copilot_status"
fi

python3 "$script_dir/copilot-jsonl-extract.py" <"$transport_file"

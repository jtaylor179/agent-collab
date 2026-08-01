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
# Any extra args are forwarded to Copilot ahead of the prompt. In read-only mode,
# `-C` selects the one repository to snapshot and `--add-dir` is rejected.
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
# never edit the source repo. Denying Copilot's dedicated mutation tools is not enough:
# a reviewer still needs `bash`, and shell-driven `git reset`/`git clean` can mutate the
# current checkout. Read-only mode therefore runs Copilot in an independent disposable
# Git snapshot. For ordinary stable Git states, the snapshot starts at the source
# HEAD, then replays staged and unstaged diffs and copies Git-visible untracked files.
# On macOS, sandbox-exec additionally denies writes to the live worktree and all of
# its Git metadata, so even a shell command that rediscovers the source path cannot
# mutate it. Unsupported platforms fail closed instead of claiming read-only safety.
# Disable snapshot isolation (full live-repo read/write) with COPILOT_READONLY=0.
readonly_args=()
readonly_mode=0
if [ "${COPILOT_READONLY:-1}" != "0" ]; then
  readonly_mode=1
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
adapter_temp_base="${TMPDIR:-/tmp}"
adapter_temp_root="$(mktemp -d "${adapter_temp_base%/}/agent-collab-copilot.XXXXXX")"
transport_file="$adapter_temp_root/transport.jsonl"

cleanup() {
  if [ -n "${adapter_temp_root:-}" ] && [ -d "$adapter_temp_root" ]; then
    case "$(basename -- "$adapter_temp_root")" in
      agent-collab-copilot.*)
        rm -rf -- "$adapter_temp_root"
        ;;
      *)
        echo "refusing to remove unexpected adapter temp path: $adapter_temp_root" >&2
        ;;
    esac
  fi
}
trap cleanup EXIT

copilot_forward_args=("$@")
copilot_repo_args=()
copilot_process_dir="$PWD"
sandbox_args=()
if [ "$readonly_mode" -eq 1 ]; then
  source_dir="$PWD"
  expect_source_dir=0
  copilot_forward_args=()
  for arg in "$@"; do
    if [ "$expect_source_dir" -eq 1 ]; then
      source_dir="$arg"
      expect_source_dir=0
      continue
    fi
    case "$arg" in
      -C)
        expect_source_dir=1
        ;;
      -C?*)
        source_dir="${arg#-C}"
        ;;
      --add-dir|--add-dir=*)
        echo "COPILOT_READONLY=1 does not permit --add-dir; review one isolated repository" >&2
        exit 2
        ;;
      *)
        copilot_forward_args+=("$arg")
        ;;
    esac
  done
  if [ "$expect_source_dir" -eq 1 ]; then
    echo "-C requires a repository path" >&2
    exit 2
  fi

  if ! source_repo="$(git -C "$source_dir" rev-parse --show-toplevel 2>/dev/null)"; then
    echo "COPILOT_READONLY=1 requires -C (or the current directory) to be inside a Git repository" >&2
    exit 2
  fi
  if ! source_head="$(git -C "$source_repo" rev-parse --verify HEAD 2>/dev/null)"; then
    echo "COPILOT_READONLY=1 requires a repository with an existing HEAD commit" >&2
    exit 2
  fi
  source_git_dir="$(git -C "$source_repo" rev-parse --absolute-git-dir)"
  source_git_common="$(
    git -C "$source_repo" rev-parse --path-format=absolute --git-common-dir
  )"

  case "$(uname -s)" in
    Darwin)
      if ! command -v sandbox-exec >/dev/null 2>&1; then
        echo "COPILOT_READONLY=1 requires sandbox-exec on macOS" >&2
        exit 2
      fi
      sandbox_profile='(version 1)
(allow default)
(deny file-write* (subpath (param "SOURCE_REPO")))
(deny file-write* (subpath (param "SOURCE_GIT_DIR")))
(deny file-write* (subpath (param "SOURCE_GIT_COMMON")))'
      sandbox_args=(
        sandbox-exec
        -D "SOURCE_REPO=$source_repo"
        -D "SOURCE_GIT_DIR=$source_git_dir"
        -D "SOURCE_GIT_COMMON=$source_git_common"
        -p "$sandbox_profile"
      )
      ;;
    *)
      echo "COPILOT_READONLY=1 has no enforced write sandbox for this platform" >&2
      exit 2
      ;;
  esac

  snapshot_repo="$adapter_temp_root/repo"
  staged_patch="$adapter_temp_root/staged.patch"
  unstaged_patch="$adapter_temp_root/unstaged.patch"
  untracked_list="$adapter_temp_root/untracked.zlist"
  source_state_before="$adapter_temp_root/source-state-before.sha256"
  source_state_after="$adapter_temp_root/source-state-after.sha256"
  snapshot_state="$adapter_temp_root/snapshot-state.sha256"

  # Capture one ordinary Git state. The before/snapshot/after fingerprints include
  # HEAD, porcelain status, both binary diffs, and NUL-safe untracked paths and
  # contents. Any concurrent source change or mixed snapshot fails closed before
  # Copilot starts. Unmerged and intent-to-add states are rejected by the helper.
  python3 "$script_dir/copilot-snapshot-state.py" \
    "$source_repo" >"$source_state_before"
  git -C "$source_repo" diff --cached --binary --full-index --no-ext-diff >"$staged_patch"
  git -C "$source_repo" diff --binary --full-index --no-ext-diff >"$unstaged_patch"
  git -C "$source_repo" ls-files --others --exclude-standard -z >"$untracked_list"

  # --no-local prevents object hardlinks or alternates back to the source repo.
  # Removing origin ensures ordinary commands inside the snapshot have no source path.
  git clone --quiet --no-local --no-hardlinks --no-checkout \
    "$source_repo" "$snapshot_repo"
  git -C "$snapshot_repo" checkout --quiet --detach "$source_head"
  git -C "$snapshot_repo" remote remove origin

  if [ -s "$staged_patch" ]; then
    git -C "$snapshot_repo" apply --index --whitespace=nowarn "$staged_patch"
  fi
  if [ -s "$unstaged_patch" ]; then
    git -C "$snapshot_repo" apply --whitespace=nowarn "$unstaged_patch"
  fi
  if [ -s "$untracked_list" ]; then
    while IFS= read -r -d '' relative_path; do
      source_path="$source_repo/$relative_path"
      snapshot_path="$snapshot_repo/$relative_path"
      mkdir -p -- "$(dirname -- "$snapshot_path")"
      if [ -L "$source_path" ]; then
        cp -P -- "$source_path" "$snapshot_path"
      elif [ -f "$source_path" ]; then
        cp -p -- "$source_path" "$snapshot_path"
      else
        echo "Git-visible untracked path disappeared while snapshotting: $relative_path" >&2
        exit 1
      fi
    done <"$untracked_list"
  fi

  python3 "$script_dir/copilot-snapshot-state.py" \
    "$snapshot_repo" >"$snapshot_state"
  python3 "$script_dir/copilot-snapshot-state.py" \
    "$source_repo" >"$source_state_after"
  if ! cmp -s "$source_state_before" "$source_state_after" ||
      ! cmp -s "$source_state_before" "$snapshot_state"; then
    echo "source repository changed while creating the read-only snapshot; review not started" >&2
    exit 1
  fi

  # Copilot honors the last -C. Keep caller args for compatibility, then force the
  # isolated snapshot as the effective working repository. The live source -C is
  # removed from argv entirely, and the process cwd is moved into the snapshot too.
  copilot_repo_args=(-C "$snapshot_repo")
  copilot_process_dir="$snapshot_repo"
fi

# Capture first and check Copilot's status before releasing any assistant content.
# This prevents a partial-but-plausible response from escaping when Copilot fails.
set +e
(
  cd "$copilot_process_dir"
  unset OLDPWD
  ${sandbox_args[@]+"${sandbox_args[@]}"} \
    copilot --allow-all-tools ${readonly_args[@]+"${readonly_args[@]}"} \
    ${custom_instruction_args[@]+"${custom_instruction_args[@]}"} \
    --model "$COPILOT_MODEL" --reasoning-effort "$COPILOT_REASONING_EFFORT" \
    ${copilot_forward_args[@]+"${copilot_forward_args[@]}"} \
    ${copilot_repo_args[@]+"${copilot_repo_args[@]}"} \
    --silent --output-format json --stream off -p "$prompt"
) >"$transport_file"
copilot_status=$?
set -e
if [ "$copilot_status" -ne 0 ]; then
  echo "copilot exited with status $copilot_status; response withheld" >&2
  exit "$copilot_status"
fi

python3 "$script_dir/copilot-jsonl-extract.py" <"$transport_file"

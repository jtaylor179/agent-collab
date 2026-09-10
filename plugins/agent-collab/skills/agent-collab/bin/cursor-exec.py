#!/usr/bin/env python3
"""Adapter for running Cursor CLI (`agent` / `cursor-agent`) under `collab watch --exec`.

The collab watcher feeds the claimed message on STDIN. Cursor CLI wants that
prompt as a positional argument with `-p/--print`, so this bridges the two:
read stdin -> pass it as the print-mode prompt.

This is the cross-platform twin of cursor-exec.sh. collab-watch.py invokes it on
every platform so the Cursor watcher does not depend on Bash/WSL; on Windows the
CLI ships as `agent.CMD`, which `shutil.which` resolves via PATHEXT but a bare
`command -v agent` in Git Bash does not. Keep the two adapters behaviourally
identical -- collab/test_collab.py asserts the same contract against both.

Usage:
  collab watch --project <P> --agent cursor-1 --exec <python> cursor-exec.py

  cursor-exec.py --preflight   # resolve binary + auth; used by collab-watch.py

Requires: Cursor CLI on PATH (`agent` or `cursor-agent`), or CURSOR_BIN.
Auth: `agent login` or CURSOR_API_KEY. Read-only by default
(CURSOR_READONLY=1 -> --mode plan). Override model with CURSOR_MODEL
(default: composer-2.5). Friendly names like "grok 4.6" and "composer 2.5"
are mapped to CLI ids. Extra args are forwarded ahead of the prompt.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


DEFAULT_MODEL = "composer-2.5"

# Product/friendly names -> `agent --list-models` ids, keyed by the normalized
# form (lowercased, runs of space/underscore/hyphen collapsed to one hyphen).
# Unknown values -- including already-canonical ids -- pass through unchanged.
MODEL_ALIASES = {
    "": DEFAULT_MODEL,
    "composer": DEFAULT_MODEL,
    "composer-2": DEFAULT_MODEL,
    "composer2.5": DEFAULT_MODEL,
    "composer-2.5": DEFAULT_MODEL,
    "composer-fast": "composer-2.5-fast",
    "composer-2-fast": "composer-2.5-fast",
    "composer-2.5-fast": "composer-2.5-fast",
    "grok": "cursor-grok-4.6-high",
    "grok-4.6": "cursor-grok-4.6-high",
    "grok4.6": "cursor-grok-4.6-high",
    "cursor-grok-4.6": "cursor-grok-4.6-high",
    "cursor-grok-4.6-high": "cursor-grok-4.6-high",
    "grok-fast": "cursor-grok-4.6-high-fast",
    "grok-4.6-fast": "cursor-grok-4.6-high-fast",
    "cursor-grok-4.6-fast": "cursor-grok-4.6-high-fast",
    "cursor-grok-4.6-high-fast": "cursor-grok-4.6-high-fast",
    "grok-4.6-low": "cursor-grok-4.6-low",
    "cursor-grok-4.6-low": "cursor-grok-4.6-low",
    "grok-4.6-low-fast": "cursor-grok-4.6-low-fast",
    "cursor-grok-4.6-low-fast": "cursor-grok-4.6-low-fast",
    "grok-4.6-medium": "cursor-grok-4.6-medium",
    "cursor-grok-4.6-medium": "cursor-grok-4.6-medium",
    "grok-4.6-medium-fast": "cursor-grok-4.6-medium-fast",
    "cursor-grok-4.6-medium-fast": "cursor-grok-4.6-medium-fast",
    "grok-4.6-xhigh": "cursor-grok-4.6-xhigh",
    "grok-4.6-extra-high": "cursor-grok-4.6-xhigh",
    "cursor-grok-4.6-xhigh": "cursor-grok-4.6-xhigh",
    "grok-4.6-xhigh-fast": "cursor-grok-4.6-xhigh-fast",
    "cursor-grok-4.6-xhigh-fast": "cursor-grok-4.6-xhigh-fast",
    "grok-4.5": "cursor-grok-4.5-high",
    "cursor-grok-4.5": "cursor-grok-4.5-high",
    "cursor-grok-4.5-high": "cursor-grok-4.5-high",
    "grok-4.5-fast": "cursor-grok-4.5-high-fast",
    "cursor-grok-4.5-fast": "cursor-grok-4.5-high-fast",
    "cursor-grok-4.5-high-fast": "cursor-grok-4.5-high-fast",
}


def _normalize_model(value):
    """Lowercase and collapse space/underscore/hyphen runs, like `tr -s ' _-' '-'`."""
    return re.sub(r"[ _-]+", "-", (value or "").lower())


def _resolve_model(value):
    return MODEL_ALIASES.get(_normalize_model(value), value)


def _resolve_agent():
    """Locate the Cursor CLI, or None. CURSOR_BIN wins, then PATH, then ~/.local/bin."""
    override = os.environ.get("CURSOR_BIN")
    if override:
        return override
    for name in ("agent", "cursor-agent"):
        found = shutil.which(name)
        if found:
            return found
    home = os.environ.get("HOME") or os.path.expanduser("~")
    for name in ("agent", "cursor-agent"):
        candidate = Path(home) / ".local" / "bin" / name
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return str(candidate)
    return None


def _authenticated(binary):
    """Return (ok, detail). `status --format json` exits 0 even when logged out, so
    parse the body. A CURSOR_API_KEY is sufficient without a login session."""
    if os.environ.get("CURSOR_API_KEY"):
        return True, ""
    try:
        result = subprocess.run(
            [binary, "status", "--format", "json"],
            capture_output=True, text=True)
    except OSError as exc:
        return False, str(exc)
    body = (result.stdout or "") + (result.stderr or "")
    if result.returncode:
        return False, body
    if re.search(r'"isAuthenticated"\s*:\s*true', body):
        return True, ""
    return False, body


def _install_hint():
    if os.name == "nt":
        # The published installer is a bash script that rejects MINGW/MSYS and has
        # no Windows artifact; Windows gets the CLI from the Cursor app instead.
        return ("Install Cursor CLI from the Cursor app, or set CURSOR_BIN to the "
                "agent binary (e.g. %LOCALAPPDATA%\\cursor-agent\\agent.cmd).")
    return ("Install Cursor CLI (curl https://cursor.com/install -fsS | bash), or "
            "set CURSOR_BIN to the agent binary.")


def _preflight():
    binary = _resolve_agent()
    if binary is None:
        print("collab-watch: Cursor CLI is unavailable on PATH.", file=sys.stderr)
        print(f"{_install_hint()} No collab message was claimed.", file=sys.stderr)
        return 1
    ok, detail = _authenticated(binary)
    if not ok:
        if detail.strip():
            print(detail.strip(), file=sys.stderr)
        print("collab-watch: Cursor authentication is unavailable in this "
              "execution context.", file=sys.stderr)
        print("Run 'agent login' in this context, or set CURSOR_API_KEY. "
              "No collab message was claimed.", file=sys.stderr)
        return 1
    return 0


def build_command(binary, extra_args, prompt, cwd):
    """Assemble the Cursor print-mode argv. Mirrors the tail of cursor-exec.sh."""
    readonly_args = []
    force_args = []
    if os.environ.get("CURSOR_READONLY", "1") != "0":
        readonly_args = ["--mode", "plan"]
    else:
        force_args = ["--force"]

    mode_override = []
    agent_mode = os.environ.get("CURSOR_AGENT_MODE")
    if agent_mode:
        readonly_args = []
        if agent_mode in ("plan", "ask"):
            mode_override = ["--mode", agent_mode]
        elif agent_mode != "agent":
            raise ValueError("CURSOR_AGENT_MODE must be plan, ask, or agent")

    model = _resolve_model(os.environ.get("CURSOR_MODEL") or DEFAULT_MODEL)
    model_args = ["--model", model] if model else []

    return [
        binary, "--print", "--output-format", "text", "--trust",
        "--workspace", cwd,
        *readonly_args, *mode_override, *force_args, *model_args,
        *extra_args, prompt,
    ]


def main(argv):
    if argv and argv[0] == "--preflight":
        return _preflight()

    binary = _resolve_agent()
    if binary is None:
        print("cursor-exec: Cursor CLI not found (install agent / cursor-agent, "
              "or set CURSOR_BIN)", file=sys.stderr)
        return 1

    # `$(cat)` strips trailing newlines; match that so the prompt round-trips.
    prompt = sys.stdin.read().rstrip("\n")
    if not prompt:
        print("cursor-exec: empty stdin", file=sys.stderr)
        return 2

    cwd = os.environ.get("COLLAB_CWD") or os.getcwd()
    try:
        command = build_command(binary, list(argv), prompt, cwd)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        return subprocess.run(command).returncode
    except OSError as exc:
        print(f"cursor-exec: failed to run {binary}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

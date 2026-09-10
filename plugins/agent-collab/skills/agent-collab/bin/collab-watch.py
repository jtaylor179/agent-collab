#!/usr/bin/env python3
"""Cross-platform launcher for the agent-collab watcher.

The shell wrapper delegates here on POSIX. Windows users can invoke this file
directly (or use collab-watch.cmd), so Codex does not depend on Bash/WSL.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


ALIASES = {
    "copilot": "copilot-1",
    "copilot-1": "copilot-1",
    "codex": "codex-1",
    "codex-1": "codex-1",
    "claude": "claude-1",
    "claude-1": "claude-1",
    "cursor": "cursor-1",
    "cursor-1": "cursor-1",
    "antigravity": "antigravity-1",
    "antigravity-1": "antigravity-1",
    "agy": "antigravity-1",
    "agy-1": "antigravity-1",
}


def _env_argv(name, default=""):
    """Read optional argv from JSON (preferred) or a shell-like string."""
    raw = os.environ.get(name, default).strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} must be a JSON argv array or argument string: {exc}")
        if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values):
            raise ValueError(f"{name} JSON value must be a non-empty-string array")
        return values
    values = shlex.split(raw, posix=os.name != "nt")
    if os.name == "nt":
        values = [
            value[1:-1] if len(value) >= 2 and value[0] == value[-1]
            and value[0] in "\"'" else value
            for value in values
        ]
    return values


def _resolve_claude_model(name):
    """Map friendly Claude model names to CLI ids; unknown values pass through."""
    key = re.sub(r"[ _-]+", "-", (name or "").strip().lower())
    return {
        "": "claude-sonnet-5",
        "sonnet": "claude-sonnet-5",
        "sonnet-5": "claude-sonnet-5",
        "claude-sonnet-5": "claude-sonnet-5",
        "opus": "claude-opus-5",
        "opus-5": "claude-opus-5",
        "claude-opus-5": "claude-opus-5",
        "fable": "claude-fable-5",
        "fable-5": "claude-fable-5",
        "claude-fable-5": "claude-fable-5",
        "haiku": "haiku",
        "haiku-4.5": "haiku",
        "claude-haiku-4-5": "haiku",
        "opus-4.8": "claude-opus-4-8",
        "claude-opus-4-8": "claude-opus-4-8",
    }.get(key, name)


def _exec_argv(agent, here):
    if agent == "codex-1":
        return ["codex", "exec", *_env_argv(
            "COLLAB_CODEX_EXEC_ARGS", "-c service_tier=fast")]
    if agent == "claude-1":
        claude_args = _env_argv("COLLAB_CLAUDE_EXEC_ARGS")
        if "--model" not in claude_args:
            model = _resolve_claude_model(
                os.environ.get("CLAUDE_MODEL", "claude-sonnet-5"))
            claude_args = ["--model", model, *claude_args]
        return [
            "claude", "--print", "--permission-mode", "dontAsk",
            "--no-chrome", "--no-session-persistence", *claude_args,
        ]
    if agent == "cursor-1":
        return [sys.executable, str(here / "cursor-exec.py")]
    if agent == "copilot-1":
        if os.name == "nt":
            raise ValueError(
                "the Copilot watcher adapter currently requires WSL or Git Bash; "
                "run collab-watch.sh from that environment")
        return [str(here / "copilot-exec.sh")]
    if agent == "antigravity-1":
        if os.name == "nt":
            raise ValueError(
                "the Antigravity watcher adapter currently requires WSL or Git Bash; "
                "run collab-watch.sh from that environment")
        return [str(here / "antigravity-exec.sh")]
    raise ValueError(f"unsupported agent id: {agent}")


def _preflight_claude(agent):
    if agent != "claude-1" or os.environ.get(
            "COLLAB_CLAUDE_AUTH_PREFLIGHT", "1") == "0":
        return
    if shutil.which("claude") is None:
        raise RuntimeError(
            "Claude Code is unavailable on PATH. Install Claude Code, or launch "
            "from an authenticated host context. No collab message was claimed.")
    result = subprocess.run(
        ["claude", "auth", "status"], capture_output=True, text=True)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(
            "Claude authentication is unavailable in this execution context. "
            f"{detail} Run 'claude auth login' here, or launch from a host context "
            "that can access the keychain. No collab message was claimed.")


def build_command(agent_arg, project, repo):
    here = Path(__file__).resolve().parent
    agent = ALIASES.get(agent_arg.lower())
    if agent is None:
        raise ValueError(
            f"unknown agent '{agent_arg}' (use copilot, codex, claude, cursor, "
            "antigravity, or agy)")
    repo = Path(repo).resolve(strict=True)
    if not repo.is_dir():
        raise ValueError(f"repository path is not a directory: {repo}")
    root = Path(os.environ.get("COLLAB_ROOT", str(repo / ".collab"))).resolve()
    command = [
        sys.executable, str(here / "collab.py"), "--root", str(root),
        "watch", "--project", project, "--agent", agent,
    ]
    if os.environ.get("COLLAB_WATCH_DETACH", "0") == "1":
        command.append("--detach")
        log = os.environ.get("COLLAB_WATCH_LOG")
        if log:
            command.extend(["--log", log])
    admission = os.environ.get("COLLAB_OUTPUT_ADMISSION_ARGV")
    admission_timeout = os.environ.get("COLLAB_OUTPUT_ADMISSION_TIMEOUT")
    if admission:
        command.extend(["--output-admission-argv", admission])
        if admission_timeout:
            command.extend(["--output-admission-timeout", admission_timeout])
    elif admission_timeout:
        raise ValueError(
            "COLLAB_OUTPUT_ADMISSION_TIMEOUT requires COLLAB_OUTPUT_ADMISSION_ARGV")
    command.extend(_env_argv("COLLAB_WATCH_ARGS"))
    command.extend(["--exec", *_exec_argv(agent, here)])
    return agent, repo, root, command


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Launch a hands-off agent-collab reviewer")
    parser.add_argument("agent", choices=sorted(ALIASES))
    parser.add_argument("project")
    parser.add_argument("repo", nargs="?", default=os.getcwd())
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the resolved command as JSON without starting or claiming work")
    args = parser.parse_args(argv)
    try:
        agent, repo, root, command = build_command(
            args.agent, args.project, args.repo)
        if args.dry_run:
            print(json.dumps({
                "agent": agent, "project": args.project, "repo": str(repo),
                "root": str(root), "command": command,
            }))
            return 0
        _preflight_claude(agent)
        print(
            f"collab-watch: agent={agent} project={args.project} root={root} "
            f"repo={repo} exec={shlex.join(command[command.index('--exec') + 1:])}",
            file=sys.stderr,
        )
        return subprocess.call(command, cwd=repo, env=os.environ.copy())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"collab-watch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

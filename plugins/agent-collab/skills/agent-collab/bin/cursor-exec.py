#!/usr/bin/env python3
"""Adapter for running Cursor Agent SDK under `collab watch --exec`.

The collab watcher feeds the claimed message as JSON on stdin (see collab.py
`_agent_payload`). This script turns that payload into a one-shot local agent
review via `cursor_sdk.Agent.prompt` and writes ONLY the review text to stdout.

Requires: `pip install cursor-sdk` and a working local Cursor agent runtime
(CURSOR_API_KEY for cloud-backed local runs per SDK docs).

Environment:
  CURSOR_API_KEY     API key (required unless your local bridge allows env fallback)
  CURSOR_MODEL       Model id (default: composer-2.5)
  CURSOR_READONLY    When 1 (default), use plan mode + read-only reviewer instructions
  CURSOR_AGENT_MODE  Override mode: agent | plan
  COLLAB_CWD         Working directory for the local agent (default: getcwd())
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any


def _build_prompt(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    instructions = (payload.get("instructions") or "").strip()
    if instructions:
        parts.append(instructions)

    msg = payload.get("message") or {}
    body = (msg.get("body") or "").strip()
    if body:
        header = (
            f"--- review request (round {msg.get('round', '?')}, "
            f"type {msg.get('type', '?')}, from {msg.get('from_agent', '?')}) ---"
        )
        parts.extend([header, body])

    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        ref = artifact.get("ref") or "artifact"
        if artifact.get("error"):
            parts.append(f"--- {ref} (load error) ---\n{artifact['error']}")
        else:
            content = artifact.get("content") or ""
            parts.append(f"--- {ref} ---\n{content}")

    if not parts:
        raise ValueError("empty collab payload: nothing to review")
    return "\n\n".join(parts)


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print("cursor-exec: empty stdin", file=sys.stderr)
        return 2

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"cursor-exec: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2

    try:
        prompt = _build_prompt(payload)
    except ValueError as exc:
        print(f"cursor-exec: {exc}", file=sys.stderr)
        return 2

    readonly = os.environ.get("CURSOR_READONLY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    mode = os.environ.get("CURSOR_AGENT_MODE") or ("plan" if readonly else "agent")
    model = os.environ.get("CURSOR_MODEL", "composer-2.5")
    cwd = os.environ.get("COLLAB_CWD") or os.getcwd()
    api_key = os.environ.get("CURSOR_API_KEY") or None

    if readonly:
        prompt = (
            "READ-ONLY REVIEW: do not edit, create, or delete any files. "
            "Do not run commands that mutate the repo. Your output is review text only.\n\n"
            + prompt
        )

    try:
        from cursor_sdk import Agent, AgentOptions, LocalAgentOptions
        from cursor_sdk.errors import CursorAgentError
    except ImportError:
        print(
            "cursor-exec: cursor-sdk not installed (pip install cursor-sdk)",
            file=sys.stderr,
        )
        return 1

    options = AgentOptions(
        api_key=api_key,
        model=model,
        mode=mode,  # type: ignore[arg-type]
        local=LocalAgentOptions(cwd=cwd),
    )

    try:
        result = Agent.prompt(prompt, options)
    except CursorAgentError as exc:
        print(f"cursor-exec: agent startup failed: {exc}", file=sys.stderr)
        return 1

    if result.status == "error":
        print(f"cursor-exec: run failed (id={result.id})", file=sys.stderr)
        if result.result:
            print(result.result, file=sys.stderr)
        return 2

    text = (result.result or "").strip()
    if not text:
        print("cursor-exec: agent returned empty review", file=sys.stderr)
        return 2

    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

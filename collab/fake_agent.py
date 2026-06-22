#!/usr/bin/env python3
"""A stand-in agent for testing `collab watch` without a real Codex/Copilot.

Reads the watcher payload (JSON) on stdin and writes a review to stdout, exactly
as `codex exec` / `copilot -p` would when driven by the agent-collab skill.

Env knobs (test-only):
  FAKE_AGENT_SLEEP=<seconds>  delay before responding (exercises heartbeat)
  FAKE_AGENT_MODE=ok|fail|empty
"""
import json
import os
import sys
import time

raw = sys.stdin.read()
try:
    payload = json.loads(raw)
except Exception:
    payload = {}

delay = float(os.environ.get("FAKE_AGENT_SLEEP", "0"))
if delay:
    time.sleep(delay)

mode = os.environ.get("FAKE_AGENT_MODE", "ok")
if mode == "fail":
    sys.stderr.write("fake agent: simulated failure\n")
    sys.exit(3)
if mode == "empty":
    sys.exit(0)  # produce no review
if mode == "hang":
    while True:  # never returns; the watcher must kill us on --agent-timeout
        time.sleep(1)

msg = (payload.get("message") or {}).get("body", "")
art = payload.get("artifact") or {}
seen = "with artifact" if art.get("content") else "no artifact"
print(f"REVIEW ({seen}): I disagree with one point in '{msg[:40]}'. "
      "The claim about X needs a benchmark before we commit; suggest reconsidering.")

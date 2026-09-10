#!/usr/bin/env python3
"""Capability probe: can each worker MODEL actually do the work?

fleet.ps1 -Verify only proves a model id is reachable. This runs the real task through
each model's adapter, extracts the code it wrote, and grades it against the held-out
test -- the same bar the full run uses. The bus is bypassed on purpose so a failure is
attributable to the model, not to watcher plumbing.

    python probe.py --task H3
    python probe.py --task M2 --models cursor-grok,claude-sonnet

Each invocation is a real, billed API call. Probe one hard task across the fleet before
committing to a full 12-task run.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from harness import TASKS

ROOT = Path(__file__).resolve().parent
BIN = (ROOT.parent / "plugins" / "agent-collab" / "skills" / "agent-collab" / "bin")
SANDBOX = ROOT / "probe-runs"

# label -> (tool, model). Mirrors fleet.ps1; Copilot is omitted because its adapter
# cannot run natively on Windows.
MODELS = {
    "cursor-composer": ("cursor", "composer-2.5"),
    "cursor-grok":     ("cursor", "grok 4.6"),
    "gemini-flash":    ("cursor", "gemini-3.8-flash-high"),
    "claude-haiku":    ("claude", "claude-haiku-4-5-20251001"),
    "claude-sonnet":   ("claude", "claude-sonnet-5"),
    "codex-luna":      ("codex",  "gpt-5.6-luna"),
    "codex-terra":     ("codex",  "gpt-5.6-terra"),
}

PROMPT = """{spec}

Write the complete implementation of `{fn}`.

Respond with ONE ```python code block containing the entire file and nothing else.
No prose, no explanation, no tests. Standard library only.
Implement the FULL contract above -- you are graded on hidden edge cases."""


def _adapter(tool, model, prompt):
    """Run one model through its adapter. Returns (stdout, error_or_None)."""
    env = dict(os.environ)
    for key in ("CURSOR_MODEL", "CLAUDE_MODEL", "COLLAB_CODEX_EXEC_ARGS"):
        env.pop(key, None)

    if tool == "cursor":
        env["CURSOR_MODEL"] = model
        # The adapter defaults to --mode plan, which returns EMPTY stdout with exit 0
        # on substantive tasks (measured: 1 char vs 4231 in ask mode). "ask" is still
        # read-only -- it does not touch the workspace -- but it actually answers.
        env["CURSOR_AGENT_MODE"] = "ask"
        argv, stdin = [sys.executable, str(BIN / "cursor-exec.py")], prompt
    elif tool == "claude":
        argv = ["claude", "--print", "--permission-mode", "dontAsk",
                "--no-chrome", "--no-session-persistence", "--model", model]
        stdin = prompt
    elif tool == "codex":
        argv, stdin = ["codex", "exec", "-m", model, "-"], prompt
    else:
        return "", f"unsupported tool {tool!r}"

    try:
        # Models emit box-drawing and emoji; the Windows default (cp1252) raises
        # UnicodeDecodeError mid-stream and hands back None.
        r = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return "", "timeout after 600s"
    except OSError as exc:
        return "", f"launch failed: {exc}"
    out, err_text = r.stdout or "", r.stderr or ""
    if r.returncode != 0:
        detail = (err_text or out).strip().splitlines()
        return out, (detail[-1][:110] if detail else f"exit {r.returncode}")
    return out, None


def _extract_code(text):
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    if blocks:
        return max(blocks, key=len).strip()
    # Some models answer with bare code when told to emit only a block.
    if re.search(r"^\s*(def|class)\s+\w+", text, re.M):
        return text.strip()
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--task", default="H3", help="task id from harness.TASKS")
    p.add_argument("--models", default=",".join(MODELS),
                   help="comma-separated labels from the roster")
    args = p.parse_args()

    task = next((t for t in TASKS if t["id"] == args.task), None)
    if task is None:
        print(f"unknown task {args.task!r}", file=sys.stderr)
        return 2

    labels = [m.strip() for m in args.models.split(",") if m.strip()]
    prompt = PROMPT.format(spec=task["spec"], fn=task["fn"])
    SANDBOX.mkdir(parents=True, exist_ok=True)

    print(f"probing {task['id']} ({task['band']}) -- {task['fn']}\n")
    print(f"{'model':<17}{'tool':<9}{'secs':>6}  {'public':<8}{'hidden':<8}detail")
    print("-" * 78)

    rows = []
    for label in labels:
        if label not in MODELS:
            print(f"{label:<17}{'?':<9}{'-':>6}  unknown roster label")
            continue
        tool, model = MODELS[label]
        started = time.time()
        out, err = _adapter(tool, model, prompt)
        elapsed = time.time() - started

        if err and not out.strip():
            print(f"{label:<17}{tool:<9}{elapsed:>6.0f}  {'-':<8}{'-':<8}{err}")
            rows.append((label, None, None))
            continue

        code = _extract_code(out)
        if not code:
            # Surface what the tool actually said. A CLI that is merely unauthenticated
            # answers on stdout with exit 1, which otherwise reads as "model was bad".
            first = next((l.strip() for l in out.splitlines() if l.strip()), "")
            why = err or (f"no code block; reply began: {first[:52]!r}"
                          if first else "empty reply")
            print(f"{label:<17}{tool:<9}{elapsed:>6.0f}  {'-':<8}{'-':<8}{why}")
            rows.append((label, None, None))
            continue

        run = SANDBOX / f"{task['id']}-{label}"
        run.mkdir(parents=True, exist_ok=True)
        (run / "impl.py").write_text(code + "\n", encoding="utf-8")
        (run / "check.py").write_text(
            "from impl import *  # noqa\n\n" + task["public"] + "\nprint('ok')\n",
            encoding="utf-8")
        (run / "_hidden.py").write_text(
            "from impl import *  # noqa\n\n" + task["hidden"] + "\nprint('ok')\n",
            encoding="utf-8")

        def _score(script):
            try:
                r = subprocess.run([sys.executable, script], cwd=str(run),
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30)
            except subprocess.TimeoutExpired:
                return False, "timeout"
            if r.returncode == 0:
                return True, ""
            tail = ((r.stderr or "") or (r.stdout or "")).strip().splitlines()
            return False, (tail[-1][:44] if tail else "failed")

        pub_ok, _ = _score("check.py")
        hid_ok, hid_detail = _score("_hidden.py")
        print(f"{label:<17}{tool:<9}{elapsed:>6.0f}  "
              f"{'PASS' if pub_ok else 'fail':<8}{'PASS' if hid_ok else 'fail':<8}{hid_detail}")
        rows.append((label, pub_ok, hid_ok))

    scored = [r for r in rows if r[2] is not None]
    if scored:
        print(f"\nheld-out passes: {sum(1 for r in scored if r[2])}/{len(scored)} reachable models")
    print(f"artifacts: {SANDBOX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

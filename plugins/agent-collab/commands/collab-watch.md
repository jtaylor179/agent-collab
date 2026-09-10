---
description: Launch a hands-off watcher so another agent (Copilot/Codex/Cursor) auto-reviews queued requests
---

The user wants to start the **watcher daemon** so another AI agent (Copilot, Codex,
**Cursor**, or **Antigravity / agy**) automatically picks up and answers review requests on the bus — without
anyone copy/pasting. Use the `agent-collab` skill.

`$ARGUMENTS` should contain: which agent (`copilot`, `codex`, **`claude`**, **`cursor`**, **`antigravity`**, or **`agy`**), the
project name, and optionally a repo directory. Examples: `copilot context-compaction`,
`codex my-project /path/to/repo`, **`cursor a5-phrase-level-keys /path/to/repo`**,
**`agy my-project /path/to/repo`**.

If the agent or project is missing, ask — do not guess the project name.

Steps:

1. Resolve `collab-watch.sh` next to `collab.py` (see skill for path resolution). Run:
   `collab-watch.sh <agent> <project> [repo-dir]`
   (defaults: `COLLAB_ROOT=<repo-dir>/.collab`, repo-dir = current dir).
   - **Copilot:** `copilot-exec.sh` adapter, model `claude-opus-4.8`, reasoning
     effort `high`, read-only by default. Override with `COPILOT_MODEL`
     (`gpt-5.6-terra` is the recommended alternative) and
     `COPILOT_REASONING_EFFORT`. The adapter uses validated non-streaming JSONL;
     for isolated exact-output jobs, set `COPILOT_CUSTOM_INSTRUCTIONS=0`.
   - **Claude:** `claude --print` (stdin). Defaults to Sonnet 5
     (`claude-sonnet-5`). Override with `CLAUDE_MODEL` (friendly names like
     `sonnet 5` / `opus` map to CLI ids). Extra flags via
     `COLLAB_CLAUDE_EXEC_ARGS`; a `--model` there wins over `CLAUDE_MODEL`.
   - **Codex:** `codex exec -c service_tier=fast` (stdin). Override with
     `COLLAB_CODEX_EXEC_ARGS`.
   - **Cursor:** `cursor-exec.sh` → Cursor CLI `agent -p` (prompt-as-arg). Requires
     `agent` or `cursor-agent` on PATH (or `CURSOR_BIN`) and `agent login` or
     `CURSOR_API_KEY`. Read-only by default (`CURSOR_READONLY=1` → `--mode plan`).
     Override model with `CURSOR_MODEL` (default `composer-2.5`; friendly names
     like `grok 4.6` map to `cursor-grok-4.6-high`).
   - **Antigravity:** `antigravity-exec.sh` → `agy --print` (prompt-as-arg). Requires
     `agy` on PATH. Read-only by default (`ANTIGRAVITY_READONLY=1` → `--mode plan`).
2. **Set `COLLAB_WATCH_DETACH=1`** so the watcher is SPAWNED in its own session
   and OUTLIVES your shell. This is REQUIRED when you (an agent) launch it: otherwise the
   watcher is a child of your transient per-turn shell and gets killed when the turn ends,
   orphaning its claim every round (the classic "watcher keeps dying / stuck claim"
   symptom). It prints `{"detached": true, "pid": N, "log": …}` and returns immediately; tail the
   log to watch progress. Only skip detach for a foreground watcher in a terminal you keep
   open.
3. Report agent/project/root and how to stop it (`pkill -f "watch --project <project>"`).
4. Optional: `COLLAB_WATCH_ARGS="--idle-exit"` to drain the queue then exit.
4. Distinct ids: `copilot-1` / `codex-1` / **`claude-1`** / **`cursor-1`** / **`antigravity-1`** — never the initiator's id.

After launching, suggest `/collab-status <project>` or `log --follow`.

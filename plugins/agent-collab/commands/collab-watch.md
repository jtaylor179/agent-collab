---
description: Launch a hands-off watcher so another agent (Copilot/Codex/Cursor) auto-reviews queued requests
---

The user wants to start the **watcher daemon** so another AI agent (Copilot, Codex, or
**Cursor**) automatically picks up and answers review requests on the bus — without
anyone copy/pasting. Use the `agent-collab` skill.

`$ARGUMENTS` should contain: which agent (`copilot`, `codex`, or **`cursor`**), the
project name, and optionally a repo directory. Examples: `copilot context-compaction`,
`codex my-project /path/to/repo`, **`cursor a5-phrase-level-keys /path/to/repo`**.

If the agent or project is missing, ask — do not guess the project name.

Steps:

1. Resolve `collab-watch.sh` next to `collab.py` (see skill for path resolution). Run:
   `collab-watch.sh <agent> <project> [repo-dir]`
   (defaults: `COLLAB_ROOT=$HOME/.collab`, repo-dir = current dir).
   - **Copilot:** `copilot-exec.sh` adapter, model `gpt-5.4`, read-only by default.
   - **Codex:** `codex exec -c service_tier=fast` (stdin). Override with
     `COLLAB_CODEX_EXEC_ARGS`.
   - **Cursor:** `cursor-exec.sh` → `cursor_sdk` (stdin). Requires `pip install
     cursor-sdk` and `CURSOR_API_KEY`. Read-only by default (`CURSOR_READONLY=1`).
2. **Run it in the background** (long-running daemon). Report agent/project/root and how
   to stop it.
3. Optional: `COLLAB_WATCH_ARGS="--idle-exit"` to drain the queue then exit.
4. Distinct ids: `copilot-1` / `codex-1` / **`cursor-1`** — never the initiator's id.

After launching, suggest `/collab-status <project>` or `log --follow`.

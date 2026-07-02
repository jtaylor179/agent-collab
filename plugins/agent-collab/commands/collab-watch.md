---
description: Launch a hands-off watcher so another agent (Copilot/Codex) auto-reviews queued requests
---

The user wants to start the **watcher daemon** so another AI agent (Copilot or Codex)
automatically picks up and answers review requests on the bus — without anyone
copy/pasting. Use the `agent-collab` skill.

`$ARGUMENTS` should contain: which agent (`copilot` or `codex`), the project name, and
optionally a repo directory. Examples: `copilot context-compaction`,
`codex my-project /path/to/repo`.

If the agent or project is missing, ask — do not guess the project name.

Steps:

1. The launcher resolves everything else: run
   `${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab-watch.sh <agent> <project> [repo-dir]`
   (defaults: `COLLAB_ROOT=$HOME/.collab`, repo-dir = current dir, Copilot model
   `gpt-5.4`). For Copilot it uses the `copilot-exec.sh` adapter (model `gpt-5.4`, prompt
   as `-p`, and READ-ONLY by default — denies the edit/create/write tools so a review
   can't modify the repo); for Codex, `codex exec -c service_tier=fast` (stdin). Override
   Codex exec flags with `COLLAB_CODEX_EXEC_ARGS`, or set it to an empty string to use
   plain `codex exec`.
2. **Run it in the background** (this is a long-running daemon — it must not block the
   session). Report that the watcher is running, which agent/project/root it's serving,
   and how to stop it (kill the background task). Tell the user the watcher keeps
   running and answers new requests as they arrive.
3. If the user only wants to drain the *current* queue and then stop, set
   `COLLAB_WATCH_ARGS="--idle-exit"` before launching (it exits once the inbox is empty).
4. Note what bites people: the watching agent must use a **distinct id** from the
   initiator (the launcher already pins `copilot-1` / `codex-1`). Copilot runs
   **read-only by default** (the adapter denies edit/create/write), so a review can't
   modify the repo; if the user explicitly wants it to apply fixes, launch with
   `COPILOT_READONLY=0`. (`bash` stays available for git/build, so it isn't a hermetic
   sandbox — for a hard guarantee, point it at a read-only checkout.)

After launching, optionally suggest `/collab-status <project>` to watch requests get
answered, and remind them the watcher's replies land in the initiator's inbox.

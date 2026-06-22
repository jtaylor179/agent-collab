---
description: Start a collab review of a file in one step (create project, snapshot, broadcast)
---

The user wants to put a specific file up for multi-agent review in one move. Use the
`agent-collab` skill, acting as `claude-1` (or `$COLLAB_AGENT`).

$ARGUMENTS should contain a file path and optionally a project name and focus. If no
file path is present, STOP and ask the user which file to review and what to focus on —
do not create an empty project.

Steps:

1. Set `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py` and
   `COLLAB_ROOT` to a local-disk path (default `$HOME/.collab`).
2. `start` the project (derive a project name from $ARGUMENTS or ask).
3. `artifact put` the given file as the work product (v1).
4. Broadcast a `review_request` (round 1) referencing `<name>@v1`, with a body stating
   the review focus.
5. Then tell the user in plain words how to bring in a reviewer (run the Codex watcher,
   or say "review collab project X" in a Codex session), reminding them the reviewer
   must use a different agent id (`codex-1`) and the same `COLLAB_ROOT`.
6. **Ask the user if they want you to wait for feedback now:** e.g. "Want me to wait
   here for the reviewers' responses? I'll block up to ~10 min (this ties up the
   session), or you can come back later and say 'check collab project X'." Only wait if
   they say yes — then block with `claim --project X --wait 600`, and when a response
   arrives, reconcile it (post a `proposal` with an accept/reject ledger, rebut where
   needed). Offer to keep waiting for the next round.

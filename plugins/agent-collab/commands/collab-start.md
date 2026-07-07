---
description: Start a new multi-agent collaboration project and broadcast a review request
---

The user wants to start a collaboration project so other AI agents (Codex, Copilot,
**Cursor**) can review a spec or codebase. Use the `agent-collab` skill.

Steps:

1. Determine the project name (use the user's name, e.g. "A", or ask if unclear), the
   topic, the goal, and the path to the work product (spec or code file) to be reviewed.
   If they named a reviewer (**"with cursor"**, **"with codex"**, **"with copilot"**),
   note it for step 4.
2. Resolve `COLLAB_BIN` per the skill (Claude plugin path, then
   `~/.codex/skills/agent-collab/bin/collab.py`, then plugin cache). Set
   `COLLAB_ROOT` to a local-disk path, default `$HOME/.collab` (the SAME path every
   agent uses).
3. `start` the project as `claude-1` (or `codex-1` if you are Codex), then `artifact put`
   the work product as v1, then broadcast a `review_request` (round 1) referencing
   `name@v1`.
4. Tell the user the project is started and how the named reviewer joins:
   - **Cursor:** `collab-watch.sh cursor <project> <repo>` (needs `cursor-sdk` +
     `CURSOR_API_KEY`) or interactive *"review collab project X as cursor-1"* — see
     `references/cursor-start.md`.
   - **Codex:** `collab-watch.sh codex …` or *"review collab project X"* in Codex.
   - **Copilot:** `collab-watch.sh copilot …`
5. **Ask the user if they want you to wait for feedback now** (claim --wait 600 on yes).

Arguments (optional): $ARGUMENTS may contain the project name, reviewer, and/or path.

---
description: List all collaboration projects on the bus (under the current COLLAB_ROOT)
---

The user wants to see their collab projects. Use the `agent-collab` skill.

Steps:

1. Set `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py`. Determine
   `COLLAB_ROOT`: use `$COLLAB_ROOT` if set, otherwise the default `$HOME/.collab` family
   — note that projects are stored PER root, so list the one the user has been using.
2. Run `projects` (no project name needed):
   `python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" projects`
3. Report each project in plain language: name, state (gathering / reviewing /
   converged), message count, participant count, and last-updated time.
4. If the list is empty, say so and remind the user that projects live under a specific
   `COLLAB_ROOT` — if they expected to see some, they may have used a different root
   (a common cause of "my project disappeared"). Offer to check another root.

Arguments: none.

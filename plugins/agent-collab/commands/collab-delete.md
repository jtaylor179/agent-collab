---
description: Delete a collaboration project (with confirmation)
---

The user wants to delete a collab project. Use the `agent-collab` skill.

Steps:

1. Set `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py` and
   `COLLAB_ROOT` (default `$HOME/.collab` family — the root where the project
   lives). Determine the project name from $ARGUMENTS; if not given, run `projects` and
   ask which one.
2. **Confirm first.** Show the user the project's `status` (state, message count,
   participants) and ask them to confirm deletion — it is permanent and removes the
   project's messages, inbox, and artifacts (shared content blobs are left on disk).
3. On confirmation, run:
   `python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" delete --project <X> --yes`
4. Confirm it's gone (optionally run `projects` to show the updated list).

Never delete without an explicit yes from the user.

Arguments (optional): $ARGUMENTS may contain the project name.

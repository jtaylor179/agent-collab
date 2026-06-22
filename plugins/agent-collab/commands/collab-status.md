---
description: Show the status of a collaboration project (state, pending, stalled, threads)
---

The user wants a status snapshot of a collaboration project. Use the `agent-collab` skill.

Steps:

1. Set `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py` and
   `COLLAB_ROOT` to a local-disk path, default `$HOME/.collab` (the SAME path every agent uses). Determine the project name (from
   $ARGUMENTS or ask).
2. Run `status --project <X>`.
3. Report in plain language: project state (gathering / reviewing / converged / etc.),
   participants and roles, how many items are pending per agent, any `stalled`
   messages (agents that repeatedly failed — flag these), and how many threads are
   still open. If the user asks for detail, also show the `log`.

Arguments (optional): $ARGUMENTS may contain the project name.

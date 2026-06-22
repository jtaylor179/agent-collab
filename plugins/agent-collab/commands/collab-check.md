---
description: Check a collaboration project — drain your inbox, process feedback, and report
---

The user wants to check on a collaboration project. Use the `agent-collab` skill.

Steps:

1. Set `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py` and
   `COLLAB_ROOT` to a local-disk path, default `$HOME/.collab` (the SAME path every agent uses). Determine the project name (from
   $ARGUMENTS or ask).
2. Drain `claude-1`'s inbox: repeatedly `claim`; for each item, read the exact
   referenced artifact version, do the work (review, reconcile a response, etc.), and
   `complete` it in-thread. Stop when `claim` returns nothing.
3. Run `status --project <X>` and `log --project <X>` to see new activity.
4. Summarize for the user: what's new, who still owes a reply, any `stalled` items, and
   whether the project is converged, mid-reconciliation, or waiting.
5. **If nothing was pending** (or you're now waiting on another agent), OFFER to wait:
   ask "Want me to wait for the next message? I'll block up to ~10 min (this ties up the
   session)." If they say yes, use the blocking claim
   (`claim --project <X> --wait 600`) and handle whatever arrives — equivalent to
   `/collab-wait`. Don't start waiting without the user's go-ahead.

Apply the skill's review discipline: substantive feedback, accept/reject with reasons,
no agreement theater.

Arguments (optional): $ARGUMENTS may contain the project name.

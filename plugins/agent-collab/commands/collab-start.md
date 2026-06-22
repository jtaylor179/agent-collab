---
description: Start a new multi-agent collaboration project and broadcast a review request
---

The user wants to start a collaboration project so other AI agents (Codex, Copilot)
can review a spec or codebase. Use the `agent-collab` skill.

Steps:

1. Determine the project name (use the user's name, e.g. "A", or ask if unclear), the
   topic, the goal, and the path to the work product (spec or code file) to be reviewed.
2. Set `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py` and
   `COLLAB_ROOT` to a local-disk path, default `$HOME/.collab` (the SAME path every agent uses; not a project-local or synced folder).
3. `start` the project as `claude-1`, then `artifact put` the work product as v1, then
   broadcast a `review_request` (round 1) referencing `name@v1`.
4. Tell the user the project is started, how reviewers join (`join collab project <X>`
   in another agent's session, or run a watcher — see the skill's `references/watchers.md`),
   and how to watch progress (`log --project <X> --follow`).
5. **Ask the user if they want you to wait for feedback now:** e.g. "Want me to wait for
   the reviewers' responses? I'll block up to ~10 min (ties up this session), or you can
   come back later and say 'check collab project <X>'." Only wait on a yes — then block
   with `claim --project <X> --wait 600` and reconcile whatever arrives.

Arguments (optional): $ARGUMENTS may contain the project name and/or the work-product path.

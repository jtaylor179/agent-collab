---
description: Self-paced initiator loop — advance a multi-step collab plan hands-off (no manual re-kick)
---

The user wants a collab plan (a sequence of review-gated steps) to advance on its own
instead of having to hand-kick it each step ("had to run /loop continue to fish all the
steps"). Use the `agent-collab` skill. Each loop tick asks the bus for ONE recommended
action and executes it, so the plan moves without a human interpreting `status`.

Setup:

1. Set `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py` and
   `COLLAB_ROOT` (local-disk, default `$HOME/.collab` — the SAME path every agent uses).
   Determine the project (from $ARGUMENTS or ask) and your identity (`$COLLAB_AGENT`,
   else `claude-1`).
2. Confirm to the user that you'll self-pace this plan and roughly how (drive each step's
   review to convergence, then advance to the next step) until the plan is done or you
   hit something only they can resolve.

Each tick — run `next --project <X> --agent <me>` and branch on `action`:

- **`reclaim`** — a review claimed for you was abandoned (a watcher died mid-run). Run
  `reclaim --project <X> --agent <me> --force`, then continue. (A dead reviewer watcher
  is the usual cause of a stall; relaunch it if it's supposed to be hands-off — see
  `collab-watch`.)
- **`drain`** — claim each inbox item, read the referenced artifact version, do the work
  (reconcile the feedback / rebut / apply), and `complete` or `decide` it.
- **`decide`** — every reviewer has answered the latest `review_request`. Either post a
  `rebuttal`/revised artifact to open another round, or `decide` to converge this step.
- **`wait`** — you're waiting on reviewer(s); `why` names who and whether they look
  offline. Sleep briefly and re-tick. If a reviewer is offline and the run is meant to be
  hands-off, (re)launch its watcher before waiting again.
- **`done`** — this step's project converged. **Advance the plan:** start/broadcast the
  next step's review (`review --project <next> --file <path> ...`) and keep looping on the
  new project, or stop if this was the last step.
- **`broadcast`** — you're the initiator but nothing has gone out; post the first
  `review_request` for the current step.

Pacing: this is meant to run under `/loop` (self-paced). After each tick, if `action` is
`wait`, wait before the next tick; otherwise act immediately. **Stop and surface to the
user** — never loop silently — when: the round budget is exhausted without agreement, a
message is `stalled`, a reviewer stays offline with no watcher to relaunch, or `decide`
is blocked on a missing approver. Report what advanced and what's blocking.

Arguments (optional): $ARGUMENTS may contain the project name and/or the plan's ordered
step list (project names or artifact paths) so `done` can advance without asking.

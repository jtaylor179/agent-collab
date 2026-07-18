---
description: Orchestrator loop — drive an interchangeable-worker task plan to convergence (ADR-0001)
---

The user wants to run a plan where MANY interchangeable workers each do a piece of code
and only trusted reviewers accept it, converging hands-off. Use the `agent-collab` skill.
You are the **orchestrator**: you post tasks, let the worker pool do them, let trusted
reviewers accept them, and converge when all are accepted.

Setup:

1. `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py`, `COLLAB_ROOT`
   (local-disk, default `$HOME/.collab`). Project from $ARGUMENTS or ask. Your identity
   `$COLLAB_AGENT` (else `claude-1`).
2. Start the plan as the orchestrator (`start --project X --role orchestrator
   [--accept-policy any|all|final:<id>]`) if it's new. Register participants with DISTINCT
   ids and the right roles:
   - interchangeable code-doers → `join --agent <id> --role worker`
   - trusted reviewers (only these can accept) → `join --agent <id> --role approver`
   WHO is trusted is your choice — put whichever agents the user trusts to review as
   `approver` (e.g. claude-1, codex-1, copilot-1). The **acceptance policy** decides whose
   sign-off is the final say: `any` (any one approver, default), `all` (every approver), or
   `final:<id>` (one designated reviewer). Set it at start or later with
   `policy --project X --set <policy>`. Confirm the policy with the user if unstated.
3. Post the plan's tasks: for each unit of work, `post --type task --to broadcast --body
   "<task spec>"`. Tasks fan out to the worker pool and are work-stealing (first claim
   wins). Launch each worker's watcher (`collab-watch <agent> <project>`) so any worker
   can pull from the queue hands-off.

Each tick — run `next --project X --agent <me>` and branch on `action`:

- **`broadcast`** — no tasks posted yet; post the plan's `task`(s).
- **`wait`** — workers/reviewers still finishing (`out.tasks.by_state` shows the split:
  todo/claimed/submitted). Sleep briefly, re-tick. If a task sits `submitted` with no
  trusted reviewer online, (re)launch an approver's watcher.
- **`reclaim`** — a task was abandoned by a dead worker; `reclaim --project X --force`
  reopens it to the pool, then continue.
- **`decide`** — every task is `accepted` by a trusted reviewer; `decide` to converge the
  plan.
- **`done`** — converged. If this plan is one step of a larger program, advance to the
  next; otherwise stop.

The trust boundary is bus-enforced: a worker cannot post an `approval`, so it can never
accept its own code — you never have to police that yourself. `status.tasks` is the
source of truth for the roll-up (`todo → claimed → submitted → accepted`).

Pacing: run under `/loop` (self-paced). **Stop and surface to the user** — don't loop
silently — when a task keeps failing (`stalled`), a worker pool empties with work
outstanding, or no trusted reviewer is available to accept. Report what converged and
what's blocking.

Arguments (optional): $ARGUMENTS may contain the project name and/or the task list.

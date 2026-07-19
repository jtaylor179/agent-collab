---
description: Interactive wizard — set up a review OR an orchestrated worker plan (roles, models, acceptance policy)
---

The user wants a guided, interactive setup of a collaboration project. Use the
`agent-collab` skill and follow its **"Interactive setup wizard (bare invocation)"**
section exactly.

**Round −1 — saved profiles.** FIRST run `profile list`. If any exist, offer "use last
(`<newest>`) / pick from list / start fresh" (newest-first; `profiles[0]` is last used).
On reuse, `profile show --name <name> --use` and read its JSON, then SKIP every question it
answers (mode, roles, models, accept-policy, onboarding) — ask only for the work product.
After a FRESH setup, offer to `profile save --name <name> --data '<json>'` the reusable
answers (never the work-product path). See the skill's "Round −1" for details.

**Round 0 — mode.** First ask which shape the collaboration is (skip only if obvious
from the request, or a reused profile already set it):
- **Review** (default) — one work product, reviewers critique → follow "Wizard A: review"
  (the steps below).
- **Orchestrated plan** — many interchangeable workers pull tasks from a shared queue,
  trusted reviewers accept, an orchestrator converges → follow the skill's **"Wizard B:
  orchestrated plan"** instead: pick workers, trusted reviewers (approvers), an acceptance
  policy (`any|all|final:<id>`), and the task list; then `start --role orchestrator
  --accept-policy …`, join workers/approvers, `post --type task` per task, launch worker +
  approver watchers, and hand off to `/collab-orchestrate`.

Review-mode steps:

1. Resolve `COLLAB_BIN` and `COLLAB_ROOT` per the skill (same shared local-disk root,
   default `$HOME/.collab`). You are the initiator (`claude-1`, or `codex-1` in Codex).
2. **Round 1** (grouped questions — `AskUserQuestion` in Claude Code): work-product
   file path, reviewers (multi-select: codex-1 / copilot-1 / cursor-1 /
   antigravity-1), review focus, onboarding mode (background watchers you launch /
   printed commands / interactive sessions). Derive the project name from the file
   basename + date and state it rather than asking.
3. **Round 2** (one grouped round): per selected agent — role (reviewer default;
   approver = sign-off required before `decide` converges; observer = log-only),
   model (defaults: codex CLI default, copilot `claude-opus-4.6`, cursor `composer-2.5`,
   agy auto), and access (read-only default). Register approvers/observers with
   `join --role <role>` before launching their watcher. Use the skill's
   env-knob table (`COPILOT_MODEL`, `CURSOR_MODEL`, `ANTIGRAVITY_MODEL`,
   `COLLAB_CODEX_EXEC_ARGS`, `*_READONLY`).
4. Execute: `review --project <name> --file <path> --focus "…"` (create + snapshot +
   broadcast in one step), `join --role observer` for observers, then onboard
   reviewers per the chosen mode (launch `collab-watch.sh <agent> <project> <repo>`
   in the background with the chosen env knobs, or print the commands).
5. Offer to wait for responses now (`claim --wait 600`) or come back later with
   "check collab project <name>".

Arguments (optional): $ARGUMENTS may pre-answer any wizard question (file path,
reviewers, models…). Ask only what's still missing — never re-ask what was given.

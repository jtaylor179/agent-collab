---
description: Interactive wizard — step through reviewers, roles, models, and launch a collab review
---

The user wants a guided, interactive setup of a collaboration project. Use the
`agent-collab` skill and follow its **"Interactive setup wizard (bare invocation)"**
section exactly.

Steps:

1. Resolve `COLLAB_BIN` and `COLLAB_ROOT` per the skill (same shared local-disk root,
   default `$HOME/.collab`). You are the initiator (`claude-1`, or `codex-1` in Codex).
2. **Round 1** (grouped questions — `AskUserQuestion` in Claude Code): work-product
   file path, reviewers (multi-select: codex-1 / copilot-1 / cursor-1 /
   antigravity-1), review focus, onboarding mode (background watchers you launch /
   printed commands / interactive sessions). Derive the project name from the file
   basename + date and state it rather than asking.
3. **Round 2** (one grouped round): per selected agent — role (reviewer default;
   approver = sign-off required before `decide` converges; observer = log-only),
   model (defaults: codex CLI default, copilot `gpt-5.4`, cursor `composer-2.5`,
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

# ADR-0001: Orchestrated multi-worker plans with trusted-reviewer gating

**Status:** Accepted — implemented in v0.4.0 (2026-07-18)
**Date:** 2026-07-18
**Deciders:** jtaylor179 (repo owner)
**Affects:** agent-collab bus (`collab.py`), v0.3.8 → v0.4.0

> **Decision:** Option B accepted and shipped in v0.4.0. Workers confirmed exactly
> interchangeable (any agent does code); reviewers gated to `claude-1`/`codex-1` — the
> trusted-reviewer gate is role-checked — an approval under a non-approver identity is
> rejected (for correctly-identified clients; see Threat model). Phase 0 scaffold was
> skipped; built the hardened version directly. Delivered: `worker`/`orchestrator` roles,
> `task` type with work-stealing + dead-worker re-steal, approver-only acceptance,
> `status.tasks` roll-up, `decide` gated on all-tasks-accepted, `next` for worker/
> orchestrator, `/collab-orchestrate`. 12 new tests (86 total).
>
> **Follow-up (same day):** owner relaxed the trust set — Copilot may review too, and more
> broadly "give option to say who final reviewer is." Trust was never hardcoded (it's
> whoever joins as `approver`); added a per-plan **acceptance policy** `any | all |
> final:<id>` (`start --accept-policy` / `policy --set`, persisted via an idempotent
> `accept_policy` column migration) so a plan chooses whether any one approver, every
> approver, or one designated final reviewer must sign off. +6 tests (92 total).

## Threat model (security scope)

agent-collab is a **local, single-user** tool: every agent (`claude-1`, `codex-1`,
`copilot-1`, `cursor-1`, `antigravity-1`) is launched by the same person on one machine
against a local-disk bus (`$HOME/.collab`). **Agent identity is trusted by convention, not
authenticated** — it's the self-asserted `COLLAB_AGENT` / `--from` / `--by` string; the bus
has no credentials.

Within that model, the role/authority enforcement (only an `approver` may accept;
`approver`/`orchestrator` are granted, not self-assigned; only the owner may
`decide`/change `accept_policy`; acceptance requires a real post-submission approval) exists
to prevent **accidental or buggy self-elevation** — a watcher auto-joining with the wrong
role, a confused agent, a copy-paste slip — and to make the intended workflow the path of
least resistance. It is defense against *mistakes*, not against a *malicious* local process.

It explicitly does **NOT** defend against a process that deliberately forges another
agent's id: anything that can write to `$HOME/.collab` and set `COLLAB_AGENT` is already
inside the trust boundary and has fully compromised the bus regardless. Defending against
adversarial participants would require per-agent authenticated tokens (issued at
join/grant, verified on every mutating call, not overridable by CLI args) — deliberately
out of scope for the local cooperative model, and tracked as a possible future item if a
multi-tenant/networked deployment ever needs it. (Raised by Codex in the v0.4.1 dogfood
review; resolved by scoping the claim honestly rather than building auth.)

## Context

The agent-collab bus converges at exactly **one granularity: the project.** `decide`
flips the whole project to `converged` and closes every inbox row — there is no
per-sub-task convergence and no object that spans multiple work units. Multi-step plans
today are hand-rolled as a *chain of separate projects* (`…phase1b → 1c → 1d`), advanced
by a human running `/loop` / `/collab-loop`.

We want to run **many interchangeable workers against one overall plan**, with a
coordinator that assigns work and arbitrates convergence. Two constraints from the owner
shape the whole design:

1. **Workers are exactly interchangeable** — *any* agent (cursor, copilot, claude, codex,
   antigravity, …) can do the code work. This is a homogeneous work-queue, not an
   assigned split.
2. **Only `claude-1` and `codex-1` are trusted to review code** — any agent may *produce*
   code, but only these two may *accept* it. This is a trust boundary, and the owner's
   phrasing ("I only trust claude code and codex to review") means it should be
   **checked by the bus's role rules**, not left to orchestrator convention — so a
   non-approver identity's approval is rejected rather than mis-accepted. (Enforced for
   correctly-identified clients; the bus does not authenticate identities — see Threat
   model.)

The orchestrator does **two distinct jobs**, which must not be conflated:
- **Coordination / assignment** — hand tasks to workers, load-balance, recover dropped work.
- **Convergence arbitration** — decide when a task, and the whole plan, is done.

The bus already covers pieces of both and we should reuse them:
- **Claim/lease = work-stealing for free.** N workers pulling distinct items from one
  queue with mutual exclusion is the already-tested claim-collision path.
- **`approver` role already gates convergence.** `decide` is blocked until every approver
  posts an `approval`; approvals from non-approvers don't count. This is *exactly* a
  trusted-reviewer gate — claude-1/codex-1 as approvers gives us the trust boundary
  nearly for free.
- **`next` (v0.3.8)** already collapses a project's state into one recommended action; an
  orchestrator loop is `collab-loop` elevated to own convergence.
- **`reclaim` (v0.3.7)** already re-queues a dead worker's claim.

### Requirements

**Functional**
- FR1 — Orchestrator posts a set of tasks for one plan; any worker claims a *distinct*
  task (no double-work) and posts a result.
- FR2 — A task result is accepted only by a **trusted reviewer** (an `approver`); the role
  check rejects an approval posted under a non-approver identity. (For correctly-identified
  cooperating clients — the bus does not authenticate which process owns an identity; see
  the Threat model section.)
- FR3 — Orchestrator arbitrates plan convergence per an explicit policy (default: every
  task accepted by a trusted reviewer).
- FR4 — Dropped work self-heals (dead worker → task re-queued). *(reclaim, done.)*
- FR5 — Plan-level visibility: tasks `todo / claimed / in-review / accepted / rejected`.

**Non-functional**
- Throughput scales ~linearly with worker count (parallel claim).
- Durable + crash-safe (SQLite, atomic `complete`), consistent with current guarantees.
- **Additive** — must not change `decide`'s project-atomic semantics or existing roles'
  behavior. Composes with `next` / `reclaim` / `collab-loop`.
- Role checks reject approval/authority actions from non-approver identities — for
  correctly-identified cooperating clients. (The bus does not authenticate which process
  owns an identity; see the Threat model section. This is defense against accidental
  misuse, not a malicious forged identity.)

## Decision

Introduce an **orchestrated plan as a single project containing a claimable task queue**,
with an `orchestrator` role that owns plan-level convergence and a **role-checked
trusted-reviewer gate** built on the existing `approver` machinery. Keep "project =
atomic convergence unit"; the plan *is* the project. Ship it in two phases (validate the
coordination pattern with zero schema, then make it first-class).

Concretely:
- **Roles:** add `worker` (task fanout, no review authority) and `orchestrator` (sole
  caller of the plan's final `decide`). Trusted reviewers join as **`approver`**
  (`claude-1`, `codex-1`) — reusing approval-gating as the acceptance mechanism.
- **`task` message type:** work to *do* (claimable by workers), distinct from
  `review_request` (work to *review*, fanned out to trusted reviewers/approvers).
- **Acceptance:** a task is `accepted` when a trusted reviewer posts an `approval`
  referencing it; the bus rejects "acceptance" from non-approvers, enforcing FR2.
- **Convergence:** orchestrator's `decide` stays gated by the existing approver rule, now
  evaluated per-task; plan converges when policy over tasks is met.

## Options Considered

### Option A: Zero-schema — orchestrator agent + plan manifest (convention-only trust)
Plan = a manifest artifact listing task specs. A `/collab-orchestrate` skill posts each
task as a message to `broadcast`; workers claim via the existing lease; the orchestrator
*filters* responses to only accept `claude-1`/`codex-1`, tracks state in-session, and
calls `decide` when done. No bus changes.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low (skill only) |
| Cost | Ships on v0.3.8 today |
| Scalability | Good (reuses claim/lease) |
| Trust boundary | **Convention only — not enforced** |
| Durability | Weak (orchestration state in-session; rebuilt from log on crash) |

**Pros:** fastest to validate the coordination pattern; nothing to migrate; proves the
loop before committing schema.
**Cons:** fails the role-check requirement (FR2) — acceptance is only convention here, so
even a correctly-identified worker's `response` could be mis-accepted by the orchestrator;
`task` vs `review` not first-class (so
`next`/`status` can't distinguish do-work from review-work); no durable plan/task state.

### Option B: Orchestrator role + task queue + trusted-reviewer gate (single project) — RECOMMENDED
Add `worker`/`orchestrator` roles and a `task` type in one project. Trusted reviewers are
`approver`s; a task is accepted only on a trusted `approval`. Orchestrator arbitrates;
`decide` reuses approver-gating per task.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium (new roles, one type, per-task acceptance) |
| Cost | One focused change; heavy reuse of approver + claim + next |
| Scalability | Good (parallel claim; single project = one hot SQLite db) |
| Trust boundary | **Role-checked** (a non-approver identity's approval is rejected; identity trusted by convention — see Threat model) |
| Durability | Strong (task state persisted in the bus) |

**Pros:** meets FR2 by construction; `task` first-class so `next`/`status` see the queue;
convergence policy lives in the bus; scales with workers; large reuse of existing
approver machinery keeps the delta small.
**Cons:** real schema + role work and tests; convergence is still single-project
(all tasks converge together) — acceptable when the plan is one overall unit, but not if
tasks must converge independently (→ Option C). One project = one SQLite write-hot db
(fine to dozens of workers; revisit only at extreme fan-out).

### Option C: Plan over child projects (two-level, independent convergence)
A `plan` references N child projects; each task is its own project (converges
independently via today's `decide`); orchestrator rolls up per policy.

| Dimension | Assessment |
|-----------|------------|
| Complexity | High (plan registry, cross-project verbs, orchestrator spans many projects) |
| Cost | Largest |
| Scalability | Best isolation (separate dbs per task) |
| Trust boundary | Enforced per child (allowlist reviewers) |
| Durability | Strong |

**Pros:** true independent per-task convergence; strongest isolation; `decide` untouched.
**Cons:** project-per-task is heavy ceremony (start/join/broadcast overhead each) for
*interchangeable, short* tasks; overkill given the owner wants a lightweight queue on one
plan. Right choice only if tasks become long-lived/heterogeneous or need independent
sign-off.

## Trade-off Analysis

The deciding force is constraint #2. "Only claude/codex may review" is a **security/trust
requirement**, and Option A can only satisfy it by convention — the orchestrator has to be
trusted to filter correctly, and a malformed or malicious worker message can slip through.
Options B and C enforce it with a role check (a worker isn't an approver, so an approval
posted under a non-approver identity is rejected — for correctly-identified clients; the
bus doesn't authenticate identity, see Threat model). Between B and C, constraint #1
removes C's advantage: independent per-task convergence is ceremony we don't need when any
worker can do any task and the plan converges as one unit. B pays for the trust boundary
with a modest, well-reused schema delta and keeps the lightweight single-queue model the
owner asked for.

Risk of B — "single project = one convergence unit" — is acceptable because the plan *is*
the overall unit. If a future plan needs tasks that ship independently, revisit C then;
B's `task`/`orchestrator`/approver-gate concepts port forward to a child-project model.

De-risking: ship **A as Phase 0** to validate the orchestrator loop and queue dynamics
with zero schema, *then* harden into **B** once the pattern is proven. A is not the
destination (it fails the trust requirement); it's a scaffold.

## Consequences

**Easier**
- Add throughput by launching more workers — no assignment logic, they self-serve the queue.
- The trust boundary is role-checked: an approval under a non-approver identity is rejected
  (for correctly-identified clients — not defense against a forged identity; see Threat model).
- Plans become first-class and observable (`status`/`next` see tasks), so `collab-loop`
  can advance a plan hands-off without a human kick.

**Harder / to revisit**
- `next` and `status` grow a task dimension (do-work vs review-work vs orchestrate); more
  states to reason about and test.
- One hot SQLite db per plan under heavy fan-out — revisit sharding / Option C only if it
  bites.
- Orchestrator becomes a coordination SPOF; needs its own liveness story (reuse presence +
  a reclaim-style recovery so a dead orchestrator doesn't strand a plan).
- Role proliferation (`worker`, `orchestrator` on top of initiator/reviewer/approver/
  observer) — document the role matrix clearly.

## Action Items

1. [ ] **Phase 0 (validate, zero-schema):** `/collab-orchestrate` skill — post a task
       manifest to a `broadcast` queue, workers claim via lease, orchestrator accepts only
       `claude-1`/`codex-1` responses and `decide`s. Prove queue dynamics + recovery on a
       real multi-worker run. *(Trust is convention-only here — throwaway scaffold.)*
2. [ ] **Phase 1 (harden, Option B):**
   - [ ] Add `worker` + `orchestrator` to `ROLES`; define fanout (workers get `task`,
         approvers/reviewers get `review_request`).
   - [ ] Add `task` to `MSG_TYPES`; make it claimable/actionable; teach `next`/`status`
         the task dimension (`todo/claimed/in-review/accepted/rejected`).
   - [ ] Enforce FR2: task acceptance requires an `approval` from a trusted reviewer
         (approver); reject acceptance from non-approvers.
   - [ ] Plan convergence policy on `decide` (default: all tasks accepted); orchestrator
         is the sole decider.
   - [ ] Orchestrator liveness/recovery (presence + reclaim analog).
   - [ ] Tests: work-stealing across N workers, untrusted-review rejection, dead-worker
         re-queue, plan roll-up convergence, orchestrator failover.
3. [ ] Update SKILL.md role matrix + a `/collab-orchestrate` command; version bump; sync
       three `collab.py` copies; `check_version` + `plugin validate`.
4. [ ] Revisit Option C only if a future plan needs independently-shipping tasks.
```

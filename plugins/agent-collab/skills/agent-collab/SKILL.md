---
name: agent-collab
description: >-
  Collaborate with other AI agents (Codex, Copilot, Cursor, Antigravity) on a shared spec or
  codebase through a durable message bus, instead of the human copy/pasting between
  tools. Use when the user says "start collab project <X>", "start agent-collab with
  cursor", "start collab session with antigravity", "start collab session with agy",
  "join collab project <X>", "check collab project <X>", asks to get another
  agent's review through the bus, to send feedback/a rebuttal to another agent, or to
  run a multi-agent convergence loop toward a shared design. A bare invocation with no
  project, file, or reviewers named (e.g. just "agent-collab" or "set up a collab")
  runs the interactive setup wizard. The bus is the bundled `collab` CLI.
---

# agent-collab

You collaborate with other AI agents over a shared, durable message bus so a human
no longer has to relay messages between tools.

## Your identity (read this first — the #1 thing to get right)

Every participant in a project MUST have a **distinct** id, and the id must match *which
tool you are*:

- If `$COLLAB_AGENT` is set, use it verbatim.
- If it is NOT set, choose by your tool — **`claude-1` if you are Claude, `codex-1` if
  you are Codex, `copilot-1` if you are Copilot, `cursor-1` if you are Cursor,
  `antigravity-1` if you are Antigravity (agy)** — and
  tell the user which you chose.  Do **not** blindly default to `claude-1` if you are
  not Claude; that's how two tools collide on one id.

State your identity in your first reply, e.g. *"Acting as codex-1 on project X."* The
CLI reads `$COLLAB_AGENT` automatically, so you can omit `--agent`/`--from` when it's
set; otherwise pass `--agent <your-id>` explicitly.

**Before you join/post/claim, run `doctor --project X` and read its hints.** If two
tools share an id, nothing routes and `wait`/`check` is always empty — `doctor` will say
"Only one participant", and `join` will now refuse a reviewer that reuses the
initiator's id. If you hit either, STOP and fix `COLLAB_AGENT` (distinct per tool) before
continuing. This is the single most common setup failure; catch it early.

## Setup (do this first, once per request)

The bus is a single-file CLI bundled with this plugin. Define these and use them in
every call:

- `COLLAB_BIN` = path to `collab.py`. Resolve in order (first file that exists):
  1. `${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py` (Claude plugin)
  2. `$HOME/.codex/skills/agent-collab/bin/collab.py` (Codex skill copy)
  3. newest version under `$HOME/.codex/plugins/cache/agent-collab-marketplace/agent-collab/*/skills/agent-collab/bin/collab.py` (`ls | sort -V | tail -1`)
  4. newest version under `$HOME/.claude/plugins/cache/agent-collab-marketplace/agent-collab/*/skills/agent-collab/bin/collab.py` (same)
- `COLLAB_ROOT` = the data dir for the bus. **Use a local-disk path** that every
  participating agent shares; default to `$HOME/.collab` so both sides
  deterministically land on the same bus. Pass it as `--root "$COLLAB_ROOT"` on every
  command, or export it once. Avoid a mounted/synced/network folder: SQLite needs file
  locking, and the CLI will say so clearly if the path can't support it.
- Cursor watcher adapter: `cursor-exec.sh` beside `collab.py` (requires `pip install
  cursor-sdk` and `CURSOR_API_KEY`). See `references/cursor-start.md`.
- Antigravity watcher adapter: `antigravity-exec.sh` beside `collab.py` (requires `agy`
  on PATH). See `references/antigravity-start.md`.

Every command is `python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" <verb> ...`. Output is
JSON on stdout; parse it. Errors go to stderr with a non-zero exit code — read them,
don't ignore them (a `lease lost` or `idempotency key collision` error means stop and
re-evaluate, never retry blindly).

## Command reference (exact signatures — don't probe `--help`)

`--agent`/`--from` default to `$COLLAB_AGENT`; all need `--project`. Where a verb takes a
body, supply it as `--body <text>`, `--body-file <path>`, or `--body-file -` (stdin). Use
these directly:

```
review   --project X --file <path> [--name N] [--topic ..] [--goal ..] [--focus ..] [--round 1]
            # one step: create (if needed) + snapshot + broadcast. Preferred way to start.
start    --project X [--topic ..] [--goal ..] [--max-rounds 6]
artifact put --project X --name <name> --file <path> --by <agent>      # -> <name>@vN
artifact get --project X --name <name> [--version N] [--out <path>]    # prints content (or writes to --out)
post     --project X --type review_request --round 1 --artifact <name>@v1 (--body <text> | --body-file <f>) [--to broadcast]
join     --project X [--role reviewer|approver|observer]               # default reviewer
projects                                                               # list all (no --project)
status   --project X
next     --project X --agent <me>                                      # ONE recommended action for a self-paced loop
doctor   --project X                                                   # "what's wrong / next?"
poll     --project X --agent <me>                                      # peek inbox (no claim)
claim    --project X [--wait <sec>] [--poll-interval 2]                # returns claim_token + claim_message_id + body
complete --project X --claim-message <id> --claim-token <tok> --type response (--body <text> | --body-file <f>) \
            [--round N] [--to <agent>] [--parent <id>] [--thread <id>] [--artifact <name>@vN] \
            [--blob <path>] [--role <role>] [--idempotency-key <k>]   # --round optional (no default)
ack      --project X --message <id> --claim-token <tok>                # finish, no reply
extend   --project X --message <id> --claim-token <tok> [--lease-min 10]
decide   --project X [--thread <id>] (--body <text> | --body-file <f>) [--parent <id>] [--idempotency-key <k>] [--force]
            # binding; converges + clears this project's pending AND claimed inbox rows.
            # BLOCKED until every approver participant has posted an `approval`
            # message; --force converges anyway (recorded in the output).
log      --project X [--since N] [--follow]
delete   --project X --yes
watch    --project X --exec codex exec -c service_tier=fast             # hands-off reviewer loop
reclaim  --project X [--agent <id>] [--message <id>] [--force]         # recover a dead watcher's stranded claim
policy   --project X [--set any|all|final:<agent-id>]                  # show/set task acceptance policy
```

Orchestrated-plan verbs: `start --role orchestrator [--accept-policy any|all|final:<id>]`,
`join --role worker|approver`, `post --type task --to broadcast`, `policy`, `next` (worker/
orchestrator), `status.tasks`/`status.accept_policy`. See "Orchestrated multi-worker plans" below.

**When a watcher dies mid-review** (agent goes offline, process/machine killed) the
claimed inbox row is stuck in `claimed` and is INVISIBLE to `poll`/`inbox` (those show
only `pending`) — an empty pending count can hide an abandoned review. `status` surfaces
these under `in_flight` (each with an `orphaned` flag = lease expired), and `doctor`
lists `in_flight_for_you` with a recovery hint. To recover immediately instead of
waiting out the lease: `reclaim --project X --agent <id> --force` returns the row to
pending so a fresh watcher picks it up. `reclaim` without `--force` only recovers
already-expired leases (a reportable, scopeable `sweep`). Reclaim is safe if the old
watcher was only wedged: it mints no token, so the zombie's later `complete` is fenced
out (token mismatch), never a double-post.

**Self-paced plans (no manual re-kick).** To advance a multi-step, review-gated plan
hands-off, don't make the loop interpret `status` (its `open_threads` is noisy — `decide`
converges a whole project at once). Ask `next --project X --agent <me>` instead: it
collapses the board into ONE action — `reclaim` (recover an abandoned claim), `drain`
(handle your inbox), `decide` (all reviewers answered — converge/rebut), `wait` (waiting
on reviewer(s); names who and if they're offline), `done` (converged — advance to the
next step), or `broadcast` (initiator, nothing sent yet). The `/collab-loop` command runs
exactly this tick loop under `/loop`.

**Orchestrated multi-worker plans (interchangeable workers + trusted reviewers).** For a
plan where MANY interchangeable workers each do a piece and only specific agents are
trusted to accept the work, use the task-queue model (ADR-0001):

- **Roles:** the plan owner joins/starts as `orchestrator` (`start --role orchestrator`);
  the interchangeable code-doers join as `worker`; the trusted reviewers join as
  `approver` (only an approver may accept — see below). A plain `reviewer` can give
  feedback but not accept.
- **`task` type:** `post --type task --to broadcast` posts a unit of work to DO. It fans
  out to the **worker pool** (not reviewers) and is **work-stealing**: the first worker
  to `claim` it wins, the rest are preempted. If that worker dies, `sweep`/`reclaim`
  reopens it to the whole pool — any interchangeable worker re-steals it.
- **Trusted-reviewer gate (enforced):** only an `approver` may post an `approval`; a
  worker/orchestrator/plain-reviewer/non-participant is rejected by the bus. So a worker
  can never certify its own code. WHO is trusted is configurable — you pick who joins as
  `approver` (not hardcoded to any agent).
- **Acceptance policy (who's the final say):** `start --accept-policy` (or `policy --set`)
  chooses when a task is `accepted`: `any` (default — any one approver accepts), `all`
  (every approver must approve it), or `final:<agent-id>` (only that designated reviewer's
  approval accepts). Other approvers' input is then non-binding feedback.
- **Convergence:** `status.tasks` rolls each task up as `todo → claimed → submitted →
  accepted` (per the policy); `status.accept_policy` shows the rule. `decide` is blocked
  until every task is accepted (override with `--force`). `next` for a `worker` returns
  `do-task`; for the `orchestrator` it returns `broadcast`/`wait`/`reclaim`/`decide`/`done`
  off the task roll-up. `/collab-orchestrate` runs the orchestrator tick loop.

Message `--type`: `review_request question task response rebuttal proposal approval decision status heartbeat`
(the first six create inbox work; approval/decision/status/heartbeat are log-only — an
`approval` records a trusted reviewer's sign-off and is what unblocks `decide`/accepts a
`task`). Broadcast fan-out is BY TYPE: `task` → workers, review types → reviewers/approvers.

## Orient before acting (auto role detection)

When the user names a project, run `doctor --project X` first and let it tell you what
to do. It returns whether the project exists, the participants, your pending work, and
plain-language `hints` — relay those hints to the user.

- **Project doesn't exist →** you are the **initiator**. Do NOT create it from a name
  alone; first get a work product (see below).
- **Project exists and you're not in it →** you are a **reviewer**; `join` and drain
  your inbox.
- **Project exists and you're already in it →** continue your role (drain inbox, or
  reconcile responses as initiator).

So the user can just say "collab on project X" and you pick the right role.

## When the user doesn't name a project

If the user refers to a project without naming one (e.g. "join collab project",
"check my collab", "what are my collab projects"), do NOT guess or invent a name. Run
`projects` to list what exists under the current `COLLAB_ROOT`, show them as a short
numbered list (name — state, participants, last updated), and ask which one. Once they
pick, continue with the normal flow (orient with `doctor`). If the list is empty, say
so and offer to start a new project (which needs a work product).

## Interactive setup wizard (bare invocation)

When the user invokes the skill **bare** — no project name, no file, no reviewers
(e.g. just "agent-collab", "start a collab", "set up a collab session") — do not ask
one open-ended question and do not guess. Step them through setup as a short wizard,
then run the flow with their answers.

Ask in grouped rounds, not a serial interrogation. In Claude Code, use the
`AskUserQuestion` tool (one call per round, up to 4 questions; `multiSelect` where
noted). In harnesses without such a tool, ask the same rounds as compact chat messages.

**Round 0 — collaboration mode.** Ask which shape the collaboration is (skip only if the
request already makes it obvious):

- **Review** (default) — ONE work product; reviewers critique it and the initiator
  converges. This is the review wizard below.
- **Orchestrated plan** — MANY interchangeable workers each do a piece of work pulled
  from a shared task queue; trusted reviewers accept; an orchestrator converges when all
  tasks are accepted. If chosen, follow **"Wizard B: orchestrated plan"** instead of
  Rounds 1–2.

## Wizard A: review (default)

**Round 1 — the project:**

1. **Work product** — path to the file to review (spec or code). Free-text; there is
   no default. A project with nothing to review is the #1 failure mode, so this is
   mandatory.
2. **Reviewers** (multi-select) — `codex-1`, `copilot-1`, `cursor-1`,
   `antigravity-1`. Note in the option descriptions: Cursor needs
   `pip install cursor-sdk` + `CURSOR_API_KEY`; Antigravity needs `agy` on PATH.
3. **Review focus** — e.g. correctness, security, design/architecture, "tear the
   premise apart", or free-text.
4. **Onboarding mode** — (a) *hands-off watchers I launch in the background*
   (recommended; needs the reviewer CLIs installed locally), (b) *print the watcher
   commands for the user to run in their own terminals*, or (c) *interactive — the
   user will open each tool and say "review collab project X" themselves*.

Derive the project name automatically (artifact basename + short date, e.g.
`spec-review-0714`) and state it; only ask if the user objects or a project with that
name already exists.

**Round 2 — per selected agent** (one question per agent, batched into one round):
role, model, and access, phrased as one choice list per agent. Defaults first.

- **Role:** `reviewer` (default — gets inbox work and must respond), `approver`
  (a reviewer whose explicit sign-off additionally **gates `decide`** — use for a
  secondary approver whose OK is required before convergence), or `observer`
  (log-only; no inbox rows). Register non-default roles with
  `join --agent <id> --role approver|observer` **before** launching that agent's
  watcher — the watcher's auto-join keeps an existing role. Approvers work
  hands-off too: the watcher payload tells them they're an approver, and output
  whose first line is `APPROVED` is posted as an `approval` (anything else posts
  as a normal response and the gate stays closed). Only ask when it's
  plausible the user wants a non-reviewer; otherwise default everyone to reviewer
  and say so.
- **Model:** offer the default plus 1–2 known alternatives; free-text for anything
  else. Plumb the choice through the env knob when launching that agent's watcher.
- **Access:** read-only (default; reviewers should not edit the repo) or
  edit-capable (`*_READONLY=0`).

Per-agent knobs (set in the watcher's environment; defaults apply when unset):

| Agent | Model knob | Default model | Read-only knob (default on) |
|---|---|---|---|
| `codex-1` | `COLLAB_CODEX_EXEC_ARGS` — append `-m <model>` (keep `-c service_tier=fast`) | Codex CLI default | codex exec sandbox (default read-only) |
| `copilot-1` | `COPILOT_MODEL` | `gpt-5.4` | `COPILOT_READONLY` |
| `cursor-1` | `CURSOR_MODEL` | `composer-2.5` | `CURSOR_READONLY` |
| `antigravity-1` | `ANTIGRAVITY_MODEL` (alias `AGY_MODEL`) | agy picks | `ANTIGRAVITY_READONLY` (`--mode plan`) |

**Then execute** the normal initiator flow — one `review` command (create + snapshot
+ broadcast), `join --role observer` for any observers, and onboard reviewers per the
chosen mode:

- Mode (a): launch each watcher yourself as a background process, e.g.
  `COPILOT_MODEL=<choice> COLLAB_WATCH_ARGS="--idle-exit" collab-watch.sh copilot <project> <repo>`
  (one per reviewer; `--idle-exit` makes a one-shot review; omit it to keep the
  watcher alive for later rounds). Report each response as it lands.
- Mode (b): print one ready-to-paste `collab-watch.sh` line per reviewer with the
  chosen env knobs inlined.
- Mode (c): tell the user what to say in each tool ("review collab project X as
  `<agent-id>`").

Finish with the standard offer: wait for responses now (`claim --wait 600`) or come
back later with "check collab project X". Do not re-run the wizard when the user
names a project, file, or reviewer in their request — answer only what's missing.

## Wizard B: orchestrated plan

For the "many interchangeable workers + trusted reviewers" mode (see "Orchestrated
multi-worker plans" above for the mechanics). You are the **orchestrator**.

**Round 1 — the plan** (one `AskUserQuestion` round):

1. **Workers** (multi-select) — the interchangeable code-doers that pull tasks:
   `codex-1`, `copilot-1`, `cursor-1`, `antigravity-1` (and/or `claude-1`). Any agent can
   be a worker; they need not be trusted reviewers.
2. **Trusted reviewers** (multi-select) — who may **accept** work; each joins as
   `approver`. Only these can post an `approval`; a worker can never accept its own code.
3. **Acceptance policy** — whose sign-off is the final say: `any` (default — any one
   trusted reviewer accepts a task), `all` (every trusted reviewer must approve it), or
   `final:<agent-id>` (one designated final reviewer). Maps to `--accept-policy`.
4. **Onboarding mode** — same three choices as review mode (hands-off watchers you
   launch / print the commands / interactive).

**Round 2 — the tasks.** Get the task list: either free-text (one line per task) or a
path to a plan file you split into tasks. Derive the project name (e.g.
`widget-plan-0718`) and state it. (Per-agent model/access knobs are the same table as
review mode; ask only if the user wants non-defaults.)

**Then execute:**

1. `start --project <name> --role orchestrator --accept-policy <any|all|final:<id>>`.
2. `join --agent <id> --role worker` for each worker; `join --agent <id> --role approver`
   for each trusted reviewer. (Register roles BEFORE launching watchers — the watcher's
   auto-join preserves an existing role.)
3. `post --type task --to broadcast --body "<task>"` once per task. Tasks fan out to the
   worker pool and are work-stealing (first claim wins).
4. Onboard per the chosen mode: launch a watcher for each **worker** (they pull tasks) and
   each **approver** (they review/accept) — mode (a) background, (b) print commands, or
   (c) interactive. An approver's `APPROVED`-first output posts as an `approval`.
5. Hand off to the orchestrator loop: run `/collab-orchestrate <name>` (or drive it
   manually with `next --agent <name>` → `broadcast|wait|reclaim|decide|done`). Offer to
   start it now or let the user come back later.

## Intent → action

| User says | Do |
|---|---|
| bare invocation — "agent-collab" / "start a collab" / "set up a collab" (no project, file, or reviewer named) | Run the **interactive setup wizard** above. |
| "list / what are my collab projects" | Run `projects` and present the list (name, state, participants, last updated). |
| "join collab project" / "check collab" (no name) | Run `projects`, present the list, and ask which one before doing anything. |
| "start collab project X" / "collab on X" | Orient with `doctor`; if new, get a work product then start + broadcast; if it exists, join/continue. |
| "start agent-collab with cursor …" / "collab on X with cursor" | Initiator flow (you = `claude-1` or `codex-1`); after broadcast, onboard **cursor-1** via watcher or interactive Cursor session — follow `references/cursor-start.md` verbatim. |
| "start collab session with antigravity …" / "start collab with agy …" / "collab on X with antigravity" | Initiator flow (you = `claude-1` or `codex-1`); after broadcast, onboard **antigravity-1** via watcher or interactive Antigravity session — follow `references/antigravity-start.md` verbatim. |
| "check collab project X" / "any feedback?" | Drain your inbox (claim → act → complete), summarize from the `log`; if nothing's pending, OFFER to wait. |
| "wait for the review / wait for replies" | Confirm, then block with `claim --project X --wait <sec>` and handle what arrives; loop if they want. Note it ties up the session (watcher is better for walk-away). |
| "send my feedback / rebuttal to <agent>" | `post` a `rebuttal`/`response` in the existing thread. |
| "what's the status / who hasn't replied" | `status` and report state, pending per agent, stalled items, open threads. |
| "delete collab project X" | Confirm with the user, then `delete --project X --yes` (removes messages, inbox, artifacts; shared blobs are left). |
| "we're agreed / lock it in" | `decide` to post the binding decision and converge. |

## Starting a project (you are the initiator)

**A project needs something to review. If the user gave only a name, STOP and ask:**
*"What should I post for review — a file path (spec or code) — and what should reviewers
focus on?"* Do not create an empty project; it leaves reviewers with nothing and is the
#1 cause of "nothing's happening."

Once you have the work product:

1. Create the project: `start --project X --topic "…" --goal "…"` (you are `claude-1`).
2. Snapshot the work product as v1:
   `artifact put --project X --name spec.md --file <path> --by claude-1`
3. Broadcast the request for review (one row lands per reviewer; they each respond
   independently — that's the point):
   ```bash
   echo "Please review spec.md@v1. Focus on <the questions you actually want answered>." \
   | python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" post --project X --from claude-1 \
       --to broadcast --type review_request --round 1 --artifact spec.md@v1 --body-file -
   ```
4. Then **tell the user, in words, exactly what to do next** — don't just print CLI
   commands. Pick the reviewer they asked for:
   - **Codex:** *"In your Codex session say 'review collab project X', or run
     `collab-watch.sh codex X /path/to/repo`."*
   - **Copilot:** *"Run `collab-watch.sh copilot X /path/to/repo`."*
   - **Cursor:** *"Run `collab-watch.sh cursor X /path/to/repo` (needs
     `pip install cursor-sdk` + `CURSOR_API_KEY`), or in Cursor say 'review collab
     project X' as cursor-1."* — full recipe in `references/cursor-start.md`.
   - **Antigravity:** *"Run `collab-watch.sh antigravity X /path/to/repo` (or
     `collab-watch.sh agy X …`), or in Antigravity say 'review collab project X' as
     antigravity-1."* — full recipe in `references/antigravity-start.md`.
   Make sure the reviewer uses a different id (`codex-1`, `copilot-1`, `cursor-1`, or
   `antigravity-1`) and the same `COLLAB_ROOT` (`$COLLAB_ROOT`).
5. **Then ask whether to wait for feedback now:** *"Want me to wait here for the
   reviewers' responses? I'll block up to ~10 min (this ties up the session), or you can
   come back later and say 'check collab project X'."* Only wait on a yes — then
   `claim --project X --wait 600`, and when a response lands, reconcile it (proposal +
   accept/reject ledger, rebut where needed) and offer to wait for the next round.

Reviewers can join before or after the broadcast — a reviewer who joins later is
automatically backfilled the open broadcast work, so no review is dropped. If you
broadcast with no reviewers yet, `post` returns a `warning` (zero current recipients);
that's expected.

**Backfill policy:** a late joiner is backfilled *every still-open* broadcast (one
whose thread has no `decision`). If a new round's `review_request` supersedes an older
one, close the old thread with `decide --thread <old_thread_id>`, otherwise a new
reviewer gets both the stale and the current request.

## Reviewing (you are a reviewer who joined)

Drain your inbox one item at a time. For each:

1. `claim --project X --agent claude-1` → returns the message plus a `claim_token`
   and `claim_message_id`. If it returns `{"claimed": null}`, your inbox is empty.
2. Read the **exact** artifact the message references — never "the latest". Parse the
   claimed message's `refs_json` for `artifact` (e.g. `spec.md@v3`), split into name
   and version, and fetch that version:
   `artifact get --project X --name spec.md --version 3`. Read the `body` for what's
   asked. (If there's no artifact ref, the body is self-contained.)
3. Produce your review, then post it **and** ack atomically with `complete`:
   ```bash
   echo "<your review>" | python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" complete \
       --project X --from claude-1 --claim-message <claim_message_id> \
       --claim-token <claim_token> --type response --round <N> \
       --idempotency-key "claude-1:resp:<claim_message_id>:r<N>" --body-file -
   ```
   `complete` defaults routing to reply-to-sender and keeps your reply in the original
   thread — don't override unless you mean to.
4. Repeat until `claim` returns nothing.

## The convergence loop (initiator, after reviews arrive)

1. Drain: `claim` each `response` addressed to you, read it, reconcile.
2. Post a `proposal` with the next artifact version and a per-point ledger:
   `artifact put` the new version, then reply with a `proposal` whose body is a table
   of every reviewer point marked **accepted** or **rejected — with a one-line
   reason**. Stay in the same thread: `complete` the claimed response, or
   `post --parent <response_id>` (a reply inherits its parent's thread).
3. Where you reject a point, post a `rebuttal` citing the response (`--parent <id>`)
   and give the reason. Let the reviewer concede or counter.
4. If the project has **approvers**, collect their sign-off before converging: each
   approver posts an `approval` message (`post --type approval`, or
   `complete --type approval` when draining their inbox). `status`/`doctor` show
   who still owes one (`approvals`); `decide` refuses while any is missing. If the
   user explicitly wants to converge without a sign-off, use `decide --force` and
   say so in the decision body.
5. When no open disagreements remain (or you hit the round budget), converge:
   ```bash
   echo "Decision: <what was chosen and why>." | python3 "$COLLAB_BIN" \
       --root "$COLLAB_ROOT" decide --project X --from claude-1 \
       --thread <review_thread_id> --body-file -
   ```
   If the loop deadlocks or a reviewer is unresponsive, **stop and surface it to the
   user** — don't loop indefinitely.

## How to review and respond (this is the point of the tool)

The value of cross-agent review is genuine disagreement and reconciliation, not
agreement theater. Hold yourself and the loop to this:

- **As reviewer:** lead with your strongest substantive objection. If you genuinely
  agree, say specifically *why* and name the one thing you would still change.
  "Looks good" with no specifics is not an acceptable review. Point at concrete
  lines/sections and give reasons, not vibes.
- **Challenge the premise, not just the details.** You are reviewing the *goal*, not
  only the artifact as written. If you think the whole approach is wrong, say so plainly
  and propose the alternative — a fundamentally different design is a valid review, not
  out of scope. Don't confine yourself to improving the first idea if a better one
  exists.
- **As initiator:** for every piece of feedback, record accept or reject with a
  one-line reason. Do not accept a change you can't justify, and do not reject one
  just to defend your draft. When another agent is right, say so and incorporate it.
  If a reviewer proposes a different approach, engage it on its merits — don't defend
  your original by default.
- Reference artifacts as `name@version`, never "the latest" — versions are immutable.
- Keep replies in their thread so the convergence history stays coherent.

## Bringing in Codex / Copilot / Cursor / Antigravity as hands-off reviewers

Other agents don't have to be babysat. Tell the user they can run a watcher in a
separate terminal so Codex/Copilot/Cursor/Antigravity pick up review requests automatically:

```bash
# Launcher (resolves collab.py + adapter paths):
"${COLLAB_BIN%/collab.py}/collab-watch.sh" codex        X /path/to/repo
"${COLLAB_BIN%/collab.py}/collab-watch.sh" copilot      X /path/to/repo
"${COLLAB_BIN%/collab.py}/collab-watch.sh" cursor       X /path/to/repo
"${COLLAB_BIN%/collab.py}/collab-watch.sh" antigravity  X /path/to/repo
"${COLLAB_BIN%/collab.py}/collab-watch.sh" agy          X /path/to/repo

python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project X --agent codex-1   --exec codex exec -c service_tier=fast
# Copilot: prompt-as-arg + non-interactive perms; {} is replaced with the message:
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project X --agent copilot-1 --exec copilot --allow-all-tools --model gpt-5.4 -p {}
# Cursor: Cursor Agent SDK via cursor-exec.sh (stdin JSON, like Codex):
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project X --agent cursor-1 --exec "${COLLAB_BIN%/collab.py}/cursor-exec.sh"
# Antigravity: agy --print via antigravity-exec.sh (prompt-as-arg, like Copilot):
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project X --agent antigravity-1 --exec "${COLLAB_BIN%/collab.py}/antigravity-exec.sh"
```

See `references/watchers.md`, `references/cursor-start.md`, and `references/antigravity-start.md` for details.

## Watching and reporting

- Live tail: `log --project X --follow`
- Snapshot: `status --project X` → state, message count, pending per agent, stalled
  items, open threads.
- After draining, summarize for the user: what's new, who still owes a reply, whether
  you're converged, mid-reconciliation, or stalled.

## Message types (when to use which)

`review_request` (ask for review) · `question` (a specific question) · `response`
(answer/critique) · `proposal` (a revised version + accept/reject ledger) ·
`rebuttal` (disagree with a response, with reason) · `approval` (an approver's
binding sign-off — required from every approver before `decide` will converge) ·
`decision` (binding, converges the project) · `status`/`heartbeat` (informational,
no reply expected). Only the first five create work items in an inbox; approval,
decisions, and status are log-only.

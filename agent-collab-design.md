# Multi-Agent Collaboration Skill — Design & Build Plan

**Status:** Draft v3 — converged, Phase 1 green-lit by Codex pending Jeff's ratification · **Author:** Claude (with Jeff & Codex) · **Date:** 2026-06-21

> Review this the way you'd review any spec. Disagree freely — the design assumes you'll push back on the transport choice, the polling model, and the protocol before we write code.

---

## 0. Revision log — round 1 (Codex review)

This is the convergence loop run by hand on the design doc itself. Per the anti-sycophancy convention (§7.4), every point is logged accept/refine/reject with a reason.

| # | Codex point | Disposition | What changed |
|---|---|---|---|
| 1 | Broadcast claim query still selects `to_agent='broadcast'` from `messages` → first reviewer claims it away | **Accepted** | Unified `inbox` table; claim always joins `inbox`; no broadcast branch on `messages` (§6.3, §6.3.1, R4) |
| 2 | `message_id` dedupe insufficient (crash after post, before ack mints a new id) | **Accepted + strengthened** | Caller-supplied `idempotency_key` with `UNIQUE(project, idempotency_key)`; **plus** transactional response+ack on SQLite so the window doesn't exist (§6.3.1, R10) |
| 3 | R13 security overstated — local agents can write anywhere | **Accepted** | Reframed as protocol *convention* + real enforcement via per-watcher sandbox; no claim that the protocol constrains a misbehaving agent (R13) |
| 4 | Watcher `--on-message "…{prompt}…"` is shell-injection-prone | **Accepted** | Watcher runs fixed argv + feeds message via stdin/temp file; content is data, never code (§5.1) |
| 5 | "Content-addressed" but schema is version-addressed | **Accepted** | Split into immutable `blobs` (keyed by sha256) + versioned `artifacts` pointing at them; relabeled "versioned-with-hash-verification" (§6.3, R5) |
| — | Single delivery/inbox table for both modes | **Accepted** | One `inbox` table, one claim path (§6.3) |
| — | Add `round`, `parent_message_id`, `idempotency_key`; `refs.answers` too loose | **Accepted** | Added to envelope + schema; `parent_message_id` replaces `refs.answers` (§6.2) |
| — | Generate `seq` transactionally | **Accepted** | `projects.next_seq` bumped under the write txn that inserts the message (§6.3) |
| — | Move heartbeats/lease-extension earlier than Phase 5 | **Accepted** | Now in Phase 3 with the Codex watcher (§8) |
| — | Observability as `collab status` / `log --follow` in Phase 2/3 | **Accepted** | `collab status`/`log --follow` pulled into Phase 2; HTML view stays optional Phase 6 (§8) |
| — | SQLite default right; don't build Redis yet | **Accepted** | Redis adapter explicitly deferred (§4, §8) |
| — | Codex `exec` / Copilot `-p` premise checks out | **Confirmed** | Independent corroboration of §3 |

One point where I went **beyond** Codex's fix rather than just taking it: on idempotency (#2), the proposed key `(project, input_message_id, responder_agent, response_type)` would block a *legitimate* second response of the same type in a later round. I added `round` to the key so true retries dedupe but intentional follow-ups don't — and made the SQLite path transactional so the key is belt-and-suspenders there, only load-bearing on the Azurite adapter. Flag if you'd rather keep the key strictly as Codex specified.

## 0b. Revision log — round 2 (Codex re-review → Phase 1 green-lit)

Codex approved for Phase 1 after three spec fixes. All accepted.

| # | Codex point | Disposition | What changed |
|---|---|---|---|
| 1 | "No window" only holds if it's one atomic op, but Phase 1 still had separate `post`/`ack` | **Accepted** | Added atomic `collab complete` verb: verify token → insert response → mark input `done`, one txn. `post`/`ack` kept only for non-paired messages (§6.3.1, Phase 1) |
| 2 | Missing lease/fencing token — a slow handler could `ack` after its lease expired and another worker reclaimed | **Accepted** | Added `claim_token` to `inbox`, regenerated each claim, required on `ack`/`complete`/`extend` (§6.3.1, R-fencing in §10) |
| 3 | Stale §10 line still said "`message_id` dedupe" | **Accepted** | Fixed to `idempotency_key` + atomic `complete` (§10) |
| — | Idempotency rule should be "one response per reviewer per request per round" | **Accepted as the contract** | Stated explicitly in §6.3.1; `operation_id` escape hatch noted but out of scope |
| — | §9 strawman answers (transport, polling, work-product, storage, authority, roster) | **Accepted** | §9 converted from open questions to resolved decisions |

Status: **converged.** No open protocol disagreements. Awaiting Jeff's go to start Phase 1.

---

## 1. Problem & current workflow

Today you run a manual review loop across heterogeneous coding agents:

1. Claude Code drafts a technical spec (or code).
2. You start a Codex / Copilot session and paste the artifact in, asking for a review.
3. You copy Codex's feedback back to Claude.
4. Claude evaluates each point, incorporates what it agrees with, and writes a rebuttal for what it doesn't.
5. Repeat until the agents converge on a shared approach.

You are the message bus. You're also the memory, the router, and the conflict mediator. The loop works but it's slow, lossy (context gets dropped in copy/paste), and you can't step away from it.

**Goal:** Replace *you-as-the-bus* with a shared, durable message channel and a pair of skills that let any agent **start** a collaboration project, **join** one, **ask** structured questions, **review** a work product, and **respond** — while preserving the thing that makes your current loop valuable: agents genuinely disagreeing and reconciling toward a shared vision, not just rubber-stamping.

---

## 2. Enhanced requirements

Your original requirements, sharpened and extended with the parts that bite once you build it.

### 2.1 Functional (from your description)

- **Start a project:** "start collab project A" → an initiating agent creates a durable project with a topic/goal and a shared work product (a markdown spec or a code tree).
- **Join a project:** "join collab project A" → a reviewer agent attaches to the project and begins watching for work.
- **Ask:** the initiator posts structured questions/requests (idea generation, code review, design critique) and waits for responses.
- **Review/advise:** reviewers read the current work product, respond with feedback/opinions, optionally pointing at specific lines/sections.
- **Converge:** the initiator evaluates responses, incorporates what it agrees with, rebuts what it doesn't, and the loop continues until consensus or an explicit stop.

### 2.2 Requirements I'm adding (the parts that bite)

These come from experience with message-passing systems and multi-agent loops. Flag any you disagree with.

- **R1 — Stable agent identity.** Every participant has an `agent_id` (`claude-1`, `codex-1`, `copilot-1`) and a declared `role` (initiator / reviewer / observer). Without this, you can't route, attribute opinions, or detect "who hasn't responded yet."
- **R2 — Threaded, typed messages, not a flat chat.** Each message has a `type` (`question`, `review_request`, `response`, `rebuttal`, `proposal`, `decision`, `status`, `heartbeat`) and a `thread_id`. The convergence loop *is* a thread; flat chat loses the structure that makes consensus detectable.
- **R3 — Durable, ordered, replayable log.** A late-joining or restarted agent must be able to reconstruct full project state by replaying the log. This is the single most important property and it's why the transport choice matters (§4).
- **R4 — One delivery table, two routing modes.** Delivery, claim, and lease live in a single `inbox` table keyed by `(message_id, recipient)` — one code path for everything (per Codex's design note, this avoids two subtly different claim paths). The *routing* differs: a `question` addressed to one agent creates one inbox row (work-queue, single handler, lease + visibility timeout so a crashed handler's row returns to pending); a **broadcast** `review_request` creates one row per reviewer, because the entire point is independent opinions, so it is never "claimed away" by one agent. Same claim query for both.
- **R5 — Work product is versioned, with content-hash verification, not inlined.** Specs/diffs can be large and change every round. Immutable content lives in a blob store keyed by `sha256`; named **artifacts** are versioned pointers into it (`spec.md@v3 → sha256:…`). Messages reference `name@version`, never embed the content. So identity is *versioned* (human-meaningful) and *hash-verified* (tamper/confusion-proof) — Codex flagged the earlier draft for conflating the two.
- **R6 — Explicit convergence state.** A project moves through states: `open → gathering → reviewing → reconciling → converged | stalled`. "Converged" requires an explicit `decision` message, not just silence. Prevents the loop from ending ambiguously.
- **R7 — Disagreement is first-class.** A `rebuttal` must cite the `response` it answers and give a reason. The skill should *encourage* substantive disagreement and discourage sycophantic "looks good to me" — that's the whole point of cross-agent review. (Anti-sycophancy is a prompt-design requirement, see §7.4.)
- **R8 — Liveness & timeouts.** Heartbeats + per-message deadlines so the initiator can tell "reviewer is thinking" from "reviewer is gone," and can proceed or escalate to you.
- **R9 — Human-in-the-loop seam.** You can always inspect the log, inject a message, or force a decision. The agents should surface to you when they're stalled or in a genuine deadlock rather than looping forever.
- **R10 — Idempotency & at-least-once delivery.** Assume messages can be redelivered (a handler can crash *after* posting its response but *before* ack). Dedupe on a caller-supplied `idempotency_key`, not on the server-assigned `message_id` — a naive retry would mint a fresh `message_id` and defeat dedupe (Codex's catch). On SQLite we additionally make response-post + input-ack a single transaction so the window doesn't exist. Details in §6.3.1.
- **R11 — Cost/loop guardrails.** Max rounds per thread, max auto-spend, and a hard stop. Two agents can ping-pong "I respectfully disagree" forever; bound it.
- **R12 — Transport-agnostic core.** The skills talk to a thin `collab` interface, not to Azurite/Redis/SQLite directly, so the backend is swappable (§4).
- **R13 — Security & scope (convention + enforcement).** At the *protocol* level, reviewers only read the work product and only write the message log + their own artifacts; project namespacing prevents A and B crosstalking. But a local agent process can write anywhere its OS permissions allow — the protocol cannot enforce this on its own (Codex's correction; the earlier wording overstated it). Real enforcement is **per-watcher sandboxing**: run each reviewer with a restricted working dir / read-only mount of the work product / container or OS-level fs scoping. v1 ships the convention + a documented sandbox recipe for the watcher; we do not claim the protocol itself constrains a misbehaving agent.

### 2.3 Explicit non-goals (v1)

- Not a real-time streaming protocol — turn-based polling is fine and matches how these agents actually run.
- Not multi-machine / cloud-hosted in v1 — local-first, single host. (Transport adapter leaves the door open; see §4.)
- Not a replacement for git — the work product can *be* a git repo, but the bus doesn't reimplement version control.
- Not more than ~3–4 concurrent agents in v1.

---

## 3. Does Codex/Copilot support a background daemon? (verified)

You were right to question my assumption. Verified June 2026:

**Codex** can run autonomously, three relevant ways:

- `codex exec` — non-interactive, single-shot execution designed for scripting/CI. This is the key primitive: a shell loop can call `codex exec -p "check collab project A and handle pending items"` on an interval. ([Codex CLI features](https://developers.openai.com/codex/cli/features), [Codex CLI reference](https://developers.openai.com/codex/cli/reference))
- **App-server daemon / remote-control** with explicit daemon-style start/stop commands, used for managed/SSH workflows. ([Codex changelog](https://developers.openai.com/codex/changelog))
- **Background mode** for long-horizon tasks, kicked off async and polled for status — built for exactly the "run for a long time without timing out" case. ([Run long-horizon tasks with Codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex), [Background mode](https://platform.openai.com/docs/guides/background))

**Copilot CLI** is more constrained but workable: single-shot via `-p`/`--prompt` for scripting, and a **Background** session type for async local tasks (Copilot-only). Fully non-interactive `exec`/stdin piping was still a requested gap as of early 2026. ([Copilot CLI non-interactive issue #96](https://github.com/github/copilot-cli/issues/96), [Copilot CLI reference](https://ggprompts.github.io/htmlstyleguides/techguides/copilot-cli.html))

**Implication for the polling model.** We don't have to pick one. The reliable, agent-agnostic pattern is a **thin external watcher**: a small shell/Python loop *outside* the agent that polls the bus and, when a message is addressed to its agent, invokes the agent single-shot (`codex exec -p ...` / `copilot -p ...` / `claude -p ...`) with the pending item. The agent does the thinking; the watcher does the waiting. This sidesteps the "can an LLM agent reliably sit in a polling loop for 20 minutes" problem and works identically across all three tools.

We still support two lighter modes for when you don't want to run a watcher:
- **Manual poll** — you type "check collab project A" each turn; the agent drains the queue once and replies.
- **Bounded long-poll** — on "join", the agent loops polling within a single turn for N minutes (good for a focused review session you're watching).

Recommendation in §5.

---

## 4. Transport: recommendation

You named Azurite (Queues + Blob/Tables in Docker) and said you're open. Here's the trade-off and my pick.

| Option | Queue semantics | Ops burden | Portability | Azure parity | Verdict |
|---|---|---|---|---|---|
| **Filesystem** (append log + lockfiles) | Weak — no atomic claim, hand-rolled visibility | None | Perfect | None | Too fragile for R4/R10 |
| **SQLite** (single file, WAL mode) | Strong — ACID, `UPDATE…RETURNING` for atomic claim | ~None (no server) | Perfect (one file) | None | **Default core** |
| **Redis Streams** (consumer groups) | Excellent — native groups, acks, pending lists | One container | Good | None | Best if you outgrow SQLite locally |
| **Azurite** (Queues/Tables/Blob in Docker) | Good — visibility timeouts, dequeue count | One container | Good | **Exact** | **Cloud/Azure adapter** |

**Recommendation: SQLite-backed bus as the default core, behind a `collab` adapter interface, with Azurite and Redis as drop-in adapters.**

Why SQLite as default:

- **Zero daemon for the bus itself.** No container to start before agents can talk. A single `collab.db` file on a shared path. Every agent already has shell + filesystem access.
- **Real queue semantics.** WAL mode handles concurrent readers/writers; `UPDATE … WHERE status='pending' … RETURNING` gives atomic single-handler claim (R4) and dedupe (R10) for free. The filesystem option can't do this safely.
- **Replayable log (R3) is trivial** — it's just `SELECT * FROM messages ORDER BY seq`.
- **It maps cleanly onto Azurite later.** The schema (queue = table, message = row, visibility timeout = `leased_until`, artifacts = blob refs) is deliberately Azure-shaped, so the Azurite adapter is a thin re-implementation, not a redesign.

**Keep Azurite as the first alternate adapter** — it's the right target the moment you want agents on different machines or an Azure deployment, and your instinct to design Azure-shaped is correct. We just don't pay the container/setup tax during local development. Large artifacts (specs, diffs, code snapshots) live as files in a shared `artifacts/` dir (the "blob store" abstraction); in the Azurite adapter they become actual blobs.

If you specifically want to develop against Azure-parity from day one, we flip the default to the Azurite adapter — same skills, same protocol, one config change.

---

## 5. Architecture

```
                          ┌─────────────────────────────────────┐
                          │  collab bus (default: SQLite WAL)    │
                          │  ┌───────────┐  ┌──────────────────┐ │
   start/join/ask/        │  │ messages  │  │ projects (state) │ │
   review/respond  ◄────► │  │ (queue +  │  │ participants     │ │
                          │  │  log)     │  │ artifacts (refs) │ │
                          │  └───────────┘  └──────────────────┘ │
                          └───────────────▲─────────────────────┘
                                          │ collab CLI (one binary/script)
        ┌─────────────────────────────────┼─────────────────────────────────┐
        │                                 │                                  │
 ┌──────┴───────┐                 ┌───────┴───────┐                  ┌───────┴───────┐
 │ Claude skill │                 │  Codex skill  │                  │ Copilot skill │
 │ (initiator)  │                 │  (reviewer)   │                  │  (reviewer)   │
 └──────┬───────┘                 └───────┬───────┘                  └───────┬───────┘
        │ invoked by                      │ invoked by watcher /             │
        │ user turn                       │ manual poll / long-poll          │
        └─────────────────────────────────┴──────────────────────────────────┘
                         artifacts/  (shared dir = "blob store")
```

### 5.1 Components

1. **`collab` CLI** — the single integration point. One script (Python, stdlib + sqlite3) exposing verbs: `start`, `join`, `post`, `poll`, `claim`, `ack`, `artifact put/get`, `state`, `log`, `decide`. Every agent and every watcher calls this. Adapters (`--backend sqlite|azurite|redis`) implement the same verbs. This is R12.
2. **Two skills** (one shared SKILL.md, role-parameterized) — see §7. They translate natural-language intents ("start collab project A", "join collab project A", "ask the others whether the queue schema should be normalized") into `collab` CLI calls, and translate polled messages into agent actions.
3. **Optional watcher** — polls, claims, and invokes the agent single-shot per message. The portable answer to "can it run a daemon." **It never interpolates message content into a shell string** (Codex pt.4 — that risks shell-quoting bugs and prompt-injection from agent-authored messages). Instead it runs a fixed argv list and feeds the claimed message to the agent over **stdin or a temp file**:

   ```
   collab watch --agent codex-1 --backend sqlite \
     --exec codex exec --   # everything after -- is literal argv; prompt arrives on stdin
   # internally: Popen(["codex","exec"], stdin=PIPE); proc.communicate(input=message_json)
   ```

   The message body is data, never code. The watcher template is the same for `copilot -p` (stdin) and `claude -p`.

### 5.2 Recommended polling model (your call)

Default to **manual poll + optional watcher**:

- **Reviewers run the watcher** when you want hands-off: `collab watch --agent codex-1 --exec codex exec --`. Reliable, agent-agnostic, survives agent restarts. (Prompt passed via stdin, never shell-interpolated — see §5.1.)
- **Initiator (Claude) is turn-driven** — it posts questions on your turn and drains responses when you say "check collab project A" or at the start of your next message. The initiator rarely needs to sit idle; it acts when you're present.
- **Bounded long-poll** available for a focused live session.

This gives you the daemon behavior you wanted (via the watcher, which we *verified* Codex can drive with `codex exec`) without betting the protocol on any single agent's ability to loop.

---

## 6. Message protocol & data model

### 6.1 Project lifecycle

```
open ──start──► gathering ──review_request──► reviewing ──responses in──► reconciling
  │                                                                            │
  │                                                          decision/converged│
  └───────────────────────────── stalled (timeout/deadlock → surface to Jeff) ◄┘
```

### 6.2 Message envelope

```json
{
  "message_id": "uuid",            // server-assigned identity
  "idempotency_key": "string",     // caller-supplied; unique per logical write (R10) — see §6.3.1
  "project": "A",                  // namespace (R13)
  "thread_id": "uuid",             // the convergence conversation (R2)
  "round": 2,                      // explicit convergence round (R2/R6)
  "parent_message_id": "uuid",     // what this answers/rebuts — replaces loose refs.answers (R2/R7)
  "seq": 128,                      // monotonic per project, assigned transactionally by CLI (R3)
  "from": "codex-1",               // agent_id (R1)
  "to": "claude-1|broadcast",      // routing
  "role": "reviewer",
  "type": "question|review_request|response|rebuttal|proposal|decision|status|heartbeat",
  "refs": { "artifact": "spec.md@v3", "blob": "sha256:…" },  // R5; version + content hash
  "body": "markdown text",
  "created_at": "iso8601"
}
```

Delivery/lease state lives in a separate `inbox` table, not on the message — a message has one row per recipient (§6.3). `parent_message_id` replaces the looser `refs.answers` array so convergence accounting (which response a rebuttal answers, in which round) is unambiguous.

### 6.3 SQLite schema (default backend)

```sql
CREATE TABLE projects (
  name TEXT PRIMARY KEY, topic TEXT, goal TEXT,
  state TEXT NOT NULL DEFAULT 'open',
  next_seq INTEGER NOT NULL DEFAULT 1,    -- seq generator, bumped under write txn (R3)
  max_rounds INT DEFAULT 6, created_at TEXT, updated_at TEXT
);
CREATE TABLE participants (
  project TEXT, agent_id TEXT, role TEXT,
  last_heartbeat TEXT, PRIMARY KEY (project, agent_id)
);
-- immutable content store; one row per unique blob (Codex pt.5)
CREATE TABLE blobs (
  sha256 TEXT PRIMARY KEY, path TEXT, bytes INTEGER, created_at TEXT
);
-- named, versioned pointers INTO the blob store (versioned-with-hash-verification)
CREATE TABLE artifacts (
  project TEXT, name TEXT, version INTEGER, sha256 TEXT REFERENCES blobs(sha256),
  created_by TEXT, created_at TEXT,
  PRIMARY KEY (project, name, version)
);
CREATE TABLE messages (
  message_id TEXT PRIMARY KEY,
  idempotency_key TEXT,
  project TEXT, thread_id TEXT, round INTEGER, parent_message_id TEXT,
  seq INTEGER, from_agent TEXT, to_agent TEXT, role TEXT, type TEXT,
  refs_json TEXT, body TEXT, created_at TEXT,
  -- a logical write retried after a crash collapses onto the same row (R10, Codex pt.2):
  UNIQUE (project, idempotency_key)
);
-- ONE delivery model for both direct and broadcast (Codex design change #1):
-- direct msg => 1 inbox row; broadcast => 1 row per reviewer. claim/ack/lease identical.
CREATE TABLE inbox (
  message_id TEXT, recipient TEXT,
  status TEXT DEFAULT 'pending',          -- pending|claimed|done
  claimed_by TEXT, claim_token TEXT,      -- fencing token, regenerated each claim (Codex r2 #2)
  leased_until TEXT, deliveries INTEGER DEFAULT 0,
  PRIMARY KEY (message_id, recipient)
);
CREATE INDEX idx_inbox ON inbox(recipient, status);
```

#### 6.3.1 Claim, and the dual-write/idempotency story (Codex pts. 1 & 2)

Claim is now **one path** for direct and broadcast — always against `inbox`, so a broadcast is never "claimed away" from another reviewer (fixes the pt.1 inconsistency: there is no longer a `to_agent='broadcast'` branch on `messages`):

```sql
UPDATE inbox SET status='claimed', claimed_by=:me,
  claim_token=:fresh_token,               -- new token each claim; fences stale workers
  leased_until=datetime('now','+10 minutes'), deliveries=deliveries+1
WHERE rowid = (
  SELECT i.rowid FROM inbox i JOIN messages m USING (message_id)
  WHERE i.recipient=:me AND i.status='pending' AND m.project=:p
  ORDER BY m.seq LIMIT 1)
RETURNING *;
-- sweeper: status='pending' WHERE status='claimed' AND leased_until < now
```

**Lease fencing (Codex r2 #2).** Every claim mints a fresh `claim_token` returned to the worker. `ack`, `complete`, and `extend` all require `claim_token=:my_token` in their `WHERE` — so a slow handler whose lease already expired and was reclaimed by another worker can't complete or ack the row out from under the new owner; its write matches zero rows and it knows to abort.

**Crash-after-response-before-ack (Codex r1 pt.2) — and why `post`+`ack` as two verbs isn't enough (Codex r2 #1).** The SQLite "no window" guarantee only holds if it's *one* atomic operation. So the CLI exposes an atomic verb:

```
collab complete --claim-id <inbox row> --claim-token <tok> \
                --response <file|-> --type response --parent <msg> --round N
```

In one transaction it (a) verifies the token still owns the lease, (b) inserts the response message + its recipients' inbox rows, and (c) marks the input's inbox row `done`. No interleaving of a separate `post` then `ack`. (`post` and `ack` remain as standalone verbs for messages that aren't a paired reply — e.g. the initiator's opening `review_request`, heartbeats — but any *reply* uses `complete`.)

Defense in depth across backends:

1. *Atomic `complete` (primary, SQLite-only).* The window cannot occur — concrete payoff of one database over a split queue+blob service.
2. *Idempotency key (required for non-transactional backends, e.g. Azurite, which can't do cross-service transactions).* The handler computes `idempotency_key = hash(project, parent_message_id, responder_agent, type, round)` **before** posting; `UNIQUE(project, idempotency_key)` collapses a retry onto the same row.

**Idempotency rule, stated explicitly (Codex r2):** *one response of a given type, per reviewer, per parent message, per round.* The `round` component means an intentional follow-up in a later round is allowed; a true retry within the same round dedupes. If we ever need multiple distinct same-round responses from one reviewer, the key gains an explicit `operation_id` — out of scope for this workflow, where one-response-per-request-per-round is the cleaner contract.

### 6.4 Canonical convergence flow

1. Claude (initiator): `start A` → posts `review_request{round:1, refs.artifact: spec.md@v1}` as broadcast.
2. Codex watcher claims its inbox row, reads `spec.md@v1`, posts `response{parent_message_id: <review_request>, round:1}` with substantive critique.
3. Copilot does the same independently — the `review_request` was a **broadcast**, so it has one inbox row per reviewer; neither claims it away from the other (§6.3.1).
4. Claude drains responses, posts `proposal{round:2}` (spec@v2 + a per-point table: accepted / rejected-with-reason).
5. Where Claude rejects, it posts `rebuttal{parent_message_id: <codex's response>, round:2}`. Codex either concedes or counters.
6. When no open disagreements remain (or `max_rounds` hit), initiator posts `decision` → state `converged`. If deadlocked → `stalled`, surface to Jeff (R9, R11).

---

## 7. The skill(s)

### 7.1 One skill, two roles

A single `agent-collab` skill, role-parameterized, ships in three flavors of front-matter/wrapper so it triggers naturally in each tool:

- **Claude** — a Cowork/Claude Code skill (`SKILL.md`) triggered by "start/join/check collab project …".
- **Codex** — same logic packaged as a Codex prompt/skill + the watcher invocation.
- **Copilot** — same logic as a Copilot CLI custom command / `-p` prompt template.

The *protocol and CLI are identical*; only the trigger wrapper and invocation differ. This keeps behavior consistent across agents — critical, because divergent skill logic would reintroduce the lossiness we're removing.

### 7.2 Skill responsibilities

- Parse intent → map to `collab` verbs.
- On `start`: create project, register self as initiator, capture topic/goal, snapshot the work product as `artifact@v1`.
- On `join`: register as reviewer, replay log to build context, then enter chosen poll mode.
- On poll: claim → load referenced artifact → produce typed message → ack.
- Maintain heartbeats; respect `max_rounds` and stop conditions.

### 7.3 What the skill must NOT do

- Must not inline large artifacts into messages (R5).
- Must not mark a thread converged without an explicit `decision` (R6).
- Must not write outside the message log / its own artifacts (R13).

### 7.4 Anti-sycophancy prompt design (R7)

The reviewer prompt explicitly instructs: *lead with the strongest substantive objection; if you genuinely agree, say specifically why and name the one thing you'd still change; "looks good" with no specifics is not an acceptable review.* The initiator prompt instructs: *for each piece of feedback, record accept/reject with a one-line reason; do not accept changes you can't justify.* This encodes the valuable part of your manual loop — real disagreement and reconciliation — into the skill itself.

---

## 8. Phased build plan

Each phase is independently testable and leaves you with something usable.

**Phase 0 — Spec sign-off (this doc).** You review, we converge on transport (SQLite default + Azurite adapter?), polling model, and the protocol. No code until this is agreed. *Exit: you approve §2, §4, §6.*

**Phase 1 — `collab` CLI on SQLite.** Build the single-file CLI: `start/join/post/poll/claim/complete/ack/extend/artifact/state/log/decide`, WAL mode, unified `inbox` claim with `claim_token` fencing, transactional `complete` (response+ack in one txn), `next_seq` under write txn, blob/artifact store, idempotency-key dedupe, lease sweeper. Pure stdlib Python. *Exit: a test script drives a full convergence flow with two fake agents; redelivery, broadcast-fan-out, claim-collision, crash-after-post-before-ack, and stale-worker-ack (fencing) tests all pass.*

**Phase 2 — Claude skill + basic observability.** `SKILL.md` + intent mapping; plus `collab status` and `collab log --follow` (Codex's ask to pull observability earlier — it's how you'll debug everything after this). Drive a real loop with Claude playing both roles. *Exit: "start collab project A" through "decision" works in one Claude session simulating two participants, watchable via `collab status`.*

**Phase 3 — Codex skill + watcher + liveness.** Package the prompt; build the stdin-fed `collab watch` (§5.1) invoking `codex exec`. Ship heartbeats + lease-extension now — moved up from Phase 5 because a real Codex review will exceed a 10-min lease and must heartbeat-extend it (Codex's catch). *Exit: Claude in one terminal, Codex watcher in another, a spec converges with zero copy/paste; a >10-min review does not get falsely redelivered.*

**Phase 4 — Copilot skill.** Same via `copilot -p` (stdin), accounting for its non-interactive limits. *Exit: three-agent loop.*

**Phase 5 — Hardening + Azurite adapter.** Timeouts, deadlock→stall surfacing, cost/round guardrails, per-watcher sandbox recipe (R13); then the Azurite adapter behind the same CLI for multi-machine/Azure — this is where the idempotency key earns its keep, since Azurite can't do the transactional response+ack. *Exit: `--backend azurite` passes the Phase 1 test suite unchanged.* (Redis adapter intentionally deferred — build only if a real need appears, per Codex.)

**Phase 6 (optional) — Rich observability.** A live HTML artifact over `collab status` showing project state, threads, who's pending, and the disagreement ledger.

---

## 9. Resolved decisions (Claude rec + Codex strawman, agreed)

These were open questions; the cross-agent loop converged on all six. Jeff to ratify.

1. **Transport:** SQLite default, Azurite adapter later. No Azure-parity tax during v1.
2. **Polling:** manual-poll + *optional* watcher. Always-on is never mandatory.
3. **Work product:** support both, but Phase 1 optimizes for **markdown specs and git diffs**, not full-repo snapshotting.
4. **Storage location:** workspace-local `.collab/` dir per project (holds `collab.db` + `artifacts/`). No global `~/.collab` in v1 — keeps R13 scoping tight; revisit only if you want cross-repo projects.
5. **Convergence authority:** **initiator owns the final `decision`**; reviewers advise; Jeff can override at any time (R9). No quorum/voting in v1.
6. **Roster:** **fixed roster per project** for v1. Open join complicates broadcast fan-out and pending-response accounting; defer.

Still genuinely needs your sign-off: nothing blocking — these six are agreed defaults. Say the word and Phase 1 starts.

---

## 10. Risks

- **Infinite disagreement loops** — bounded by `max_rounds` + cost guardrail + stall→human (R11, R9).
- **At-least-once double-posts** — handled by `idempotency_key` dedupe + the atomic `complete` verb (R10, §6.3.1); skills must compute the key before posting.
- **Lease handed to the wrong worker** — a slow handler whose lease expired must not `ack`/`complete` a row another worker has reclaimed; fenced by `claim_token` (§6.3.1).
- **Copilot non-interactive gaps** — may need the Background session type or a thin wrapper; Phase 4 de-risks this last.
- **Lease expiry mid-review** — a long Codex review could exceed the 10-min lease; heartbeat-extend the lease during long work (shipped Phase 3).
- **Stale artifact references** — always reference `name@version`; never "the latest spec."

---

### Sources

- [Codex CLI — features](https://developers.openai.com/codex/cli/features)
- [Codex CLI — command reference](https://developers.openai.com/codex/cli/reference)
- [Codex CLI — overview](https://developers.openai.com/codex/cli)
- [Codex — changelog](https://developers.openai.com/codex/changelog)
- [Run long-horizon tasks with Codex](https://developers.openai.com/blog/run-long-horizon-tasks-with-codex)
- [OpenAI API — Background mode](https://platform.openai.com/docs/guides/background)
- [Copilot CLI — non-interactive exec request (issue #96)](https://github.com/github/copilot-cli/issues/96)
- [GitHub Copilot CLI — reference guide](https://ggprompts.github.io/htmlstyleguides/techguides/copilot-cli.html)
- [Claude and Codex available for Copilot Business & Pro](https://github.blog/changelog/2026-02-26-claude-and-codex-now-available-for-copilot-business-pro-users/)

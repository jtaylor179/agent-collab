---
name: agent-collab
description: >-
  Collaborate with other AI agents (Codex, Copilot) on a shared spec or codebase
  through a durable message bus, instead of the human copy/pasting between tools.
  Use when the user says "start collab project <X>", "join collab project <X>",
  "check collab project <X>", asks to get another agent's review through the bus,
  to send feedback/a rebuttal to another agent, or to run a multi-agent
  convergence loop toward a shared design. The bus is the `collab` CLI.
---

# agent-collab

You collaborate with other AI agents over a shared, durable message bus so a human
no longer has to relay messages between tools. Your stable identity on the bus is
**`claude-1`** unless the user specifies otherwise.

## Setup (do this first, once per request)

The bus is a single-file CLI. Locate it and the data dir, then use them in every call:

- `COLLAB_BIN` = path to `collab.py` (typically `collab/collab.py` under the project root).
- `COLLAB_ROOT` = data dir for the bus, default `.collab` in the project root. Pass
  it as `--root "$COLLAB_ROOT"` on every command (or export `COLLAB_ROOT`).

Find the CLI if you don't know the path:

```bash
ls collab/collab.py 2>/dev/null || find . -name collab.py -path '*/collab/*' | head -1
```

Every command is `python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" <verb> ...`. Output is
JSON on stdout; parse it. Errors go to stderr with a non-zero exit code — read them,
don't ignore them (a `lease lost` or `idempotency key collision` error means stop and
re-evaluate, never retry blindly).

## Intent → action

| User says | Do |
|---|---|
| "start collab project X [about …]" | `start` the project as initiator, snapshot the work product as an artifact, broadcast a `review_request`. |
| "join collab project X" | `join` as reviewer, replay the `log` to build context, then drain your inbox. |
| "check collab project X" / "any feedback?" | Drain your inbox (claim → act → complete), then summarize new activity from the `log`. |
| "send my feedback / rebuttal to <agent>" | `post` a `rebuttal`/`response` in the existing thread. |
| "what's the status / who hasn't replied" | `status` and report state, pending per agent, open threads. |
| "we're agreed / lock it in" | `decide` to post the binding decision and converge. |

## Starting a project (you are the initiator)

1. Capture topic and goal from the user.
2. Create the project first (the project must exist before artifacts can attach):
   `start --project X --topic "…" --goal "…" --agent claude-1`
3. Snapshot the work product (the spec/code under discussion) as v1:
   `artifact put --project X --name spec.md --file <path> --by claude-1`
4. Broadcast the request for review (one row lands per reviewer; they each respond
   independently — that's the point):
   ```bash
   echo "Please review spec.md@v1. Focus on <the questions you actually want answered>." \
   | python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" post --project X --from claude-1 \
       --to broadcast --type review_request --round 1 --artifact spec.md@v1 --body-file -
   ```
5. Tell the user it's posted and how to watch: `log --project X --follow`.

Reviewers can join before or after the broadcast — a reviewer who joins later is
automatically backfilled the open broadcast work, so no review is dropped. If you
broadcast while no reviewers have joined yet, `post` returns a `warning` (zero current
recipients); that's expected, and they'll pick it up on join.

**Backfill policy (important):** a late joiner is backfilled *every still-open*
broadcast — i.e. every broadcast whose thread has no `decision`. So if you issue a new
round's `review_request` that supersedes an earlier one, close the old thread with a
`decide --thread <old_thread_id>` (or keep replies in the same thread), otherwise a
new reviewer will be handed both the stale and the current request. Closing superseded
threads is how you tell the bus "this one no longer needs review."

## Reviewing (you are a reviewer who joined)

Drain your inbox one item at a time. For each:

1. `claim --project X --agent claude-1` → returns the message plus a `claim_token`
   and `claim_message_id`. If it returns `{"claimed": null}`, your inbox is empty.
2. Read the **exact** artifact the message references — never "the latest". Parse the
   claimed message's `refs_json` for `artifact` (e.g. `spec.md@v3`), split it into name
   and version, and fetch that version:
   `artifact get --project X --name spec.md --version 3`. Read the message `body` for
   what's being asked. (If there's no artifact ref, the body is self-contained.)
3. Produce your review, then post it **and** ack atomically with `complete`:
   ```bash
   echo "<your review>" | python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" complete \
       --project X --from claude-1 --claim-message <claim_message_id> \
       --claim-token <claim_token> --type response --round <N> \
       --idempotency-key "claude-1:resp:<claim_message_id>:r<N>" --body-file -
   ```
   `complete` defaults routing to reply-to-sender and keeps your reply in the
   original thread — don't override unless you mean to.
4. Repeat until `claim` returns nothing.

## The convergence loop (initiator, after reviews arrive)

1. `check`/drain: `claim` each `response` addressed to you, read it, and reconcile.
2. Post a `proposal` with the next artifact version and a per-point ledger:
   `artifact put` the new version, then reply with a `proposal` whose body is a table of
   every reviewer point marked **accepted** or **rejected — with a one-line reason**.
   Stay in the same thread: either `complete` the claimed response (it keeps the thread
   automatically), or `post --parent <response_id>` (a reply inherits its parent's
   thread; add `--thread <root_thread_id>` if you want to be explicit).
3. Where you reject a point, post a `rebuttal` citing the response (`--parent <id>`)
   and give the reason. Let the reviewer concede or counter.
4. When no open disagreements remain (or you hit the round budget), converge:
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
- **As initiator:** for every piece of feedback, record accept or reject with a
  one-line reason. Do not accept a change you can't justify, and do not reject one
  just to defend your draft. When another agent is right, say so and incorporate it.
  When you improve on their fix, say how.
- Reference artifacts as `name@version`, never "the latest" — versions are immutable.
- Keep replies in their thread so the convergence history stays coherent.

## Watching and reporting

- Live tail: `log --project X --follow`
- Snapshot: `status --project X` → state, message count, pending per agent, open threads.
- After draining, summarize for the user: what's new, who still owes a reply, and
  whether you're converged, mid-reconciliation, or stalled.

## Message types (when to use which)

`review_request` (ask for review) · `question` (a specific question) · `response`
(answer/critique) · `proposal` (a revised version + accept/reject ledger) ·
`rebuttal` (disagree with a response, with reason) · `decision` (binding, converges
the project) · `status`/`heartbeat` (informational, no reply expected). Only the
first five create work items in someone's inbox; decisions and status are log-only.

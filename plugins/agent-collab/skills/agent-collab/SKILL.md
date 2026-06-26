---
name: agent-collab
description: >-
  Collaborate with other AI agents (Codex, Copilot) on a shared spec or codebase
  through a durable message bus, instead of the human copy/pasting between tools.
  Use when the user says "start collab project <X>", "join collab project <X>",
  "check collab project <X>", asks to get another agent's review through the bus,
  to send feedback/a rebuttal to another agent, or to run a multi-agent
  convergence loop toward a shared design. The bus is the bundled `collab` CLI.
---

# agent-collab

You collaborate with other AI agents over a shared, durable message bus so a human
no longer has to relay messages between tools.

## Your identity (read this first — the #1 thing to get right)

Every participant in a project MUST have a **distinct** id, and the id must match *which
tool you are*:

- If `$COLLAB_AGENT` is set, use it verbatim.
- If it is NOT set, choose by your tool — **`claude-1` if you are Claude, `codex-1` if
  you are Codex, `copilot-1` if you are Copilot** — and tell the user which you chose.
  Do **not** blindly default to `claude-1` if you are not Claude; that's how two tools
  collide on one id.

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

- `COLLAB_BIN` = `${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py`
- `COLLAB_ROOT` = the data dir for the bus. **Use a local-disk path** that every
  participating agent shares; default to `$HOME/.collab` so both sides
  deterministically land on the same bus. Pass it as `--root "$COLLAB_ROOT"` on every
  command, or export it once. Avoid a mounted/synced/network folder: SQLite needs file
  locking, and the CLI will say so clearly if the path can't support it.

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
join     --project X
projects                                                               # list all (no --project)
status   --project X
doctor   --project X                                                   # "what's wrong / next?"
poll     --project X --agent <me>                                      # peek inbox (no claim)
claim    --project X [--wait <sec>] [--poll-interval 2]                # returns claim_token + claim_message_id + body
complete --project X --claim-message <id> --claim-token <tok> --type response (--body <text> | --body-file <f>) \
            [--round N] [--to <agent>] [--parent <id>] [--thread <id>] [--artifact <name>@vN] \
            [--blob <path>] [--role <role>] [--idempotency-key <k>]   # --round optional (no default)
ack      --project X --message <id> --claim-token <tok>                # finish, no reply
extend   --project X --message <id> --claim-token <tok> [--lease-min 10]
decide   --project X [--thread <id>] (--body <text> | --body-file <f>) [--parent <id>] [--idempotency-key <k>]
            # binding; converges + clears this project's pending AND claimed inbox rows
log      --project X [--since N] [--follow]
delete   --project X --yes
watch    --project X --exec codex exec                                 # hands-off reviewer loop
```

Message `--type`: `review_request question response rebuttal proposal decision status heartbeat`
(only the first five create inbox work; decision/status/heartbeat are log-only).

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

## Intent → action

| User says | Do |
|---|---|
| "list / what are my collab projects" | Run `projects` and present the list (name, state, participants, last updated). |
| "join collab project" / "check collab" (no name) | Run `projects`, present the list, and ask which one before doing anything. |
| "start collab project X" / "collab on X" | Orient with `doctor`; if new, get a work product then start + broadcast; if it exists, join/continue. |
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
   commands. For example: *"Posted. Now bring in a reviewer: in your Codex session say
   'review collab project X', or run the watcher `… watch --project X --agent codex-1
   --exec codex exec`."* Make sure the reviewer uses a different id (`codex-1`) and the
   same `COLLAB_ROOT` (`$COLLAB_ROOT`).
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

## Bringing in Codex / Copilot as hands-off reviewers

Other agents don't have to be babysat. Tell the user they can run a watcher in a
separate terminal so Codex/Copilot pick up review requests automatically:

```bash
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project X --agent codex-1   --exec codex exec
# Copilot: prompt-as-arg + non-interactive perms; {} is replaced with the message:
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project X --agent copilot-1 --exec copilot --allow-all-tools --model gpt-5.4 -p {}
```

See `references/watchers.md` for the details (timeouts, retries, the stdin payload).

## Watching and reporting

- Live tail: `log --project X --follow`
- Snapshot: `status --project X` → state, message count, pending per agent, stalled
  items, open threads.
- After draining, summarize for the user: what's new, who still owes a reply, whether
  you're converged, mid-reconciliation, or stalled.

## Message types (when to use which)

`review_request` (ask for review) · `question` (a specific question) · `response`
(answer/critique) · `proposal` (a revised version + accept/reject ledger) ·
`rebuttal` (disagree with a response, with reason) · `decision` (binding, converges
the project) · `status`/`heartbeat` (informational, no reply expected). Only the
first five create work items in an inbox; decisions and status are log-only.

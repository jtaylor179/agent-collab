# AGENTS.md — agent-collab (Codex / Claude initiator & reviewer instructions)

You collaborate with other AI agents (Codex, Copilot, **Cursor**, **Antigravity**) over a shared bus.
As **initiator** you are `claude-1` (Claude) or `codex-1` (Codex). Reviewers use a
**different** id: `codex-1`, `copilot-1`, **`cursor-1`**, or **`antigravity-1`**.

## Starting with Antigravity as reviewer

When the human says **"start collab session with antigravity …"** or **"start collab with
agy …"**, follow `skills/agent-collab/references/antigravity-start.md` after the normal
`start` → `artifact put` → `post review_request` flow. Prerequisites for the human:
`agy` on PATH, `COLLAB_ROOT=$HOME/.collab`.

Then give them **one** of:
- `collab-watch.sh antigravity <project> <repo-dir>` (or `agy`), or
- In Antigravity chat: *"review collab project &lt;X&gt; as antigravity-1"* (see `ANTIGRAVITY.md`).

## Starting with Cursor as reviewer

When the human says **"start agent-collab with cursor …"** or **"collab on X with
cursor"**, follow `skills/agent-collab/references/cursor-start.md` after the normal
`start` → `artifact put` → `post review_request` flow. Prerequisites for the human:
`pip install cursor-sdk`, `export CURSOR_API_KEY=...`, `COLLAB_ROOT=$HOME/.collab`.

Then give them **one** of:
- `collab-watch.sh cursor <project> <repo-dir>` (background terminal), or
- In Cursor chat: *"review collab project &lt;X&gt; as cursor-1"* (see `CURSOR.md`).

## Your identity (reviewer mode)

You act as **`codex-1`** (or `copilot-1` / **`cursor-1`** / **`antigravity-1`**) unless `$COLLAB_AGENT` is set, in
which case use that. **Set it explicitly: `export COLLAB_AGENT=codex-1` as your first
action if it's unset** — do not inherit a `claude-1` default from a shared skill file.
Your id MUST differ from every other participant's; the initiator is usually `claude-1`
or `codex-1`.
If you and another agent share an id, nothing routes to you and "check"/"wait" always
find nothing (the #1 setup mistake). Say your identity in your first reply, e.g.
*"Acting as codex-1 on project X."*

**Before you join/claim, run `doctor --project X`.** If it reports only one participant
after you'd expect two, or `join` refuses you because your id is already the initiator,
STOP — your `COLLAB_AGENT` collides with the other tool. Fix it (use `codex-1`), then
retry. The CLI reads `$COLLAB_AGENT` automatically once set.

> Copy this file to your repo root (or `~/.codex/AGENTS.md`) so Codex loads it, or rely
> on it being read from the installed plugin. Copilot users: add the same content to
> your custom instructions.

## The bus

The bus is a single-file Python CLI bundled with this plugin at
`skills/agent-collab/bin/collab.py`. Set:

- `COLLAB_BIN` = absolute path to `collab.py` (resolve per `agent-collab` SKILL.md)
- `COLLAB_ROOT` = the **same** local-disk path the initiator uses (e.g.
  `$HOME/.collab`). Ask the human if unsure. It must match exactly, or you're
  looking at a different (empty) bus.
- `COLLAB_AGENT` = `codex-1` (recommended: `export COLLAB_AGENT=codex-1` once).

Every command is `python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" <verb> ...`, JSON on
stdout. On a `lease lost` or `idempotency key collision` error, stop and re-evaluate;
never retry blindly.

## Orient first

Run `doctor --project X` and relay its `hints`. If it says the project doesn't exist or
you're pointed at the wrong root, fix that before anything else. Then:

## When the human says "join / review collab project X"

1. `join --project X` (you're registered as a reviewer and backfilled any open review
   requests automatically). After joining, tell the human you're watching and they can
   continue in their initiator session.
2. Drain your inbox, one item at a time:
   - `claim --project X --agent codex-1` → returns the message + a `claim_token` and
     `claim_message_id`. `{"claimed": null}` means nothing pending.
   - Read the **exact** artifact the message references — parse `refs_json.artifact`
     (e.g. `spec.md@v3`), then `artifact get --project X --name spec.md --version 3`.
     Never read "the latest"; versions are immutable.
   - Write your review, then post it and ack atomically:
     ```bash
     echo "<your review>" | python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" complete \
        --project X --from codex-1 --claim-message <claim_message_id> \
        --claim-token <claim_token> --type response --round <N> \
        --idempotency-key "codex-1:resp:<claim_message_id>:r<N>" --body-file -
     ```
   - Repeat until `claim` returns nothing.
3. Tell the human what you reviewed and your key points.

## Review discipline (the whole point)

Cross-agent review is valuable only when it's honest. Lead with your strongest
substantive objection. If you genuinely agree, say specifically *why* and name the one
thing you would still change. "Looks good" with no specifics is not an acceptable
review. Cite concrete lines/sections and give reasons. When you receive a rebuttal,
either concede with a reason or counter with one.

You are reviewing the **goal**, not just the artifact as written. If you think the whole
approach is wrong, say so plainly and propose the alternative — a fundamentally
different design is a valid review, not out of scope. Don't limit yourself to refining
the first idea if a better one exists.

## Acting as approver (secondary sign-off)

The human may register you as an **approver** (`join --project X --agent <you> --role
approver`) instead of a plain reviewer. You receive the same review work, but the
initiator **cannot converge (`decide`) until you post an `approval` message** — your
sign-off is the gate.

- Review exactly as usual. While you still have objections, post them as a normal
  `response`/`rebuttal` — do NOT approve.
- When you are satisfied, sign off explicitly:
  ```bash
  echo "APPROVED — <why you are satisfied>" | python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" \
     post --project X --from <you> --type approval --body-file -
  ```
  or, when draining a claimed item, `complete ... --type approval`.
- `doctor --project X` tells you if you are a missing approver. The initiator can
  override the gate only with an explicit `decide --force` (it's recorded).

## Hands-off mode (no human in the loop)

The human can instead run a watcher so you're invoked automatically:

```bash
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project X --agent codex-1 --exec codex exec -c service_tier=fast
# Cursor:
collab-watch.sh cursor X /path/to/repo
# Antigravity:
collab-watch.sh antigravity X /path/to/repo
```

In that mode the bus feeds you each claimed message (instructions + the message + the
exact artifact content) on stdin; you write ONLY your review to stdout.

**Hands-off approver:** if you were joined with `--role approver`, the payload's
instructions say so. To sign off, make the FIRST line of your output exactly
`APPROVED` (then your reasoning) — the watcher posts it as an `approval`, which is
what unblocks the initiator's `decide`. Any other output is posted as a normal
response and the gate stays closed. The marker is ignored for plain reviewers.

To stay in THIS interactive session and keep pulling work without being re-prompted,
use a blocking claim and loop: `claim --project X --wait 600` blocks until a message
arrives (or times out), then you read the artifact, `complete` your review, and claim
again. (The external watcher is better for true hands-off; `--wait` is for when you
want to remain in one session.)

## Message types

`review_request`, `question`, `response`, `rebuttal`, `proposal` create inbox work;
`approval` (an approver's binding sign-off — gates `decide`), `decision`, `status`,
`heartbeat` are informational (no reply expected).

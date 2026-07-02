# agent-collab demo

A self-contained way to try the multi-agent collaboration bus. Two agents (Claude as
initiator, Codex as reviewer) converge on a PRD through a shared message bus instead of
you copy/pasting between them.

```
demo/
├── bin/                 the bundled collab CLI (collab.py) + a fake reviewer for offline tests
├── prd.md               the example PRD to review (has a deliberate flaw for the reviewer to catch)
├── run_demo.sh          full loop, no external agents needed — proves the bus end to end
├── run_watcher_demo.sh  hands-off watcher demo (uses the fake reviewer; swap in real Codex)
└── README.md            this file
```

## Prerequisites

- `python3` (standard library only — nothing to install).
- For the real two-agent run: Claude Code and the Codex CLI installed.
- **`COLLAB_ROOT` must be on a local disk.** SQLite needs file locking, which some
  mounted/synced/network folders don't support. The scripts default to a local dir
  inside `demo/`; if you ever see a "does not support SQLite file locking" error, set
  `export COLLAB_ROOT="$HOME/.collab"` and re-run.

> Cleanup note: if this folder has leftover `.collab*`, `_probe`, or stray `.txt`/`.md`
> scratch files from earlier, remove them with `rm -rf demo/.collab demo/.collab2
> demo/_probe demo/*.txt` — they aren't part of the demo.

---

## Option A — instant check (no other agents)

Just confirm the whole loop works:

```bash
cd demo
./run_demo.sh            # scripts both roles with real content; prints the conversation
./run_watcher_demo.sh    # shows the hands-off watcher driving a reviewer automatically
```

You should see the project reach `state = converged` with a 4-message thread:
`review_request → response → proposal → decision`.

---

## Option B — the real thing: Claude Code + Codex

This is the workflow the tool exists for. Open **two terminals** in this `demo/`
folder, both pointed at the same bus:

```bash
export COLLAB_ROOT="$HOME/.collab"     # run this in BOTH terminals
BIN="$(pwd)/bin/collab.py"                  # both terminals; assumes you're in demo/
```

### Terminal 1 — Claude Code (initiator)

Start Claude Code and paste this prompt:

> You're collaborating with another agent (`codex-1`) over the collab bus. The CLI is at
> `./bin/collab.py` and `COLLAB_ROOT` is already set in my environment — always pass
> `--root "$COLLAB_ROOT"`. Do this:
> 1. Start a collab project called `demo` (topic: "API rate limiting", goal: "agree on
>    the algorithm"), as agent `claude-1`.
> 2. Snapshot `./prd.md` as artifact `prd.md`.
> 3. Broadcast a `review_request` (round 1) referencing `prd.md@v1` asking codex to
>    answer the three open questions in the PRD.
> Then stop and tell me it's posted.

After Codex has reviewed (Terminal 2), continue in Claude Code:

> Check collab project `demo`: claim and read codex-1's response, then post a `proposal`
> (round 2) with a revised `prd.md@v2` and a per-point ledger marking each of codex's
> points accepted or rejected **with a reason**. Keep it in the same thread. Then, if
> there are no open disagreements, post the binding `decision` and converge the project.
> Finally show me the full `log`.

### Terminal 2 — Codex (reviewer)

The repo ships an `AGENTS.md` here so Codex knows the protocol. Either:

**Hands-off (recommended)** — one command; Codex auto-reviews whatever's pending:

```bash
python3 "$BIN" --root "$COLLAB_ROOT" watch --project demo --agent codex-1 --exec codex exec -c service_tier=fast
```

**Or manual** — start `codex` in this folder and paste:

> Read `AGENTS.md`. You are `codex-1` on the collab bus; the CLI is `./bin/collab.py` and
> `COLLAB_ROOT` is set (always pass `--root "$COLLAB_ROOT"`). Join collab project `demo`,
> claim the pending review request, read the exact `prd.md` version it references off the
> bus, and post a substantive `response` — lead with your strongest objection. Then tell
> me what you found.

### What you'll see

Codex should catch the planted flaws in the PRD (fixed-window doubles the cap at window
boundaries; the `INCR`/`EXPIRE` race), Claude should accept those and rebut the
per-endpoint point with a reason, and the project converges on a token-bucket design —
all on the bus, no copy/paste.

Watch it live from a third terminal any time:

```bash
python3 "$BIN" --root "$COLLAB_ROOT" log --project demo --follow
python3 "$BIN" --root "$COLLAB_ROOT" status --project demo
```

---

## Reset between runs

```bash
rm -rf "$COLLAB_ROOT"        # or demo/.run, demo/.run-watcher for the scripted demos
```

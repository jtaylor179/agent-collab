# agent-collab — Quickstart

The goal: Claude (initiator) and Codex (reviewer) converge on a spec/PRD over a shared
bus, with no copy/pasting. Two rules make it "just work":

1. **Give a file to review** — not just a project name.
2. **Each agent gets a distinct identity and the SAME bus root.**

---

## Simplest path — slash commands (recommended)

This is the everyday flow. Set identity once per environment, then drive it with slash
commands and plain language.

**Once per terminal** (inside Claude Code / Codex):

```bash
export COLLAB_AGENT=claude-1     # in CLAUDE ;  use codex-1 in the CODEX terminal
export COLLAB_ROOT="$HOME/.collab"
```

**1. In Claude — start a review of a file:**

```
/collab-review ./path/to/spec.md
```

(or `/collab-start` and answer its questions). It snapshots the file, broadcasts the
review request, and then **asks if you want to wait** for feedback.

**2. Bring in Codex as the reviewer** — in your Codex session, either say:

```
review collab project <name>
```

…or run the hands-off watcher in a terminal so Codex auto-reviews:

```bash
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project <name> --agent codex-1 --exec codex exec
```

**3. Back in Claude — get the feedback.** Either answer "yes, wait" to the offer from
step 1, or run:

```
/collab-wait
```

It blocks until Codex's review lands, then reconciles it (proposal + accept/reject
ledger) and, once you're agreed, converges. Or skip waiting and just run `/collab-check`
whenever you're ready — the response sits safely on the bus until you do.

**Manage projects:** `/collab-list` (all projects), `/collab-status` (one project's
state), `/collab-delete` (remove one).

> The slash commands set `COLLAB_BIN` automatically inside Claude/Codex. You only need
> the terminal `COLLAB_BIN` export below for the raw-CLI watcher / status commands.

---

## Power-user detail — prompts + raw CLI

If you'd rather drive it explicitly (or from a plain terminal), here's the long form.

## One-time setup (run in BOTH terminals)

```bash
export COLLAB_ROOT="$HOME/.collab"   # local disk, identical in both
export COLLAB_AGENT="claude-1"                  # in the CLAUDE terminal
# export COLLAB_AGENT="codex-1"                 # in the CODEX terminal instead

# point COLLAB_BIN at the bundled CLI (needed for the terminal commands below).
# inside Claude/Codex the skill finds it automatically; in a plain terminal, set it:
export COLLAB_BIN="$(find "$HOME/.claude/plugins" "$HOME/.codex/skills" -name collab.py -path '*agent-collab*' 2>/dev/null | head -1)"
echo "$COLLAB_BIN"   # sanity-check it printed a path
```

(`COLLAB_ROOT` must be a local-disk path — a synced/network folder can't do SQLite
locking. If `COLLAB_BIN` is empty, the plugin isn't installed where the `find` looked —
use the absolute path to `plugins/agent-collab/skills/agent-collab/bin/collab.py` from
your checkout.)

## Terminal 1 — Claude (initiator)

Paste:

> Acting as claude-1. Start a collab project `myproject` to review `./path/to/spec.md`,
> goal: <what you want decided>. Snapshot the file and broadcast a review request to
> codex-1, then tell me exactly what to do next.

After Codex has reviewed, paste:

> Check collab project `myproject`: claim codex-1's response, post a `proposal` (v2) with
> an accept/reject ledger (one reason per point) in the same thread, rebut anything you
> disagree with, and if there are no open disagreements, `decide` and converge. Show me
> the full log.

## Terminal 2 — Codex (reviewer)

**Hands-off (recommended)** — one command; auto-reviews whatever's pending:

```bash
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project myproject --agent codex-1 --exec codex exec
```

**Or interactively** — start `codex` and paste:

> Acting as codex-1. Read AGENTS.md. Join collab project `myproject`, claim the pending
> review request, read the exact artifact version it references off the bus, and post a
> substantive `response` — lead with your strongest objection. Tell me what you found.

## Watch / check anytime (any terminal)

```bash
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" status  --project myproject
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" doctor  --project myproject   # "what do I do next?"
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" log     --project myproject --follow
```

## If "nothing's happening"

Run `doctor` — it almost always tells you why. The usual causes:

- **Both agents share one id** (e.g. both `codex-1`): set `COLLAB_AGENT` distinctly.
- **Different `COLLAB_ROOT`** in each terminal: they're on different buses; make them equal.
- **Project started from a name with no file**: there's nothing to review; start over with a work product.

# Starting agent-collab with Antigravity as reviewer

When the human says **"start agent-collab with antigravity …"** or **"start collab
session with agy …"** (from Claude or Codex), you are the **initiator** (`claude-1` or
`codex-1`). Antigravity is the **reviewer** (`antigravity-1`). Run the normal start
flow, then give the human **both** onboarding paths below.

## Prerequisites (tell the human once)

```bash
# agy must be on PATH (install via Antigravity CLI docs)
export COLLAB_ROOT="$HOME/.collab"
```

## Resolve paths (works from Claude or Codex)

```bash
export COLLAB_ROOT="${COLLAB_ROOT:-$HOME/.collab}"
for _p in \
  "${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/agent-collab/bin/collab.py}" \
  "$HOME/.codex/skills/agent-collab/bin/collab.py" \
  "$(ls -d "$HOME/.codex/plugins/cache/agent-collab-marketplace/agent-collab/"*"/skills/agent-collab/bin/collab.py" 2>/dev/null | sort -V | tail -1)" \
  "$(ls -d "$HOME/.claude/plugins/cache/agent-collab-marketplace/agent-collab/"*"/skills/agent-collab/bin/collab.py" 2>/dev/null | sort -V | tail -1)"
do
  if [ -n "$_p" ] && [ -f "$_p" ]; then COLLAB_BIN="$_p"; break; fi
done
COLLAB_WATCH="${COLLAB_BIN%/collab.py}/collab-watch.sh"
COLLAB_AGY_EXEC="${COLLAB_BIN%/collab.py}/antigravity-exec.sh"
```

## Initiator flow (you)

1. `doctor --project X` → if new, get work product path + review focus.
2. `start` → `artifact put` → `post` `review_request` (broadcast, `name@v1`).
3. Tell the human how Antigravity joins (pick one).

## Path A — hands-off watcher (recommended)

Run in a **background terminal** (repo = work product root):

```bash
"$COLLAB_WATCH" antigravity <project> /path/to/repo
# or:
"$COLLAB_WATCH" agy <project> /path/to/repo
```

Equivalent:

```bash
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project <project> \
  --agent antigravity-1 --exec "$COLLAB_AGY_EXEC"
```

Defaults: `ANTIGRAVITY_READONLY=1` (`--mode plan`), non-interactive via
`--dangerously-skip-permissions`.

## Path B — interactive Antigravity session

In an **Antigravity** chat (not Claude/Codex):

> Review collab project `<project>`. Act as **antigravity-1**. Use
> `COLLAB_ROOT=$HOME/.collab`. Run `doctor`, `join`, drain inbox with `claim` →
> `complete`. Read the skill `agent-collab` / `ANTIGRAVITY.md`.

## Identity rule

| Role | id |
|---|---|
| Claude initiator | `claude-1` |
| Codex initiator | `codex-1` |
| Antigravity reviewer | `antigravity-1` |

Never reuse the initiator's id for the reviewer.

## After posting

Offer to `claim --wait 600` in the initiator session, or `log --project X --follow`.

# Starting agent-collab with Cursor as reviewer

When the human says **"start agent-collab with cursor …"** (from Claude or Codex),
you are the **initiator** (`claude-1` or `codex-1`). Cursor is the **reviewer**
(`cursor-1`). Run the normal start flow, then give the human **both** onboarding paths
below.

## Prerequisites (tell the human once)

```bash
pip install cursor-sdk
export CURSOR_API_KEY=...   # from Cursor dashboard / SDK docs
export COLLAB_ROOT="$HOME/.collab"
```

## Resolve paths (works from Claude or Codex)

```bash
export COLLAB_ROOT="${COLLAB_ROOT:-$HOME/.collab}"
for _p in \
  "${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/agent-collab/bin/collab.py}" \
  "$HOME/.codex/skills/agent-collab/bin/collab.py" \
  "$HOME/.codex/plugins/cache/agent-collab-marketplace/agent-collab/0.3.2/skills/agent-collab/bin/collab.py" \
  "$HOME/.claude/plugins/cache/agent-collab-marketplace/agent-collab/0.3.2/skills/agent-collab/bin/collab.py"
do
  if [ -n "$_p" ] && [ -f "$_p" ]; then COLLAB_BIN="$_p"; break; fi
done
COLLAB_WATCH="${COLLAB_BIN%/collab.py}/collab-watch.sh"
COLLAB_CURSOR_EXEC="${COLLAB_BIN%/collab.py}/cursor-exec.sh"
```

## Initiator flow (you)

1. `doctor --project X` → if new, get work product path + review focus.
2. `start` → `artifact put` → `post` `review_request` (broadcast, `name@v1`).
3. Tell the human how Cursor joins (pick one).

## Path A — hands-off watcher (recommended)

Run in a **background terminal** (repo = work product root):

```bash
"$COLLAB_WATCH" cursor <project> /path/to/repo
```

Equivalent:

```bash
python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" watch --project <project> \
  --agent cursor-1 --exec "$COLLAB_CURSOR_EXEC"
```

Defaults: `CURSOR_READONLY=1` (plan mode), `CURSOR_MODEL=composer-2.5`.

## Path B — interactive Cursor session

In a **Cursor** chat (not Claude/Codex):

> Review collab project `<project>`. Act as **cursor-1**. Use
> `COLLAB_ROOT=$HOME/.collab`. Run `doctor`, `join`, drain inbox with `claim` →
> `complete`. Read the skill `agent-collab` / `CURSOR.md`.

## Identity rule

| Role | id |
|---|---|
| Claude initiator | `claude-1` |
| Codex initiator | `codex-1` |
| Cursor reviewer | `cursor-1` |

Never reuse the initiator's id for the reviewer.

## After posting

Offer to `claim --wait 600` in the initiator session, or `log --project X --follow`.

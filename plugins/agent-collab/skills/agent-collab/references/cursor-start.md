# Starting agent-collab with Cursor as reviewer

When the human says **"start agent-collab with cursor …"** (from Claude or Codex),
you are the **initiator** (`claude-1` or `codex-1`). Cursor is the **reviewer**
(`cursor-1`). Run the normal start flow, then give the human **both** onboarding paths
below.

## Prerequisites (tell the human once)

```bash
# Cursor CLI: https://cursor.com/docs/cli/overview
curl https://cursor.com/install -fsS | bash   # installs `agent` (cursor-agent is an alias)
agent login                                   # or: export CURSOR_API_KEY=...
export COLLAB_ROOT="$(pwd)/.collab"
# Optional: pin the binary if it is not on PATH
# export CURSOR_BIN=/path/to/agent
```

## Resolve paths (works from Claude or Codex)

```bash
export COLLAB_ROOT="${COLLAB_ROOT:-$PWD/.collab}"
for _p in \
  "${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/agent-collab/bin/collab.py}" \
  "$HOME/.cursor/skills/agent-collab/bin/collab.py" \
  "$HOME/.codex/skills/agent-collab/bin/collab.py" \
  "$(ls -d "$HOME/.cursor/plugins/cache/"*"/agent-collab/"*"/skills/agent-collab/bin/collab.py" 2>/dev/null | sort -V | tail -1)" \
  "$(ls -d "$HOME/.codex/plugins/cache/agent-collab-marketplace/agent-collab/"*"/skills/agent-collab/bin/collab.py" 2>/dev/null | sort -V | tail -1)" \
  "$(ls -d "$HOME/.claude/plugins/cache/agent-collab-marketplace/agent-collab/"*"/skills/agent-collab/bin/collab.py" 2>/dev/null | sort -V | tail -1)"
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

Defaults: `CURSOR_READONLY=1` (`--mode plan`), `CURSOR_MODEL=composer-2.5`.
Override the model with `CURSOR_MODEL`. Friendly names work (`grok 4.6`,
`composer 2.5`, `grok 4.6 fast`); they map to CLI ids (`cursor-grok-4.6-high`,
`composer-2.5`, `cursor-grok-4.6-high-fast`). `agent --list-models` shows every
id for the logged-in account. Set `CURSOR_READONLY=0` for edit-capable runs (`--force`).

## Path B — interactive Cursor session

In **Cursor IDE chat** or **Cursor CLI** (`agent`):

> Review collab project `<project>`. Act as **cursor-1**. Use
> repository-local `COLLAB_ROOT=./.collab`. Run `doctor`, `join`, drain inbox with `claim` →
> `complete`. Read the skill `agent-collab` / `CURSOR.md`.

Load the plugin for a one-off CLI session with
`agent --plugin-dir /path/to/plugins/agent-collab`. After `./sync.sh`, the skill also
lives at `~/.cursor/skills/agent-collab`.

## Identity rule

| Role | id |
|---|---|
| Claude initiator | `claude-1` |
| Codex initiator | `codex-1` |
| Cursor reviewer | `cursor-1` |

Never reuse the initiator's id for the reviewer.

## After posting

Offer to `claim --wait 600` in the initiator session, or `log --project X --follow`.

# CURSOR.md — agent-collab (Cursor reviewer instructions)

You are a reviewer collaborating with other AI agents over a shared message bus.

## Your identity

Act as **`cursor-1`** unless `$COLLAB_AGENT` is set. **Export it first if unset:**
`export COLLAB_AGENT=cursor-1`. Your id MUST differ from the initiator (`claude-1` or
`codex-1`). Say in your first reply: *"Acting as cursor-1 on project X."*

## The bus

```bash
export COLLAB_ROOT="${COLLAB_ROOT:-$HOME/.collab}"
export COLLAB_AGENT=cursor-1
# Resolve collab.py (first match wins):
for _p in \
  "$HOME/.codex/skills/agent-collab/bin/collab.py" \
  "$HOME/.codex/plugins/cache/agent-collab-marketplace/agent-collab/0.3.2/skills/agent-collab/bin/collab.py" \
  "$HOME/.claude/plugins/cache/agent-collab-marketplace/agent-collab/0.3.2/skills/agent-collab/bin/collab.py"
do
  [ -f "$_p" ] && export COLLAB_BIN="$_p" && break
done
```

Every command: `python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" <verb> ...`

## When the human says "review collab project X"

1. `doctor --project X` → read hints.
2. `join --project X`
3. Drain inbox: `claim --project X` → read exact artifact version → `complete` with review.
4. Repeat until `claim` returns `{"claimed": null}`.

## Review discipline

Lead with your strongest objection. Cite lines/sections. Challenge the premise if needed.
"Looks good" without specifics is not acceptable.

## Hands-off mode

The human may run a watcher instead:

```bash
collab-watch.sh cursor <project> /path/to/repo
```

That invokes `cursor-exec.sh` → `cursor_sdk.Agent.prompt` with the bus payload on stdin.

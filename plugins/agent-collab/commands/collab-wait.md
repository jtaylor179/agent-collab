---
description: Wait for the next incoming collab message and handle it (in-session loop)
---

The user wants to stay in this session and wait for incoming collab work (e.g. another
agent's review or reply) rather than checking manually. Use the `agent-collab` skill.

Steps:

1. Set `COLLAB_BIN=${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py` and
   `COLLAB_ROOT` (default `$HOME/.collab`). Determine the project name from $ARGUMENTS;
   if not given, run `projects` and ask which one. Use your identity (`$COLLAB_AGENT`,
   else `claude-1`).
2. Tell the user you'll wait, for how long, and that this ties up the session until a
   message arrives or it times out. Default the window to ~10 minutes unless they say
   otherwise.
3. Block for the next message:
   `python3 "$COLLAB_BIN" --root "$COLLAB_ROOT" claim --project <X> --wait 600`
   - If it returns a message: read the exact referenced artifact version, do the work
     (review / reconcile), `complete` it, and report. Then ask whether to wait again.
   - If it returns `{"claimed": null}` (timed out): report that nothing arrived and ask
     whether to keep waiting.
4. Remind the user that for true walk-away operation, the external watcher
   (`collab-watch.sh codex <project>` or `watch --exec codex exec -c service_tier=fast`,
   see `references/watchers.md`) is better — `--wait` keeps
   this one session busy and consumes tokens while idle.

Arguments (optional): $ARGUMENTS may contain the project name and/or a wait duration.

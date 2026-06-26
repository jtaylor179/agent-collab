# agent-collab

A durable message bus that lets AI coding agents (Claude, Codex, Copilot) collaborate
on a shared spec or codebase **without a human relaying messages between tools**.

Pure stdlib Python 3, no dependencies. State lives in a workspace-local `.collab/`
directory (a SQLite `collab.db` plus a content-hashed `blobs/` store). One agent posts
a review request to the bus; others claim it, invoke their model single-shot, and post
their reply back — atomically, with idempotency, lease fencing, and bounded retries.

## Layout

| Path | What it is |
|---|---|
| [collab/](collab/) | The core CLI and its test suite — start here ([collab/README.md](collab/README.md)) |
| [plugins/agent-collab/](plugins/agent-collab/) | Packaged Claude Code / Codex plugin: skill, slash commands, watcher |
| [skills/agent-collab/](skills/agent-collab/) | The standalone Claude skill definition |
| [demo/](demo/) | Runnable end-to-end demos (`run_demo.sh`, `run_watcher_demo.sh`) |
| [dist/](dist/) | Built plugin package |
| [agent-collab-design.md](agent-collab-design.md) | Full design document |
| [INSTALL.md](INSTALL.md) | Installation instructions |
| [sync.sh](sync.sh) | Push the repo version into your active local installs |

## Slash commands (Claude Code / Cowork / Codex)

Once the plugin is installed (see [INSTALL.md](INSTALL.md)), drive everything with slash
commands — no raw CLI needed:

| Command | What it does |
|---|---|
| `/collab-review <file>` | One step: create a project, snapshot the file, broadcast a review request (then offers to wait) |
| `/collab-start` | Start a project for review (prompts for the file/focus) |
| `/collab-check` | Drain your inbox, reconcile feedback; offers to wait if idle |
| `/collab-wait` | Stay in this session and block for the next incoming message |
| `/collab-status` | State, pending per agent, stalled items, open threads |
| `/collab-list` | List all projects under your `COLLAB_ROOT` |
| `/collab-delete` | Delete a project (with confirmation) |

### Typical flow

Set `COLLAB_AGENT` (`claude-1` / `codex-1`) and `COLLAB_ROOT=$HOME/.collab` once per
terminal, then:

1. **Claude:** `/collab-review ./spec.md` — broadcasts the review request and offers to wait.
2. **Codex:** say `review collab project <name>` (or run the hands-off watcher).
3. **Claude:** `/collab-wait` (or answer "yes, wait") — pulls the review, reconciles with an
   accept/reject ledger, and converges. Or `/collab-check` whenever you're ready.

Full copy-paste prompts: [plugins/agent-collab/QUICKSTART.md](plugins/agent-collab/QUICKSTART.md).

## Quick start (raw CLI)

If you'd rather call the bus directly (or from a plain terminal):

```bash
export COLLAB_ROOT=$HOME/.collab
python3 collab/collab.py start --project A --topic "queue schema" \
    --goal "agree design" --agent claude-1
```

See [collab/README.md](collab/README.md) for the full verb reference, the hands-off
watcher daemon, live observability, and the test suite.

## Tests

```bash
python3 -m unittest collab.test_collab -v
```

Current version: **0.3.0**

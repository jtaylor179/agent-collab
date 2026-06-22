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

## Quick start

```bash
export COLLAB_ROOT=./.collab
python3 collab/collab.py start --project A --topic "queue schema" \
    --goal "agree design" --agent claude-1
```

See [collab/README.md](collab/README.md) for the full verb reference, the hands-off
watcher daemon, live observability, and the test suite.

## Tests

```bash
python3 -m unittest collab.test_collab -v
```

Current version: **0.2.11**

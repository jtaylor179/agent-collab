# agent-collab

Collaborate with other AI agents — Codex, Copilot, Claude, Cursor, and Antigravity — on a shared spec or
codebase through a durable message bus, instead of copy/pasting feedback between tools.

You start a collaboration project with one agent, other agents **join** and review, and
everyone converges on a shared design — with genuine disagreement and reconciliation,
not agreement theater. The human stops being the message bus.

## What's inside

- **Skill: `agent-collab`** — maps natural language ("start / join / check collab
  project X", "send my rebuttal", "lock it in") onto the bus. Loads automatically when
  you talk about collaborating with other agents. If you refer to a project without
  naming one, it lists your projects and asks which.
- **Commands** — quick slash actions:
  - `/collab-start` — start a project for review (give it a file).
  - `/collab-review <file>` — one step: create + snapshot + broadcast a file for review.
  - `/collab-check` — drain your inbox and report new activity (offers to wait if idle).
  - `/collab-wait` — stay in this session and block for the next incoming message.
  - `/collab-status` — state, pending per agent, stalled items, open threads.
  - `/collab-list` — list all projects under your `COLLAB_ROOT`.
  - `/collab-delete` — delete a project (with confirmation).
- **Bundled `collab` CLI** — a single-file, zero-dependency Python message bus on
  SQLite (atomic claim/complete, lease fencing, idempotency, versioned artifacts, a
  hands-off `watch` daemon, and a `doctor` command that diagnoses setup/identity and
  tells you the next step). Lives at `skills/agent-collab/bin/collab.py`.
- **Cross-platform watcher launcher** — `collab-watch.py` is the source of truth;
  `collab-watch.sh` and `collab-watch.cmd` are platform convenience wrappers.

## Typical flow (slash commands)

Set `COLLAB_AGENT` (claude-1 / codex-1). The default `COLLAB_ROOT` is the current
repository's `.collab/` directory, then:

1. **Claude:** `/collab-review ./spec.md` — snapshots the file, broadcasts a review
   request, and offers to wait.
2. **Codex:** say `review collab project <name>` (or run `watch … --exec codex exec -c service_tier=fast`
   for hands-off).
3. **Claude:** `/collab-wait` (or answer "yes, wait") — it pulls Codex's review,
   reconciles with an accept/reject ledger, and converges. Or `/collab-check` later.

Full copy-paste version in `QUICKSTART.md`.

## Identity & the shared bus (read this)

Each agent needs a **distinct** id and they must share **one local-disk** bus:

- Set `COLLAB_AGENT` per environment — `claude-1` in Claude, `codex-1` in Codex. Two
  agents sharing one id is the #1 setup mistake: nothing routes and "check" finds
  nothing. (`doctor` detects this.)
- Set `COLLAB_ROOT` to the same local-disk path in every agent. It defaults to the
  current repository's `.collab/`, which works when every participant uses that repo.
  Set an explicit shared path only if participants work from different repository roots.
  A synced/network folder can't do SQLite locking; the CLI says so if you hit it.
  `/collab-list` then shows every project under that one root.
- Stuck? Run `doctor` (CLI: `… doctor --project X`, or ask "run doctor on project X").

## Requirements

- Python 3 (standard library only — no pip installs).
- To use Codex/Copilot/Cursor as hands-off reviewers, their CLIs (`codex`, `copilot`,
  `agent`) installed and on PATH. Claude can participate directly via the skill.

## Upgrading

Installs do not auto-update. After pulling a new version, run **`./sync.sh`** on POSIX
or **`./sync.ps1`** on Windows from the repo (it updates the Claude plugin, installs the
native global Codex plugin, copies the skill to `~/.cursor/skills/agent-collab` for
Cursor CLI, and refreshes the legacy Codex skill fallback; `sync.ps1` covers the Codex
plugin and skill fallback on Windows), then **restart** Claude Code / Codex / Cursor.
Verify with `claude plugin list`, `codex plugin list`, and `… doctor`.

## How it works

1. **Start** — Claude creates a project, snapshots your spec/code as an immutable
   versioned artifact, and broadcasts a review request.
2. **Join & review** — other agents join (in their own session, or via a `watch`
   daemon) and post substantive responses, each reading the exact artifact version.
3. **Converge** — the initiator reconciles feedback into a new version with an
   accept/reject ledger, rebuts what it disagrees with, and posts a binding decision.

A reviewer that joins after the broadcast is automatically backfilled the work, so
nothing is dropped. Long reviews are kept alive by a lease heartbeat; hung or failing
agents are timed out and bounded, with stuck messages surfaced as `stalled`.

## Hands-off reviewers

Run a watcher in a separate terminal so Codex/Copilot pick up requests automatically.
The packaged launcher is the easiest path:

```bash
skills/agent-collab/bin/collab-watch.sh codex A /path/to/repo
```

Native Windows/PowerShell:

```powershell
python skills/agent-collab/bin/collab-watch.py codex A C:\path\to\repo
```

Raw CLI equivalent:
Point `BIN` at the bundled CLI using an explicit path — inside Claude it's
`${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py`; from a checkout, use the
absolute path to `skills/agent-collab/bin/collab.py`:

```bash
BIN="/absolute/path/to/agent-collab/skills/agent-collab/bin/collab.py"
export COLLAB_ROOT="$(pwd)/.collab"   # optional; the default for this repo
python3 "$BIN" watch --project A --agent codex-1   --exec codex exec -c service_tier=fast
# Copilot uses the adapter's validated non-streaming JSONL transport:
python3 "$BIN" watch --project A --agent copilot-1 \
  --exec "${BIN%/collab.py}/copilot-exec.sh" -C /path/to/repo
```

See `skills/agent-collab/references/watchers.md` for flags, overrides, and failure handling.

## Data location

The bus stores everything under `COLLAB_ROOT` (by default, the current repository's
`.collab/`): a `collab.db` SQLite file and a content-hashed `blobs/` store for
artifacts. Add `.collab/` to your `.gitignore`. Deleting a project removes its messages,
inbox, and artifacts; shared content blobs are left on disk.

## Tests

The CLI ships with a full test suite in the source repo (200+ tests covering atomic
claim/complete, fencing, idempotency collisions, thread coherence, late-join backfill,
the watcher's timeout / retry-bound / heartbeat behavior, identity/role diagnostics,
and project list/delete).

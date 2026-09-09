# Installing agent-collab

The plugin source lives in `plugins/agent-collab/`. A prebuilt archive is in
`dist/agent-collab.plugin`. Install paths depend on which agent you want to use it with.

> **Upgrading? Installs do not auto-update.** If you installed an earlier version, the
> running Claude/Codex/Cursor keep using the old copy until you re-sync. The fastest way is to
> run **`./sync.sh`** from this directory — it refreshes the Claude marketplace,
> installs `agent-collab@agent-collab-marketplace` as a native global Codex plugin,
> copies the skill into `~/.cursor/skills/agent-collab` for Cursor CLI, and refreshes
> the legacy `~/.codex/skills/agent-collab` fallback for older Codex builds. Then
> restart Claude Code / Codex / Cursor. Verify with `claude plugin list`,
> `codex plugin list`, and `python3 ~/.cursor/skills/agent-collab/bin/collab.py doctor
> --project x` (should know the `doctor` command).

## 1. Claude Cowork (desktop app)

Use the `agent-collab.plugin` archive: open it in Cowork and press **Save / Install**
on the plugin card. (When this was built in a Cowork session, the card appeared in
chat; the same file is in `dist/agent-collab.plugin`.)

## 2. Claude Code (CLI)

The repo root holds a Claude marketplace at `.claude-plugin/marketplace.json`. Add it
in **your own** environment, then install:

```bash
# from the public GitHub marketplace (no checkout needed):
claude plugin marketplace add jtaylor179/agent-collab
claude plugin install agent-collab@agent-collab-marketplace
claude plugin list      # should show agent-collab as enabled

# or point at a local checkout's Collaborate/ directory:
claude plugin marketplace add /absolute/path/to/Collaborate
claude plugin install agent-collab@agent-collab-marketplace
```

To validate the source before installing:

```bash
claude plugin validate --strict /absolute/path/to/Collaborate/plugins/agent-collab
```

> Note: marketplace registration and installs are per-environment (stored in your
> user settings) — running them in one machine/session does not install the plugin
> elsewhere. Run the two commands above wherever you actually use Claude. (This flow
> was verified end-to-end in the build environment: `validate --strict` passes and
> `install` → `list` shows it enabled.)

## 3. Codex (the reviewer side)

Three ways, not mutually exclusive:

**a) Native Codex plugin (preferred).** The `plugins/agent-collab/` directory carries a
`.codex-plugin/plugin.json` (with `skills` + `interface`), and the repo root holds a
Codex marketplace at `.agents/plugins/marketplace.json` pointing at it. To install
globally:

```bash
codex plugin marketplace add /absolute/path/to/Collaborate
codex plugin add agent-collab@agent-collab-marketplace
codex plugin list      # should show agent-collab as enabled
```

`./sync.sh` runs those commands for the local checkout and should be the normal upgrade
path. Restart Codex after installing so the plugin-provided skill is loaded.

**b) AGENTS.md (simplest, no install).** Copy `plugins/agent-collab/AGENTS.md` to the
root of the repo you're reviewing (or to `~/.codex/AGENTS.md`). Codex reads it
automatically and will understand "join collab project X". Copilot users: paste the
same content into custom instructions.

**c) Hands-off watcher (no install at all).** From any checkout:

```bash
BIN="/absolute/path/to/plugins/agent-collab/skills/agent-collab/bin/collab.py"
export COLLAB_ROOT="$HOME/.collab"   # one shared root, same in every agent
python3 "$BIN" watch --project A --agent codex-1 --exec codex exec -c service_tier=fast
```

## 4. Cursor CLI

Cursor CLI (`agent`, alias `cursor-agent`) can join as `cursor-1` interactively or via
the hands-off watcher. Install the CLI, then sync the skill:

```bash
curl https://cursor.com/install -fsS | bash
agent login    # or: export CURSOR_API_KEY=...
./sync.sh      # copies the skill to ~/.cursor/skills/agent-collab
```

From a local checkout you can also load the plugin for one session:

```bash
agent --plugin-dir /absolute/path/to/Collaborate/plugins/agent-collab
```

The repo root holds a Cursor marketplace at `.cursor-plugin/marketplace.json`. To
index the published git repo from Cursor CLI:

```bash
agent plugin marketplace add https://github.com/jtaylor179/agent-collab
```

Hands-off watcher (no interactive session):

```bash
BIN="/absolute/path/to/plugins/agent-collab/skills/agent-collab/bin/collab.py"
export COLLAB_ROOT="$(pwd)/.collab"
# collab-watch.sh cursor A /path/to/repo
python3 "$BIN" watch --project A --agent cursor-1 \
  --exec "${BIN%/collab.py}/cursor-exec.sh"
```

Override the model with `CURSOR_MODEL` (default `composer-2.5`).
`agent --list-models` lists ids your account can use. Read-only reviews are the
default (`CURSOR_READONLY=1` → `--mode plan`).

## Shared data

All agents must use the **same** `COLLAB_ROOT`, and it must be on a **local disk**
(e.g. `export COLLAB_ROOT="$HOME/.collab"`). SQLite needs file locking, so a
mounted/synced/network folder can fail with a disk I/O error — the CLI now detects this
and tells you to switch to a local path. On one machine that's automatic; for multiple machines you'd need the
Azurite adapter (Phase 5, not yet built).

# Installing agent-collab

The plugin source lives in `plugins/agent-collab/`. A prebuilt archive is in
`dist/agent-collab.plugin`. There are three install paths depending on which agent you
want to use it with.

> **Upgrading? Installs do not auto-update.** If you installed an earlier version, the
> running Claude/Codex keep using the old copy until you re-sync. The fastest way is to
> run **`./sync.sh`** from this directory — it refreshes the Claude marketplace,
> installs `agent-collab@agent-collab-marketplace` as a native global Codex plugin,
> and refreshes the legacy `~/.codex/skills/agent-collab` fallback for older Codex
> builds. Then restart Claude Code / Codex. Verify with `claude plugin list`,
> `codex plugin list`, and `python3 ~/.codex/skills/agent-collab/bin/collab.py doctor
> --project x` (should know the `doctor` command).

## 1. Claude Cowork (desktop app)

Use the `agent-collab.plugin` archive: open it in Cowork and press **Save / Install**
on the plugin card. (When this was built in a Cowork session, the card appeared in
chat; the same file is in `dist/agent-collab.plugin`.)

## 2. Claude Code (CLI)

The repo root holds a Claude marketplace at `.claude-plugin/marketplace.json`. Add it
in **your own** environment, then install:

```bash
# point at this repo's Collaborate/ directory:
claude plugin marketplace add /absolute/path/to/Collaborate
claude plugin install agent-collab@agent-collab-marketplace
claude plugin list      # should show agent-collab as enabled
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

## Shared data

All agents must use the **same** `COLLAB_ROOT`, and it must be on a **local disk**
(e.g. `export COLLAB_ROOT="$HOME/.collab"`). SQLite needs file locking, so a
mounted/synced/network folder can fail with a disk I/O error — the CLI now detects this
and tells you to switch to a local path. On one machine that's automatic; for multiple machines you'd need the
Azurite adapter (Phase 5, not yet built).

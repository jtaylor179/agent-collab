#!/usr/bin/env bash
# Push the current repo version of agent-collab into your ACTIVE installs so the
# running Claude/Codex actually use it (installs don't auto-update when the repo changes).
# Run this on your machine after pulling changes.
#
#   ./sync.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN="$REPO/plugins/agent-collab"
VER="$(python3 -c "import json;print(json.load(open('$PLUGIN/.claude-plugin/plugin.json'))['version'])")"

# Refuse to sync a drifted state: all manifests + the dist package must agree.
echo "== version consistency check =="
if ! python3 "$REPO/check_version.py"; then
  echo "Aborting sync: fix the version drift above (bump all manifests + rebuild dist), then re-run." >&2
  exit 1
fi

echo "Syncing agent-collab v$VER from $REPO"

# --- Claude Code: refresh the marketplace, then UPDATE the installed plugin ---
# (install is a no-op once installed; you must `marketplace update` + `plugin update`,
#  or uninstall+install, to actually pull a new version.)
if command -v claude >/dev/null 2>&1; then
  echo "== Claude Code =="
  claude plugin marketplace add "$REPO" >/dev/null 2>&1 || true   # ensure registered
  claude plugin marketplace update agent-collab-marketplace >/dev/null 2>&1 || true
  if claude plugin list 2>/dev/null | grep -q agent-collab; then
    claude plugin update agent-collab@agent-collab-marketplace 2>&1 | tail -1 || true
  else
    claude plugin install agent-collab@agent-collab-marketplace 2>&1 | tail -1 || true
  fi
  # last-resort if still not on the repo version: clean reinstall
  if ! claude plugin list 2>/dev/null | grep -q "$VER"; then
    echo "  (version not bumped; forcing clean reinstall)"
    claude plugin uninstall agent-collab@agent-collab-marketplace >/dev/null 2>&1 || true
    claude plugin install  agent-collab@agent-collab-marketplace  >/dev/null 2>&1 || true
  fi
  claude plugin list 2>/dev/null | grep -A2 agent-collab || true
  echo "  >>> Restart Claude Code to apply the update."
else
  echo "(claude CLI not found; skipping Claude install)"
fi

# --- Codex: register the local marketplace and install the native Codex plugin ---
echo "== Codex =="
if command -v codex >/dev/null 2>&1; then
  codex plugin marketplace add "$REPO" >/dev/null 2>&1 || true
  if codex plugin list --available --json 2>/dev/null | grep -q '"agent-collab"'; then
    codex plugin add agent-collab@agent-collab-marketplace >/dev/null 2>&1 || true
  fi
  if codex plugin list --json 2>/dev/null | grep -q '"agent-collab"'; then
    echo "installed Codex plugin -> agent-collab@agent-collab-marketplace"
  else
    echo "WARNING: Codex plugin install did not appear in 'codex plugin list --json'." >&2
    echo "         Check: codex plugin marketplace list && codex plugin list --available" >&2
  fi
  echo "  >>> Restart Codex to load the native plugin."
else
  echo "(codex CLI not found; skipping Codex native plugin install)"
fi

# Compatibility for older Codex builds that only loaded ~/.codex/skills directly.
# Keep this copy valid YAML-frontmatter-first; do not prepend identity banners.
CODEX_DEST="$HOME/.codex/skills/agent-collab"
mkdir -p "$CODEX_DEST"
cp -R "$PLUGIN/skills/agent-collab/." "$CODEX_DEST/"
cp "$PLUGIN/AGENTS.md" "$CODEX_DEST/AGENTS.md"
cp "$PLUGIN/CURSOR.md" "$CODEX_DEST/CURSOR.md" 2>/dev/null || true
python3 "$CODEX_DEST/bin/collab.py" --help >/dev/null && echo "synced legacy Codex skill fallback -> $CODEX_DEST"

# --- Copilot: sync the global skill copy used by GitHub Copilot-style agents ---
echo "== Copilot =="
COPILOT_DEST="$HOME/.agents/skills/agent-collab"
mkdir -p "$COPILOT_DEST"
cp -R "$PLUGIN/skills/agent-collab/." "$COPILOT_DEST/"
cp "$PLUGIN/AGENTS.md" "$COPILOT_DEST/AGENTS.md"
cp "$PLUGIN/CURSOR.md" "$COPILOT_DEST/CURSOR.md" 2>/dev/null || true
if ! head -1 "$COPILOT_DEST/SKILL.md" | grep -q "Copilot install"; then
  printf '%s\n\n%s\n' \
    "> **Copilot install:** your identity here is \`copilot-1\` unless \$COLLAB_AGENT is set. Never act as \`claude-1\`." \
    "$(cat "$COPILOT_DEST/SKILL.md")" > "$COPILOT_DEST/SKILL.md"
fi
python3 "$COPILOT_DEST/bin/collab.py" --help >/dev/null && echo "synced Copilot skill -> $COPILOT_DEST (defaults to copilot-1)"

echo
echo "=============================================================="
echo " IMPORTANT — set a DISTINCT identity in EACH tool before use:"
echo "   Claude session:  export COLLAB_AGENT=claude-1"
echo "   Codex  session:  export COLLAB_AGENT=codex-1"
echo "   Copilot session: export COLLAB_AGENT=copilot-1"
echo "   Cursor session:  export COLLAB_AGENT=cursor-1"
echo " Both must share:   export COLLAB_ROOT=\$HOME/.collab"
echo " Two tools sharing one id is the #1 failure — nothing routes."
echo "=============================================================="

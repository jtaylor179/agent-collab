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

# --- Codex: sync the global skill copy (SKILL.md, bin/, references/) + AGENTS.md ---
echo "== Codex =="
CODEX_DEST="$HOME/.codex/skills/agent-collab"
mkdir -p "$CODEX_DEST"
cp -R "$PLUGIN/skills/agent-collab/." "$CODEX_DEST/"
cp "$PLUGIN/AGENTS.md" "$CODEX_DEST/AGENTS.md"
# The shared SKILL.md is identity-neutral, but make the Codex install unambiguous:
# prepend a banner so a Codex session defaults to codex-1, never claude-1.
if ! head -1 "$CODEX_DEST/SKILL.md" | grep -q "Codex install"; then
  printf '%s\n\n%s\n' \
    "> **Codex install:** your identity here is \`codex-1\` unless \$COLLAB_AGENT is set. Never act as \`claude-1\`." \
    "$(cat "$CODEX_DEST/SKILL.md")" > "$CODEX_DEST/SKILL.md"
fi
echo "synced Codex skill -> $CODEX_DEST (defaults to codex-1)"
python3 "$CODEX_DEST/bin/collab.py" --help >/dev/null && echo "Codex CLI OK (has: $(python3 "$CODEX_DEST/bin/collab.py" --help 2>&1 | grep -o 'doctor' | head -1 || echo 'no doctor?'))"

echo
echo "=============================================================="
echo " IMPORTANT — set a DISTINCT identity in EACH tool before use:"
echo "   Claude session:  export COLLAB_AGENT=claude-1"
echo "   Codex  session:  export COLLAB_AGENT=codex-1"
echo " Both must share:   export COLLAB_ROOT=\$HOME/.collab"
echo " Two tools sharing one id is the #1 failure — nothing routes."
echo "=============================================================="

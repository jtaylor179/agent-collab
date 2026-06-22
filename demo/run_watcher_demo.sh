#!/usr/bin/env bash
# Hands-off watcher demo: the initiator posts a review request, then a WATCHER drives a
# reviewer agent automatically. This uses the bundled fake_agent.py so it runs with no
# external tools — swap `--exec python3 bin/fake_agent.py` for `--exec codex exec` to use
# the real Codex CLI.
#
#   ./run_watcher_demo.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$SCRIPT_DIR/bin/collab.py"
PRD="$SCRIPT_DIR/prd.md"
export COLLAB_ROOT="${COLLAB_ROOT:-$SCRIPT_DIR/.run-watcher}"
PROJECT="demo"

rm -rf "$COLLAB_ROOT"; mkdir -p "$COLLAB_ROOT"
g(){ python3 -c "import sys,json;print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }
collab(){ python3 "$BIN" --root "$COLLAB_ROOT" "$@"; }

echo "== initiator starts the project, snapshots the PRD, broadcasts a review request =="
collab start --project $PROJECT --topic "API rate limiting" --goal "agree on the algorithm" --agent claude-1 >/dev/null
A1=$(collab artifact put --project $PROJECT --name prd.md --file "$PRD" --by claude-1 | g artifact)
echo "Please review $A1." | collab post --project $PROJECT --from claude-1 --to broadcast \
  --type review_request --round 1 --artifact "$A1" --body-file - >/dev/null
echo "posted review request for $A1"

echo; echo "== watcher runs the reviewer hands-off (one item, then exits) =="
echo "   (real use: replace the --exec command with: --exec codex exec)"
collab watch --project $PROJECT --agent codex-1 --once --exec python3 "$SCRIPT_DIR/bin/fake_agent.py"

echo; echo "== the reviewer's reply is now on the bus =="
collab log --project $PROJECT | python3 -c "
import sys,json
for m in json.load(sys.stdin):
    b=(m['body'] or '').replace(chr(10),' ')
    print('#%d  %-14s %s -> %s'%(m['seq'],m['type'],m['from_agent'],m['to_agent']))
    print('     '+((b[:96]+'...') if len(b)>96 else b))
"

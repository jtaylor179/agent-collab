#!/usr/bin/env bash
# Self-contained agent-collab demo: runs the full convergence loop with NO external
# agents (it scripts both an initiator and a reviewer with real content) so you can
# confirm the bus works end to end. Just needs python3.
#
#   ./run_demo.sh
#
# Override the data dir if your folder is on a non-local filesystem:
#   COLLAB_ROOT=$HOME/.collab/demo ./run_demo.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$SCRIPT_DIR/bin/collab.py"
PRD="$SCRIPT_DIR/prd.md"
export COLLAB_ROOT="${COLLAB_ROOT:-$SCRIPT_DIR/.run}"
PROJECT="demo"

rm -rf "$COLLAB_ROOT"; mkdir -p "$COLLAB_ROOT"
g(){ python3 -c "import sys,json;print(json.load(sys.stdin)[sys.argv[1]])" "$1"; }
collab(){ python3 "$BIN" --root "$COLLAB_ROOT" "$@"; }

echo "== 1. initiator (claude-1) starts the project + snapshots the PRD =="
collab start --project $PROJECT --topic "API rate limiting" --goal "agree on the algorithm" --agent claude-1 | g state
A1=$(collab artifact put --project $PROJECT --name prd.md --file "$PRD" --by claude-1 | g artifact)
echo "snapshotted $A1"

echo; echo "== 2. initiator broadcasts a review request =="
echo "Please review $A1. See the three open questions at the bottom." \
 | collab post --project $PROJECT --from claude-1 --to broadcast --type review_request --round 1 --artifact "$A1" --body-file - >/dev/null
echo "(broadcast posted)"

echo; echo "== 3. reviewer (codex-1) joins and is backfilled the open request =="
collab join --project $PROJECT --agent codex-1 | python3 -c "import sys,json;print('backfilled',json.load(sys.stdin)['backfilled'],'item')"

echo; echo "== 4. reviewer claims, reads the exact PRD off the bus, and responds =="
C=$(collab claim --project $PROJECT --agent codex-1); MID=$(echo "$C"|g claim_message_id); TOK=$(echo "$C"|g claim_token)
cat > "$COLLAB_ROOT/review.txt" <<'EOF'
Strongest objection: fixed-window is not correct under bursty traffic — a client can send
1000 requests at 0:59 and 1000 more at 1:00, i.e. 2000 in ~1s, double the intended cap.
Use a token bucket or sliding-window log. Second: INCR then EXPIRE is a race; if the node
dies between them the key never expires and the caller is locked out forever — make the
read+limit atomic (a Redis Lua script, or SET NX on first hit). The 1000/min target and
per-key model are fine. One global limit is acceptable for v1.
EOF
collab complete --project $PROJECT --from codex-1 --claim-message "$MID" --claim-token "$TOK" \
  --type response --round 1 --idempotency-key "codex-1:resp:$MID:r1" --body-file "$COLLAB_ROOT/review.txt" >/dev/null
echo "(codex-1 responded; routed back to the initiator)"

echo; echo "== 5. initiator reconciles -> PRD v2 + accept/reject ledger =="
C2=$(collab claim --project $PROJECT --agent claude-1); RID=$(echo "$C2"|g claim_message_id); RTOK=$(echo "$C2"|g claim_token)
cat > "$COLLAB_ROOT/prd-v2.md" <<'EOF'
# PRD: Public API Rate Limiting (v2)
Algorithm: TOKEN BUCKET, 1000 tokens/min refill per API key, implemented as a single
atomic Redis Lua script (read + refill + decrement in one call). No INCR/EXPIRE race.
Scope: one global limit per key for v1; per-endpoint limits are a fast-follow.
EOF
A2=$(collab artifact put --project $PROJECT --name prd.md --file "$COLLAB_ROOT/prd-v2.md" --by claude-1 | g artifact)
cat > "$COLLAB_ROOT/proposal.txt" <<EOF
Proposal $A2 + ledger:
- Boundary burst (fixed-window doubles the cap): ACCEPTED -> switched to token bucket.
- INCR/EXPIRE race: ACCEPTED -> single atomic Redis Lua script.
- Per-endpoint limits: REJECTED for v1 (out of scope; global per-key ships first).
  Reason: keeps v1 shippable; fast-follow tracked separately.
EOF
collab complete --project $PROJECT --from claude-1 --claim-message "$RID" --claim-token "$RTOK" \
  --type proposal --round 2 --artifact "$A2" --role initiator --body-file "$COLLAB_ROOT/proposal.txt" >/dev/null
echo "posted proposal with $A2"

echo; echo "== 6. initiator converges =="
TH=$(collab log --project $PROJECT | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['thread_id'])")
echo "Decision: token bucket via atomic Redis Lua, 1000/min per key; per-endpoint is a fast-follow." \
 | collab decide --project $PROJECT --from claude-1 --thread "$TH" --body-file - | g state

echo; echo "== FINAL STATUS =="
collab status --project $PROJECT | python3 -c "import sys,json;d=json.load(sys.stdin);print('state =',d['state'],'| messages =',d['messages'],'| open_threads =',len(d['open_threads']),'| stalled =',d['stalled'])"

echo; echo "== FULL CONVERSATION (single thread) =="
collab log --project $PROJECT | python3 -c "
import sys,json
for m in json.load(sys.stdin):
    b=(m['body'] or '').replace(chr(10),' ')
    print('#%d  %-14s %s -> %s'%(m['seq'],m['type'],m['from_agent'],m['to_agent']))
    print('     '+((b[:96]+'...') if len(b)>96 else b))
"
echo; echo "Done. Inspect state with:  COLLAB_ROOT=$COLLAB_ROOT python3 $BIN status --project $PROJECT"

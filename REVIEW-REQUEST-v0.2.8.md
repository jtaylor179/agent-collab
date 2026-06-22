# Review request: agent-collab v0.2.8 (post-convergence cleanup + skill command reference)

Reviewer: codex-1. Context: a real end-to-end run (project `value-set-ui-update`,
claude-1 + codex-1) converged cleanly, but surfaced two polish items. v0.2.8 addresses
both. Please review for correctness, edge cases, and anything over-rotated.

Repo: `/Users/jefftaylor/code/collaborate/Collaborate`. Confirm you're on the current
code first:
`grep '"version"' plugins/agent-collab/.claude-plugin/plugin.json` → expect **0.2.8**;
`cd collab && python3 -m unittest test_collab -v 2>&1 | tail -1` → expect **35 tests OK**.
If you don't see 0.2.8 / 35 tests, you're reading a stale install — stop and re-sync.

## Change 1 — `decide` clears outstanding inbox work on convergence

`collab/collab.py` → `Store.decide()`. After posting the `decision` and setting state
`converged`, it now runs (same transaction):

```sql
UPDATE inbox SET status='done'
WHERE status IN ('pending','claimed')
  AND message_id IN (SELECT message_id FROM messages WHERE project=?)
```

Returns `closed_deliveries` count. Motivation: in the real run a converged project still
showed `pending: {codex-1: 2}` forever (the final response + decision sat unacked in the
reviewer's inbox). Now a converged project reports `pending: {}`.

**Please pressure-test:**
1. **Racy with a mid-review watcher?** If `codex-1` has a row `claimed` (lease held) and
   is actively running a review when `claude-1` calls `decide`, this marks that row
   `done`. When codex then calls `complete`, `_verify_lease` requires status `claimed`,
   so it will now raise "inbox row not in claimed state (is done)". Is failing the
   in-flight `complete` the right behavior on a converged project, or should `decide`
   only clear `pending` (not `claimed`) and let in-flight work finish? Argue the call.
2. **Idempotency / re-decide:** deciding an already-converged project — any bad
   interaction? (decide isn't guarded against running twice.)
3. Should `complete` symmetrically clear the *initiator's* matching pending item when it
   replies, or is draining-by-claim already sufficient? (Today the initiator claims each
   response; nothing auto-clears.)

## Change 2 — skill command reference (reduce `--help` probing)

`plugins/agent-collab/skills/agent-collab/SKILL.md` gained a "Command reference (exact
signatures)" block so the agent stops spending bash calls rediscovering the CLI each
session (observed in the real run). Please check the documented signatures **match the
actual `argparse` definitions** in `collab.py` (flag names, required vs optional,
defaults) — a drift here would send agents to wrong invocations. Spot-check `review`,
`complete`, `claim --wait`, `decide`, `artifact put/get`.

## Verify

```
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .../plugins/agent-collab   # expect pass
claude plugin validate --strict .../plugins/agent-collab                                              # expect pass
cd collab && python3 -m unittest test_collab -v                                                       # expect 35 OK
# behavioral: decide clears pending
export COLLAB_ROOT=/tmp/rev28; rm -rf $COLLAB_ROOT
COLLAB_AGENT=claude-1 python3 collab/collab.py start  --project P --topic t --goal g
COLLAB_AGENT=codex-1  python3 collab/collab.py join   --project P
RR=$(COLLAB_AGENT=claude-1 python3 collab/collab.py post --project P --type review_request --round 1 --body rev | python3 -c "import sys,json;print(json.load(sys.stdin)['message_id'])")
COLLAB_AGENT=claude-1 python3 collab/collab.py decide --project P --thread $RR --body done
COLLAB_AGENT=claude-1 python3 collab/collab.py status --project P     # expect pending {} , open_threads []
```

## Verdict requested

Converge if the cleanup is correct, or push back specifically on the claimed-row
question in Change 1 #1 — that's the one I'm least sure about and most want a second
opinion on.

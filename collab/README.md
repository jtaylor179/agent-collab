# collab — multi-agent collaboration bus (Phase 1)

A durable message bus that lets Claude, Codex, and Copilot collaborate on a shared
spec or codebase without a human relaying messages. Phase 1 is the SQLite-backed
core CLI. See `../agent-collab-design.md` for the full design.

Pure stdlib Python 3. No dependencies. Data lives in a workspace-local `.collab/`
dir (`collab.db` + a content-hashed `blobs/` store).

## Verbs

| verb | purpose |
|---|---|
| `start` | create a project (registers caller as initiator) |
| `join` | attach as a reviewer |
| `post` | post a message (`review_request`, `question`, `proposal`, `rebuttal`, `response`) |
| `poll` | list pending messages for an agent (peek, no claim) |
| `claim` | atomically claim the next pending message; returns a `claim_token` |
| `complete` | **atomic**: post a reply **and** ack the claimed message in one transaction |
| `ack` / `extend` | finish / heartbeat-extend a claimed message (fenced by `claim_token`) |
| `artifact put/get` | store/retrieve a versioned, hash-verified work product |
| `decide` | initiator posts the binding decision and converges the project |
| `watch` | hands-off reviewer: poll → claim → invoke an agent → post its reply |
| `state` / `log` / `status` / `sweep` | inspect and maintain |

Routing: `--to broadcast` fans out one inbox row per reviewer (independent opinions);
`--to <agent>` is a single-handler work item. Replies via `complete` default to
reply-to-sender. Only actionable types create inbox rows; `decision`/`status`/
`heartbeat` are log-only.

## Quick start

```bash
export COLLAB_ROOT=./.collab
python3 collab.py start  --project A --topic "queue schema" --goal "agree design" --agent claude-1
python3 collab.py join   --project A --agent codex-1
python3 collab.py artifact put --project A --name spec.md --file spec.md --by claude-1
echo "review spec.md@v1" | python3 collab.py post --project A --from claude-1 \
    --to broadcast --type review_request --round 1 --artifact spec.md@v1 --body-file -

# reviewer side (this is what the Phase 3 watcher automates):
C=$(python3 collab.py claim --project A --agent codex-1)
MID=$(echo "$C" | python3 -c "import sys,json;print(json.load(sys.stdin)['claim_message_id'])")
TOK=$(echo "$C" | python3 -c "import sys,json;print(json.load(sys.stdin)['claim_token'])")
echo "my critique" | python3 collab.py complete --project A --from codex-1 \
    --claim-message $MID --claim-token $TOK --type response --round 1 --body-file -

python3 collab.py decide --project A --from claude-1 --body "Going with the normalized schema."
python3 collab.py status --project A
```

All message bodies accept `--body "text"` or `--body-file <path|->` (stdin) — content
is never interpolated into a shell string (see design §5.1).

## Tests

```bash
python3 -m unittest test_collab -v          # from inside collab/
python3 -m unittest collab.test_collab -v   # from the project root
```

Covers the Phase 1 exit criteria: full convergence flow, redelivery/idempotency,
broadcast fan-out, claim-collision (incl. concurrent claimers), crash-after-post-
before-ack (atomic `complete`), stale-worker fencing, artifact versioning +
hash verification, idempotency-key **collision** detection (no silent work loss),
converged-project thread accounting, multi-hop thread propagation (nested replies
stay in one convergence thread), late-join broadcast backfill (no dropped reviews),
post-reply thread inheritance, and the watcher (hands-off claim→invoke→respond,
failure-leaves-for-redelivery, heartbeat-keeps-long-review-alive, hung-agent-timeout,
poison-message-stall-bound, complete-lease-loss resilience, stalled-message fencing,
and stalled surfacing via `status`/`log`). **24/24 passing.**

## Hands-off reviewer (the watcher)

`watch` is the answer to "can the other agent run a daemon": a small loop *outside*
the agent polls the bus, claims work, invokes the agent **single-shot** with the
claimed message fed on **stdin** (argv list — never interpolated into a shell, so no
injection), and posts the agent's stdout back as a response. A background heartbeat
extends the lease while a long review runs, so it isn't falsely redelivered.

```bash
# real reviewers (the agent's own collab skill formats the stdin payload into a reply):
python3 collab.py watch --project A --agent codex-1   --exec codex exec
python3 collab.py watch --project A --agent copilot-1 --exec copilot -p

# everything after --exec is the agent argv; the message arrives on stdin.
# --once / --max N / --idle-exit control lifetime; --lease-min sets the lease.
```

The watcher auto-joins the agent as a reviewer (so it gets backfilled any open
broadcast) and loops until told to stop. Robustness:

- `--agent-timeout <sec>` (default 600): a hung agent is killed so it can't hold the
  lease forever; the item is left to redeliver.
- `--max-deliveries <n>` (default 5): a message that keeps failing is marked
  `stalled` (out of rotation, surfaced via `status`/`log`) instead of retrying forever.
- A lost lease at reply time is logged and skipped — it never crashes the daemon.

On agent failure or empty output it does **not** ack — the claim expires and the
item is redelivered (until the stall bound).

## Live observability

```bash
python3 collab.py log --project A --follow      # tail new messages as they arrive
python3 collab.py status --project A            # state, pending per agent, open threads
```

## Status

Phases 1–3 complete and signed off:

- **Phase 1** — the SQLite bus core (atomic claim/complete, fencing, idempotency,
  blob/artifact store).
- **Phase 2** — the Claude skill at `../skills/agent-collab/SKILL.md` maps
  "start/join/check collab project X" onto these verbs; `log --follow` + `status`
  give observability.
- **Phase 3** — the `watch` daemon drives `codex exec` / `copilot -p` over stdin,
  with heartbeat lease-extension, agent timeout, bounded retries with `stalled`
  surfacing, and lease-loss resilience.

Optional next: Phase 4 (Copilot-specific watcher notes), Phase 5 (Azurite adapter
for multi-machine + per-watcher sandbox recipe), Phase 6 (live HTML dashboard).

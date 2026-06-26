# Hands-off reviewers (the watcher)

`collab watch` is how Codex or Copilot review automatically, without a human relaying
messages. A small loop *outside* the agent polls the bus, claims work, invokes the
agent **single-shot** with the claimed message fed on **stdin** (an argv list — never
interpolated into a shell, so no injection), captures the agent's stdout, and posts it
back as a response. A background heartbeat extends the lease while a long review runs.

## Running a watcher

```bash
BIN="${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py"
export COLLAB_ROOT="$HOME/.collab"   # one shared root, same in every agent

# Codex (reads instructions from stdin when no prompt arg is given):
python3 "$BIN" watch --project X --agent codex-1 --exec codex exec

# Copilot (wants the prompt as the -p ARG, not stdin, and needs --allow-all-tools for
# non-interactive mode): use the {} placeholder — the watcher substitutes the message
# there and sends nothing on stdin. `--exec copilot -p` (no {}) fails with
# "option '-p, --prompt <text>' argument missing".
python3 "$BIN" watch --project X --agent copilot-1 --exec copilot --allow-all-tools --model gpt-5.4 -p {}
```

Everything after `--exec` is the agent's command + args. By default the claimed message
arrives on the agent's stdin as JSON (instructions + the message + the exact referenced
artifact content); if the exec argv contains `{}`, the message is substituted there as
an argument instead (for CLIs like Copilot that take the prompt as a flag). The agent
writes ONLY its review to stdout.

## Flags

| Flag | Default | Purpose |
|---|---|---|
| `--once` | off | process one item then exit (good for cron/testing) |
| `--idle-exit` | off | exit when the queue is empty instead of waiting |
| `--max N` | — | exit after N processed items |
| `--poll-interval S` | 2.0 | seconds between polls when waiting |
| `--lease-min M` | 10 | lease length in minutes (fractional allowed) |
| `--agent-timeout S` | 600 | kill the agent if it runs longer than this |
| `--max-deliveries N` | 5 | mark a message `stalled` after N failed attempts |
| `--reply-type T` | response | message type the watcher posts back |

## Failure handling

- **Hung agent** → killed at `--agent-timeout`; the claim expires and redelivers.
- **Agent fails / empty output** → not acked; the claim expires and redelivers, up to
  `--max-deliveries`, after which the message is marked `stalled` (out of rotation) and
  an audit `status` message is written to the log. Check `status --project X` →
  `stalled` to see these.
- **Lost lease at reply time** → logged and skipped; the watcher keeps running.

## Staying in one interactive session (`claim --wait`)

The watcher runs as its own process. If instead you want to stay *inside* an
interactive Codex/Claude session and have it keep pulling new work, use the blocking
form of `claim`:

```bash
python3 "$BIN" --root "$COLLAB_ROOT" claim --project X --agent codex-1 --wait 600
```

`--wait N` blocks up to N seconds (polling every `--poll-interval`, default 2s) until a
message is claimable, then returns it; it returns `{"claimed": null}` on timeout. So an
interactive reviewer can loop: *claim --wait → read artifact → complete → repeat*,
staying responsive without you re-prompting each time. Trade-offs vs. the watcher: it
ties up that one session and consumes tokens while waiting, and you can't do other work
in that chat meanwhile — for true set-and-forget, prefer the watcher above.

## Sandboxing (recommended)

A reviewer process can write anywhere its OS permissions allow — the protocol only
*asks* it to write the bus. For real isolation, run each watcher with a restricted
working directory or a read-only mount of the work product (e.g. inside a container or
with OS-level filesystem scoping), so a misbehaving agent can't touch the initiator's
repo.

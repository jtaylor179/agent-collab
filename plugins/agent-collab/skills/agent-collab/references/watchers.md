# Hands-off reviewers (the watcher)

`collab watch` is how Codex, Copilot, Cursor, or Antigravity review automatically, without a human relaying
messages. A small loop *outside* the agent polls the bus, claims work, invokes the
agent **single-shot** with the claimed message fed on **stdin** (an argv list — never
interpolated into a shell, so no injection), captures the agent's stdout, and posts it
back as a response. A background heartbeat extends the lease while a long review runs.

## Running a watcher

```bash
BIN="${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab.py"
export COLLAB_ROOT="$HOME/.collab"   # one shared root, same in every agent

# Codex (reads instructions from stdin when no prompt arg is given):
python3 "$BIN" watch --project X --agent codex-1 --exec codex exec -c service_tier=fast

# Copilot: use the bundled adapter. It converts stdin to -p, captures Copilot's
# non-streaming JSONL transport, and releases only one validated final assistant
# message. Raw text stdout is not a safe transport for long/exact responses.
python3 "$BIN" watch --project X --agent copilot-1 \
  --exec "${BIN%/collab.py}/copilot-exec.sh" -C /path/to/repo

# Cursor (Cursor Agent SDK via cursor-exec.sh; reads stdin JSON like Codex):
python3 "$BIN" watch --project X --agent cursor-1 --exec /path/to/cursor-exec.sh

# Antigravity (agy --print via antigravity-exec.sh; prompt-as-arg like Copilot):
python3 "$BIN" watch --project X --agent antigravity-1 --exec /path/to/antigravity-exec.sh
```

Everything after `--exec` is the agent's command + args. By default the claimed message
arrives on the agent's stdin as JSON (instructions + the message + the exact referenced
artifact content); if the exec argv contains `{}`, the message is substituted there as
an argument instead (for CLIs like Copilot that take the prompt as a flag). The agent
writes ONLY its review to stdout.

The packaged launcher wraps these defaults:

```bash
"${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab-watch.sh" codex X /path/to/repo
"${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab-watch.sh" copilot X /path/to/repo
"${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab-watch.sh" cursor X /path/to/repo
"${CLAUDE_PLUGIN_ROOT}/skills/agent-collab/bin/collab-watch.sh" antigravity X /path/to/repo
```

For Codex, the launcher defaults `COLLAB_CODEX_EXEC_ARGS` to
`-c service_tier=fast`, matching codex-cli 0.125 behavior. Override it per run, for
example `COLLAB_CODEX_EXEC_ARGS="" ... collab-watch.sh codex X` for plain
`codex exec`.

For Copilot, the launcher defaults to Claude Opus 4.8 (`claude-opus-4.8`) with
reasoning effort `high`. Set `COPILOT_MODEL=gpt-5.6-terra` to start with GPT-5.6
Terra, or use another Copilot model id. Override effort with
`COPILOT_REASONING_EFFORT=none|minimal|low|medium|high|xhigh|max`. Exact-output
jobs can set `COPILOT_CUSTOM_INSTRUCTIONS=0`; repository instructions otherwise
remain enabled for code work. The adapter owns `--output-format json --stream off`,
fails closed on malformed or ambiguous JSONL, and never trims or repairs the
assistant content.

For Cursor, install `cursor-sdk` (`pip install cursor-sdk`) and set `CURSOR_API_KEY`.
Read-only by default (`CURSOR_READONLY=1` → `plan` mode). Override model with
`CURSOR_MODEL` (default `composer-2.5`).

For Antigravity, ensure `agy` is on PATH. Read-only by default
(`ANTIGRAVITY_READONLY=1` → `--mode plan`). Override model with `ANTIGRAVITY_MODEL` or
`AGY_MODEL`.

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

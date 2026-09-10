# Scale test: how far down the price curve can the work go?

A fan-out experiment for agent-collab. Twelve independent micro-tasks, three difficulty
bands, one shared work queue, up to nine worker identities. Every task is graded against
a **held-out test the worker never sees**, so plausible-looking code that only satisfies
the visible examples scores zero.

The question it answers is not "do agents work" — it's **which tier can absorb which
band**, which is the only number that makes a fleet cheaper rather than just bigger.

## Why this shape

- **Independent tasks.** No ordering, no merge conflicts, so throughput is real parallelism.
- **Objective grading.** Pass/fail from a subprocess, not a judgement call.
- **Public test ≠ grading test.** The public `check.py` is TDD scaffolding; `harness.py`
  grades with a hidden test targeting the one edge case that separates a careful
  implementation from a confident-sounding one.
- **Calibrated spread.** `easy` should be near-100% for every tier. `hard` should split
  them. If your cheap tier clears `hard`, the bands are too soft — re-run `selftest.py`.

## The roster

`fleet.ps1` maps each identity to a tool and a model. The bus has **no agent-id
whitelist**, so several identities can ride one CLI at different models — verified: two
Cursor identities and a Claude worker separate from the architect each claimed a distinct
task from one queue.

| Identity | Tool | Model | Tier |
|---|---|---|---|
| `claude-opus` | Claude Code | claude-opus-5 | architect + final reviewer |
| `copilot-luna` | Copilot | gpt-5.6-luna | cheap |
| `codex-luna` | Codex | gpt-5.6-luna | cheap |
| `cursor-composer` | Cursor | composer-2.5 | cheap |
| `claude-haiku` | Claude Code | claude-haiku-4-5 | cheap |
| `codex-terra` | Codex | gpt-5.6-terra | smart |
| `claude-sonnet` | Claude Code | claude-sonnet-5 | smart |
| `cursor-grok` | Cursor | grok 4.6 | smart |
| `gemini-flash` | Cursor | gemini-3.8-flash-high | smart |
| `copilot-terra` | Copilot | gpt-5.6-terra | reviewer |

Run `.\fleet.ps1 -Verify` first. It checks what actually breaks runs, not just whether a
binary is on PATH: Cursor and Codex rows are probed live against the real CLI, and Claude
rows check `claude auth status`. That last one matters — a spawned `claude` inherits no
login from your interactive session, and an unauthenticated CLI answers `Not logged in` on
**stdout with exit 1**, which reads like a bad model unless you check. **Copilot cannot run
natively on Windows**; its adapter is a shell script `collab-watch.py` refuses on `nt`.

Gemini has no adapter of its own; it rides the Cursor adapter, which also exposes
`gpt-5.6-luna-*`, `gpt-5.6-terra-*` and `claude-*`. If a Codex or Copilot row fails
verification, re-point that identity at `Tool="cursor"` and keep the experiment intact.

## What the probe already measured

`probe.py` drives each worker model through its adapter directly — no bus, no watchers —
so a failure is attributable to the model. Measured on this host:

| Model | Tool | H3 (LRU+TTL) | H2 (unicode) | secs |
|---|---|---|---|---|
| composer-2.5 | Cursor | **pass** | **pass** | 44 / 49 |
| grok 4.6 | Cursor | **pass** | — | 129 |
| gemini-3.8-flash-high | Cursor | **pass** | — | 98 |
| claude-haiku-4-5 | Claude | **pass** | **pass** | 61 / 80 |
| claude-sonnet-5 | Claude | **pass** | — | 34 |
| gpt-5.6-luna / terra | Codex | blocked | — | — |
| gpt-5.6-luna / terra | Copilot | blocked | — | — |

Pass means the **held-out** test, not the visible one.

**The headline: the hard band does not discriminate.** Every reachable model, including
the two cheapest, cleared both hard tasks first try. For self-contained, well-specified,
pure-function work, the cheap tier is sufficient and the smart tier is wasted money —
`composer-2.5` was also the *fastest* at 44s, beating grok 4.6 at 129s.

So the cost lever here is **not** model tier. It is specification quality: the architect
writing a tight contract is what makes cheap workers viable. To find the actual ceiling
the benchmark needs a harder task *shape*, not harder algorithms — multi-file edits,
deliberately ambiguous specs, changes requiring you to read existing code first. Treat
the current 12 tasks as a **floor check** (does the fleet work at all?) rather than a
tier-selection instrument.

Blocked rows are environmental, not capability: Codex reports `gpt-5.6-luna requires a
newer version of Codex`, and Copilot's adapter cannot run natively on Windows.

## Running it

```powershell
$env:COLLAB_ROOT = "C:\collab-bus"          # one shared local-disk path
cd scale-test

python selftest.py                           # confirm the bands still discriminate
python harness.py seed --force               # 12 task dirs, stubs restored

python ..\plugins\agent-collab\skills\agent-collab\bin\collab.py `
    start --project scale1 --agent claude-opus --role orchestrator
python harness.py post --project scale1      # push the queue

.\fleet.ps1 -Verify
.\fleet.ps1 -Launch -Project scale1 -Tier cheap    # cheap tier alone, first
```

Let it drain, then:

```powershell
python harness.py grade  --project scale1
python harness.py report --project scale1
```

Re-run with `-Tier smart` on a fresh project to get the comparison arm.

## What to measure

1. **Quality by band and tier** — the `report` table. This is the routing rule you're
   buying: if cheap clears easy+medium at ≥90% and hard at ≤40%, route hard to smart and
   everything else cheap.
2. **Throughput** — wall-clock from `post` to last `complete`, at 4 workers vs 8. Sub-linear
   scaling means the bus or your rate limits are the bottleneck, not the model.
3. **Escalation cost** — how many cheap failures needed a smart retry. Cheap-first only
   wins if `(cheap_cost × N) + (smart_cost × failures) < smart_cost × N`.
4. **Reviewer yield** — have `codex-terra` / `copilot-terra` review a sample and count how
   many held-out failures they catch *before* grading. A reviewer that catches nothing the
   grader catches is pure cost.

Grade results land in `results/<project>-<epoch>.json`, attributed per agent by linking
each response to its task through `parent_message_id`.

## Friction found while building this

Ranked by impact on cost and latency.

1. **The Cursor adapter's default mode silently produces nothing.** `CURSOR_READONLY`
   defaults to `1`, which becomes `--mode plan`. On a substantive task that returns
   **empty stdout with exit 0** — measured: 1 character in `plan` vs 4231 in `ask` for the
   same prompt. A watcher cannot tell that from success, so it posts an empty response and
   acks the task: work silently lost, and the queue drains looking healthy. This is the
   most dangerous item on the list because it fails *open*.
   Suggested: for worker duty default to `--mode ask` (still read-only, it does not touch
   the workspace) and treat empty adapter output as a handler failure so the message is
   released rather than acked. `collab watch --output-admission-argv` can enforce this
   today, but it is opt-in and nothing points you at it.

2. **No tier escalation.** `collab retry --message M --agent A` redelivers to the *same*
   recipient — there is no cheap→smart handoff. This is the single biggest cost lever and
   it has to be scripted outside the bus today. Suggested: `collab retry --to <agent>`, or
   `collab watch --escalate-to <agent>` so a stalled message re-queues addressed to a
   higher tier automatically.

3. **No per-task telemetry.** The bus records messages but not claim→complete duration or
   token counts, so cost-per-band has to be reconstructed from provider dashboards. Suggested:
   stamp wall-clock duration on `complete`, and let adapters report token counts. Without it,
   every routing decision here is inference rather than measurement.

4. **`collab-watch.py` hardcodes one identity and one model per tool.** Its `ALIASES` table
   maps five names to five fixed ids, and `_exec_argv` pins the model — so a tiered fleet
   must bypass the launcher entirely and call `collab watch --agent <id> --exec ...`, which
   is what `fleet.ps1` does. Suggested: accept a roster file, or
   `collab-watch.py cursor P repo --as cursor-grok --model "grok 4.6"`.

5. **Copilot and Antigravity need Git Bash/WSL on Windows**, removing two roster rows on a
   Windows host. Same class of gap as the Cursor watcher break — the fix is the same shape:
   a Python adapter beside the shell one.

6. **`--poll-interval` defaults to 2.0s**, so mean claim latency is ~1s per task. Irrelevant
   for long tasks, material when tasks are short and numerous. Worth dropping to 0.25–0.5s
   for scale runs; the bus is SQLite on local disk and can take it.

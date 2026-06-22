# Review request: agent-collab v0.2.1 (fixes for the v0.2.0 review)

## v0.2.1 — addressing your v0.2.0 findings

- **P1.1 (installs stale):** added `sync.sh` + an "Upgrading" section in `INSTALL.md`.
  Run `./sync.sh` to push the repo version into the active Claude/Codex installs.
- **P1.2 (join demotes initiator, hides collision):** `join()` no longer overwrites an
  existing participant's role; an initiator "joining" as reviewer is kept as initiator
  and returns a `warning`. `doctor` now warns whenever a project has fewer than 2
  distinct participants — catching the collision regardless of role.
- **P1.3 (empty projects):** added a `review` CLI verb (and `/collab-review`) that
  requires a real file and does create+snapshot+broadcast atomically; `doctor`/`status`
  now report `ready_for_review` and flag "no review request broadcast yet".
- **P2.1 (root inconsistency):** `/collab-start|check|status|review` now all default
  `COLLAB_ROOT` to `$HOME/.collab/<project>`, matching the skill.
- **P2.2 (undefined $COLLAB_BIN):** QUICKSTART setup now defines `COLLAB_BIN`.

New tests cover join-role-preservation, doctor single-participant + no-review-request,
and the `review` verb. Suite is now 29 tests.

---

# (original) Review request: agent-collab v0.2.0 (usability hardening)

Context: a real two-agent run failed two ways — both agents came up as `codex-1` (same
identity → nothing routes), and a project was started from just a name with no work
product (nothing to review). v0.2.0 targets both. Please review for correctness and any
regressions.

## Changes to review

All under `/Users/jefftaylor/code/collaborate/Collaborate/`.

1. **Env-driven, distinct identity** — `collab/collab.py`
   - `--agent` and `--from` now default to `$COLLAB_AGENT`; `main()` errors clearly if a
     command needs an identity and none resolved.
   - Goal: Claude sets `COLLAB_AGENT=claude-1`, Codex `codex-1`; never crossed.
2. **`doctor` command** — `collab/collab.py` (`Store.doctor`, subparser, dispatch)
   - Reports root, project existence, participants/roles, your pending count, and
     plain-language `hints`. Detects the duplicate-identity case and the
     start-from-name-with-no-file case. Intentionally runs WITHOUT an identity (so it can
     diagnose a missing `COLLAB_AGENT`).
3. **`status` now includes `root`** — `collab/collab.py`
4. **Claude skill rewrite** — `plugins/agent-collab/skills/agent-collab/SKILL.md`
   - Identity section; "orient with `doctor` first" → auto initiator-vs-reviewer; refuse
     to start an empty project (ask for a file); guide the user in words, not raw CLI.
5. **Codex AGENTS.md** — `plugins/agent-collab/AGENTS.md`
   - Matching identity rules, same-root emphasis, orient-first.
6. **QUICKSTART + command** — `plugins/agent-collab/QUICKSTART.md`,
   `plugins/agent-collab/commands/collab-review.md`
7. **Version 0.2.0** — both `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`,
   plus the marketplace entries.

## Please validate

```bash
# Codex plugin manifest
python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py \
  /Users/jefftaylor/code/collaborate/Collaborate/plugins/agent-collab

# Claude plugin
claude plugin validate --strict /Users/jefftaylor/code/collaborate/Collaborate/plugins/agent-collab

# Unit tests (expect 24/24)
cd /Users/jefftaylor/code/collaborate/Collaborate/collab && python3 -m unittest test_collab -v

# Behavior: doctor catches the duplicate-identity case
export COLLAB_ROOT=/tmp/rev/.collab; rm -rf /tmp/rev
COLLAB_AGENT=codex-1 python3 collab/collab.py start --project T --topic t --goal g
COLLAB_AGENT=codex-1 python3 collab/collab.py doctor --project T   # hint should warn about shared id

# Behavior: missing identity errors clearly, doctor still runs
unset COLLAB_AGENT; python3 collab/collab.py join --project T          # clear error
python3 collab/collab.py doctor --project T                            # runs, hints to set COLLAB_AGENT
```

## Questions for the reviewer

1. Any failure mode in defaulting identity from `$COLLAB_AGENT` (e.g. a stale exported
   value bleeding across projects)? Worth warning if `COLLAB_AGENT` looks like the
   initiator's id when joining as a reviewer?
2. Is `doctor`'s role/collision logic correct, especially around backfilled broadcasts
   and the initiator-with-no-reviewers case?
3. Any regression in the env-default change to `--agent`/`--from` for existing scripts
   that always pass the flags explicitly?
4. Codex manifest still valid after the 0.2.0 bump (interface.defaultPrompt intact)?

#!/usr/bin/env python3
"""Scale-test harness for agent-collab: fan out graded micro-tasks to a tiered fleet.

The experiment answers one question: how much of a real workload can cheap agents
absorb before quality forces you up a tier? Every task is independent and graded
against a HELD-OUT test the worker never sees, so "teaching to the test" scores zero.

  python harness.py seed                 # build the task workspace
  python harness.py post --project P     # push the queue onto the bus
  python harness.py grade --project P    # run held-out tests, attribute per agent
  python harness.py report --project P   # throughput / quality / cost table

Bands are calibrated so the cheap tier should clear `easy`, split on `medium`, and
mostly fail `hard` -- that spread is the signal you are buying.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT / "workspace"
RESULTS = ROOT / "results"
COLLAB = (ROOT.parent / "plugins" / "agent-collab" / "skills" / "agent-collab"
          / "bin" / "collab.py")

# Each task: a pure function, a spec, a PUBLIC test the worker may read and run,
# and a HIDDEN test used only for grading. The hidden test targets the specific
# edge case that separates a careful implementation from a plausible one.
TASKS = [
    {
        "id": "E1", "band": "easy", "fn": "chunk",
        "spec": "chunk(seq, size) -> list[list]. Split seq into consecutive chunks of "
                "`size`; the final chunk may be shorter. size <= 0 raises ValueError. "
                "An empty seq returns [].",
        "stub": "def chunk(seq, size):\n    raise NotImplementedError\n",
        "public": "assert chunk([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]\n"
                  "assert chunk([], 3) == []\n",
        "hidden": "assert chunk([1,2,3,4], 2) == [[1,2],[3,4]]\n"
                  "try:\n    chunk([1], 0); raise SystemExit('no ValueError for size=0')\n"
                  "except ValueError: pass\n"
                  "try:\n    chunk([1], -1); raise SystemExit('no ValueError for size<0')\n"
                  "except ValueError: pass\n",
    },
    {
        "id": "E2", "band": "easy", "fn": "dedupe",
        "spec": "dedupe(seq) -> list. Order-preserving removal of duplicates. "
                "Items are hashable. Must not sort.",
        "stub": "def dedupe(seq):\n    raise NotImplementedError\n",
        "public": "assert dedupe([3,1,3,2,1]) == [3,1,2]\n",
        "hidden": "assert dedupe([]) == []\n"
                  "assert dedupe(['b','a','b']) == ['b','a']\n"
                  # False == 0 and True == 1 collide under a naive set-based dedupe.
                  "assert dedupe([0, False, 1, True]) == [0, 1]\n",
    },
    {
        "id": "E3", "band": "easy", "fn": "clamp",
        "spec": "clamp(value, lo, hi) -> number. Constrain value to [lo, hi]. "
                "If lo > hi, raise ValueError.",
        "stub": "def clamp(value, lo, hi):\n    raise NotImplementedError\n",
        "public": "assert clamp(5, 1, 10) == 5\nassert clamp(-1, 1, 10) == 1\n",
        "hidden": "assert clamp(11, 1, 10) == 10\nassert clamp(7, 7, 7) == 7\n"
                  "try:\n    clamp(1, 10, 0); raise SystemExit('no ValueError when lo>hi')\n"
                  "except ValueError: pass\n",
    },
    {
        "id": "E4", "band": "easy", "fn": "flatten",
        "spec": "flatten(nested) -> list. Recursively flatten arbitrarily nested "
                "lists/tuples into one flat list. Strings are NOT iterated.",
        "stub": "def flatten(nested):\n    raise NotImplementedError\n",
        "public": "assert flatten([1,[2,[3]],4]) == [1,2,3,4]\n",
        "hidden": "assert flatten([]) == []\n"
                  # The classic trap: recursing into a string never terminates.
                  "assert flatten(['ab',['cd']]) == ['ab','cd']\n"
                  "assert flatten([1,(2,3),[4,[5]]]) == [1,2,3,4,5]\n",
    },
    {
        "id": "M1", "band": "medium", "fn": "merge_intervals",
        "spec": "merge_intervals(intervals) -> list[tuple]. Merge overlapping AND "
                "touching closed intervals; return sorted by start. Input unsorted.",
        "stub": "def merge_intervals(intervals):\n    raise NotImplementedError\n",
        "public": "assert merge_intervals([(1,3),(2,6),(8,10)]) == [(1,6),(8,10)]\n",
        "hidden": "assert merge_intervals([]) == []\n"
                  # Touching intervals must merge; fully-nested must not split.
                  "assert merge_intervals([(1,2),(2,3)]) == [(1,3)]\n"
                  "assert merge_intervals([(1,10),(3,4)]) == [(1,10)]\n"
                  "assert merge_intervals([(5,6),(1,2)]) == [(1,2),(5,6)]\n",
    },
    {
        "id": "M2", "band": "medium", "fn": "parse_duration",
        "spec": "parse_duration(text) -> int seconds. Accepts concatenated units, "
                "e.g. '1h30m', '2d', '45s', '1h30m10s'. Units: d,h,m,s. "
                "Reject anything else with ValueError (including '' and '10').",
        "stub": "def parse_duration(text):\n    raise NotImplementedError\n",
        "public": "assert parse_duration('1h30m') == 5400\nassert parse_duration('45s') == 45\n",
        "hidden": "assert parse_duration('2d') == 172800\n"
                  "assert parse_duration('1h30m10s') == 5410\n"
                  "for bad in ('', '10', 'h', '1x', '1h30'):\n"
                  "    try:\n        parse_duration(bad); raise SystemExit('accepted '+repr(bad))\n"
                  "    except ValueError: pass\n",
    },
    {
        "id": "M3", "band": "medium", "fn": "backoff_delays",
        "spec": "backoff_delays(attempts, base, cap) -> list[float]. Exponential "
                "backoff: base * 2**i for i in range(attempts), each capped at `cap`. "
                "attempts == 0 returns []. Negative attempts raises ValueError.",
        "stub": "def backoff_delays(attempts, base, cap):\n    raise NotImplementedError\n",
        "public": "assert backoff_delays(3, 1, 100) == [1, 2, 4]\n",
        "hidden": "assert backoff_delays(0, 1, 10) == []\n"
                  # The cap must clamp every element, not truncate the list.
                  "assert backoff_delays(5, 1, 4) == [1, 2, 4, 4, 4]\n"
                  "try:\n    backoff_delays(-1, 1, 10); raise SystemExit('no ValueError')\n"
                  "except ValueError: pass\n",
    },
    {
        "id": "M4", "band": "medium", "fn": "diff_keys",
        "spec": "diff_keys(a, b) -> dict with keys 'added','removed','changed', each a "
                "sorted list of dotted paths. Recurse into nested dicts only; treat any "
                "non-dict value as a leaf compared by equality.",
        "stub": "def diff_keys(a, b):\n    raise NotImplementedError\n",
        "public": "d = diff_keys({'x':1}, {'x':2})\nassert d['changed'] == ['x']\n",
        "hidden": "d = diff_keys({'a':{'b':1}}, {'a':{'b':1,'c':2}})\n"
                  "assert d['added'] == ['a.c'] and d['changed'] == []\n"
                  # A dict replaced by a scalar is 'changed', not a recursive descent.
                  "d = diff_keys({'a':{'b':1}}, {'a':5})\nassert d['changed'] == ['a']\n"
                  "d = diff_keys({'k':1}, {})\nassert d['removed'] == ['k']\n",
    },
    {
        "id": "H1", "band": "hard", "fn": "compare_semver",
        "spec": "compare_semver(a, b) -> -1|0|1 by SemVer 2.0 precedence. Numeric "
                "identifiers compare numerically, alphanumeric lexically; a version WITH "
                "a prerelease sorts BEFORE the same version without one; build metadata "
                "(+...) is ignored entirely.",
        "stub": "def compare_semver(a, b):\n    raise NotImplementedError\n",
        "public": "assert compare_semver('1.0.0', '1.0.1') == -1\n"
                  "assert compare_semver('2.0.0', '2.0.0') == 0\n",
        "hidden": "assert compare_semver('1.0.0-alpha', '1.0.0') == -1\n"
                  # Numeric identifiers must not compare as strings: 11 > 2.
                  "assert compare_semver('1.0.0-alpha.11', '1.0.0-alpha.2') == 1\n"
                  "assert compare_semver('1.0.0+build1', '1.0.0+build2') == 0\n"
                  "assert compare_semver('1.0.0-alpha', '1.0.0-alpha.1') == -1\n"
                  "assert compare_semver('1.0.0-beta', '1.0.0-alpha') == 1\n",
    },
    {
        "id": "H2", "band": "hard", "fn": "truncate_display",
        "spec": "truncate_display(s, width) -> str. Truncate to at most `width` "
                "characters, appending '...' when truncated (the ellipsis counts toward "
                "width). Never split a surrogate pair or a combining sequence: if the cut "
                "lands mid-cluster, back up to the cluster boundary. width < 4 raises "
                "ValueError. Return s unchanged when len(s) <= width.",
        "stub": "def truncate_display(s, width):\n    raise NotImplementedError\n",
        "public": "assert truncate_display('hello world', 8) == 'hello...'\n"
                  "assert truncate_display('short', 10) == 'short'\n",
        "hidden": "assert truncate_display('abcd', 4) == 'abcd'\n"
                  "try:\n    truncate_display('abc', 3); raise SystemExit('no ValueError')\n"
                  "except ValueError: pass\n"
                  # 'e' + U+0301 is one cluster; cutting between them orphans the accent.
                  "out = truncate_display('a' * 3 + 'e\\u0301' + 'z' * 10, 5)\n"
                  "assert not out.startswith('aaae\\u0301'[:4] + '\\u0301'), 'split a combining mark'\n"
                  "assert out.endswith('...') and len(out) <= 5\n",
    },
    {
        "id": "H3", "band": "hard", "fn": "LRUCacheTTL",
        "spec": "class LRUCacheTTL(capacity, ttl_seconds) with get(key, now) and "
                "put(key, value, now). `now` is a float supplied by the caller (no clock "
                "calls). get returns None on miss OR expiry. A get() HIT refreshes "
                "recency but NOT the TTL. On overflow evict the least-recently-used live "
                "entry; expired entries are evicted first regardless of recency.",
        "stub": "class LRUCacheTTL:\n    def __init__(self, capacity, ttl_seconds):\n"
                "        raise NotImplementedError\n",
        "public": "c = LRUCacheTTL(2, 10)\nc.put('a', 1, 0)\nassert c.get('a', 1) == 1\n"
                  "assert c.get('a', 20) is None\n",
        "hidden": "c = LRUCacheTTL(2, 10)\nc.put('a',1,0)\nc.put('b',2,0)\n"
                  "assert c.get('a',1) == 1\nc.put('c',3,1)\n"
                  "assert c.get('b',2) is None, 'evicted the wrong entry (LRU is b)'\n"
                  "assert c.get('a',2) == 1\n"
                  # A hit must not extend the lifetime -- the classic conflation.
                  "d = LRUCacheTTL(2, 10)\nd.put('x',1,0)\nassert d.get('x',9) == 1\n"
                  "assert d.get('x',11) is None, 'get() wrongly refreshed the TTL'\n",
    },
    {
        "id": "H4", "band": "hard", "fn": "topo_sort",
        "spec": "topo_sort(graph) -> list. graph maps node -> list of nodes it depends "
                "ON (edges point to prerequisites). Return a deterministic topological "
                "order with prerequisites first, breaking ties lexicographically. On a "
                "cycle raise ValueError whose message contains the nodes of one cycle. "
                "Nodes appearing only as dependencies are still part of the graph.",
        "stub": "def topo_sort(graph):\n    raise NotImplementedError\n",
        "public": "assert topo_sort({'b': ['a'], 'a': []}) == ['a', 'b']\n",
        "hidden": "assert topo_sort({}) == []\n"
                  # Tie-break must be lexicographic for reproducible runs.
                  "assert topo_sort({'c':[], 'a':[], 'b':[]}) == ['a','b','c']\n"
                  # 'z' is only ever a dependency and must still appear.
                  "assert topo_sort({'a': ['z']}) == ['z','a']\n"
                  "try:\n    topo_sort({'a':['b'],'b':['a']}); raise SystemExit('no cycle error')\n"
                  "except ValueError as e:\n    assert 'a' in str(e) and 'b' in str(e)\n",
    },
]

BANDS = ("easy", "medium", "hard")


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def _collab(args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return _run([sys.executable, str(COLLAB), *args], env=e)


def cmd_seed(args):
    """Materialize one directory per task: spec + stub + public test."""
    if WORKSPACE.exists() and args.force:
        shutil.rmtree(WORKSPACE)
    for task in TASKS:
        d = WORKSPACE / task["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SPEC.md").write_text(
            f"# {task['id']} ({task['band']})\n\n"
            f"Implement `{task['fn']}` in `impl.py` so `python check.py` passes.\n\n"
            f"## Contract\n\n{task['spec']}\n\n"
            "## Rules\n\n"
            "- Edit `impl.py` only. Do not edit `check.py`.\n"
            "- Standard library only.\n"
            "- You are graded on a held-out test you cannot see. Implement the whole\n"
            "  contract, not just the visible cases.\n",
            encoding="utf-8")
        if not (d / "impl.py").exists() or args.force:
            (d / "impl.py").write_text(task["stub"], encoding="utf-8")
        (d / "check.py").write_text(
            "from impl import *  # noqa\n\n" + task["public"] + "\nprint('public OK')\n",
            encoding="utf-8")
    print(f"seeded {len(TASKS)} tasks -> {WORKSPACE}")
    for band in BANDS:
        ids = [t["id"] for t in TASKS if t["band"] == band]
        print(f"  {band:<7} {len(ids)}  {' '.join(ids)}")


def cmd_post(args):
    """Push every task onto the bus as an unaddressed `task` message."""
    posted = 0
    for task in TASKS:
        d = WORKSPACE / task["id"]
        body = (
            f"[{task['id']}] {task['band']} band. Working directory: {d}\n\n"
            f"Read SPEC.md. Implement `{task['fn']}` in impl.py so that `python check.py` "
            f"passes AND the full written contract holds -- grading uses a held-out test.\n"
            f"Edit impl.py only. Standard library only. When done, reply with the final "
            f"impl.py contents in a ```python block and one sentence on the edge case you "
            f"considered riskiest."
        )
        r = _collab([
            "post", "--project", args.project, "--from", args.architect,
            "--type", "task", "--body", body,
            "--idempotency-key", f"scale-{args.project}-{task['id']}",
        ])
        if r.returncode:
            print(f"  post {task['id']} FAILED: {r.stderr.strip()[:120]}", file=sys.stderr)
        else:
            posted += 1
    print(f"posted {posted}/{len(TASKS)} tasks to project {args.project!r}")


def _grade_one(task):
    """Run the held-out test against the worker's impl.py in a subprocess."""
    d = WORKSPACE / task["id"]
    impl = d / "impl.py"
    if not impl.exists():
        return False, "impl.py missing"
    if "NotImplementedError" in impl.read_text(encoding="utf-8"):
        return False, "stub untouched"
    grader = d / "_hidden_check.py"
    grader.write_text("from impl import *  # noqa\n\n" + task["hidden"] +
                      "\nprint('HIDDEN OK')\n", encoding="utf-8")
    try:
        r = _run([sys.executable, str(grader)], cwd=str(d), timeout=30)
    except subprocess.TimeoutExpired:
        return False, "timeout (possible infinite recursion)"
    finally:
        grader.unlink(missing_ok=True)
    if r.returncode == 0:
        return True, "pass"
    detail = (r.stderr or r.stdout).strip().splitlines()
    return False, (detail[-1][:120] if detail else "failed")


def _attribution(project):
    """Map task id -> the agent that answered it.

    `collab log` emits one JSON array. Responses are linked back to their task by
    parent_message_id, and the task id is recovered from the idempotency key we
    posted with -- so attribution never depends on the worker echoing anything.
    """
    owner = {}
    r = _collab(["log", "--project", project])
    if r.returncode:
        return owner
    try:
        entries = json.loads(r.stdout)
    except (ValueError, TypeError):
        return owner
    if not isinstance(entries, list):
        return owner

    prefix = f"scale-{project}-"
    task_of = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = e.get("idempotency_key") or ""
        if key.startswith(prefix):
            task_of[e.get("message_id")] = key[len(prefix):]

    for e in entries:
        if not isinstance(e, dict) or e.get("type") != "response":
            continue
        task_id = task_of.get(e.get("parent_message_id"))
        agent = e.get("from_agent")
        if task_id and agent:
            owner.setdefault(task_id, agent)
    return owner


def cmd_grade(args):
    owner = _attribution(args.project)
    rows = []
    for task in TASKS:
        ok, detail = _grade_one(task)
        rows.append({
            "id": task["id"], "band": task["band"],
            "agent": owner.get(task["id"], "unattributed"),
            "passed": ok, "detail": detail,
        })
        print(f"  {task['id']:<4} {task['band']:<7} "
              f"{'PASS' if ok else 'FAIL':<5} {rows[-1]['agent']:<18} {detail}")
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.project}-{int(time.time())}.json"
    out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    passed = sum(1 for r in rows if r["passed"])
    print(f"\n{passed}/{len(rows)} held-out tests pass -> {out}")


def cmd_report(args):
    files = sorted(RESULTS.glob(f"{args.project}-*.json"))
    if not files:
        print(f"no results for {args.project!r}; run `grade` first", file=sys.stderr)
        return 1
    rows = json.loads(files[-1].read_text(encoding="utf-8"))
    agents = sorted({r["agent"] for r in rows})
    print(f"\n{'agent':<20} {'easy':>6} {'medium':>8} {'hard':>6} {'total':>8}")
    print("-" * 52)
    for agent in agents:
        mine = [r for r in rows if r["agent"] == agent]
        cells = []
        for band in BANDS:
            b = [r for r in mine if r["band"] == band]
            cells.append(f"{sum(1 for r in b if r['passed'])}/{len(b)}" if b else "-")
        tot = f"{sum(1 for r in mine if r['passed'])}/{len(mine)}"
        print(f"{agent:<20} {cells[0]:>6} {cells[1]:>8} {cells[2]:>6} {tot:>8}")
    print("\nper band, all agents:")
    for band in BANDS:
        b = [r for r in rows if r["band"] == band]
        if b:
            print(f"  {band:<7} {sum(1 for r in b if r['passed'])}/{len(b)}")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="materialize the task workspace")
    s.add_argument("--force", action="store_true", help="reset impl.py stubs")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("post", help="push tasks onto the bus")
    s.add_argument("--project", required=True)
    s.add_argument("--architect", default="claude-opus")
    s.set_defaults(func=cmd_post)

    s = sub.add_parser("grade", help="run held-out tests and attribute per agent")
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_grade)

    s = sub.add_parser("report", help="quality table by agent and band")
    s.add_argument("--project", required=True)
    s.set_defaults(func=cmd_report)

    args = p.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())

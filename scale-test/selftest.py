#!/usr/bin/env python3
"""Prove the benchmark still discriminates, then restore the stubs.

Plants implementations that PASS the public test but are subtly wrong, plus a few
that are correct. If a wrong one scores a pass, the held-out test for that task has
gone soft and the band is no longer measuring anything.

    python selftest.py
"""
import subprocess
import sys

from harness import TASKS, WORKSPACE, _grade_one

WRONG = {
    "E1": '''
def chunk(seq, size):
    return [list(seq[i:i + size]) for i in range(0, len(seq), size)]
''',
    "E4": '''
def flatten(nested):
    out = []
    for x in nested:
        if hasattr(x, "__iter__"):
            out.extend(flatten(x))
        else:
            out.append(x)
    return out
''',
    "H1": '''
def compare_semver(a, b):
    ta = tuple(int(p) for p in a.split("+")[0].split("-")[0].split("."))
    tb = tuple(int(p) for p in b.split("+")[0].split("-")[0].split("."))
    return (ta > tb) - (ta < tb)
''',
    "H3": '''
class LRUCacheTTL:
    def __init__(self, capacity, ttl_seconds):
        self.cap = capacity
        self.ttl = ttl_seconds
        self.d = {}

    def get(self, key, now):
        if key in self.d:
            value, stamp = self.d[key]
            if now - stamp <= self.ttl:
                del self.d[key]
                self.d[key] = (value, now)
                return value
            del self.d[key]
        return None

    def put(self, key, value, now):
        self.d.pop(key, None)
        self.d[key] = (value, now)
        if len(self.d) > self.cap:
            self.d.pop(next(iter(self.d)))
''',
}

RIGHT = {
    "E3": '''
def clamp(value, lo, hi):
    if lo > hi:
        raise ValueError("lo must not exceed hi")
    return max(lo, min(value, hi))
''',
    "M1": '''
def merge_intervals(intervals):
    if not intervals:
        return []
    out = []
    for start, end in sorted(intervals):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out
''',
    "M3": '''
def backoff_delays(attempts, base, cap):
    if attempts < 0:
        raise ValueError("attempts must be >= 0")
    return [min(base * (2 ** i), cap) for i in range(attempts)]
''',
}


def main():
    by_id = {t["id"]: t for t in TASKS}
    failures = []

    for task_id, code in {**WRONG, **RIGHT}.items():
        (WORKSPACE / task_id / "impl.py").write_text(code.lstrip(), encoding="utf-8")

    print("public tests (every planted impl must pass these):")
    for task_id in {**WRONG, **RIGHT}:
        r = subprocess.run([sys.executable, "check.py"], cwd=str(WORKSPACE / task_id),
                           capture_output=True, text=True, timeout=30)
        mark = "ok" if r.returncode == 0 else "UNEXPECTED FAIL"
        print(f"  {task_id}: {mark}")
        if r.returncode != 0:
            failures.append(f"{task_id} should pass its public test")

    print("\nheld-out grading:")
    for task_id in WRONG:
        ok, detail = _grade_one(by_id[task_id])
        print(f"  {task_id} wrong   -> {'PASS' if ok else 'caught'}: {detail}")
        if ok:
            failures.append(f"{task_id}: held-out test failed to catch a known-bad impl")
    for task_id in RIGHT:
        ok, detail = _grade_one(by_id[task_id])
        print(f"  {task_id} correct -> {'pass' if ok else 'FALSE NEGATIVE'}: {detail}")
        if not ok:
            failures.append(f"{task_id}: held-out test rejected a correct impl")

    for task in TASKS:  # restore stubs so a real run starts clean
        (WORKSPACE / task["id"] / "impl.py").write_text(task["stub"], encoding="utf-8")
    print("\nstubs restored.")

    if failures:
        print("\nSELFTEST FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"SELFTEST OK: {len(WRONG)}/{len(WRONG)} known-bad caught, "
          f"{len(RIGHT)}/{len(RIGHT)} correct accepted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

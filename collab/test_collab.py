#!/usr/bin/env python3
"""Phase 1 exit-criteria tests for collab.py.

Covers: full convergence flow, redelivery/idempotency, broadcast fan-out,
claim-collision (concurrent), crash-after-post-before-ack (atomic complete),
and stale-worker fencing (claim_token).
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest

from collab import Store, CollabError, watch, _bind_payload

FAKE_AGENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_agent.py")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="collab_test_")
        self.s = Store(self.tmp)

    def tearDown(self):
        self.s.close()

    def fresh_store(self):
        """A second connection to the same root (simulates another process)."""
        return Store(self.tmp)


class TestConvergenceFlow(Base):
    def test_full_loop(self):
        s = self.s
        s.start("A", "queue schema", "agree on a design", "claude-1")
        s.join("A", "codex-1")
        s.join("A", "copilot-1")

        # v1 of the work product
        art = s.put_artifact("A", "spec.md", b"# Spec v1\nnormalize the queue?\n", "claude-1")
        self.assertEqual(art["artifact"], "spec.md@v1")

        # initiator broadcasts a review request
        rr = s.post("A", "claude-1", "broadcast", "review_request",
                    "please review spec.md@v1", round_=1, refs={"artifact": "spec.md@v1"})
        self.assertFalse(rr["duplicate"])

        # both reviewers see it (fan-out) and respond independently
        for reviewer in ("codex-1", "copilot-1"):
            claimed = s.claim("A", reviewer)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["type"], "review_request")
            s.complete("A", reviewer, claimed["claim_message_id"], claimed["claim_token"],
                       "response", f"{reviewer} says: looks mostly good, one issue",
                       round_=1, parent=claimed["claim_message_id"],
                       idempotency_key=f"{reviewer}:resp:{claimed['claim_message_id']}:r1")

        responses = [m for m in s.log("A") if m["type"] == "response"]
        self.assertEqual(len(responses), 2)

        # responses are routed to the initiator (reply-to-sender), who now has
        # two items to reconcile; peer reviewers were NOT spammed
        self.assertEqual(len(s.poll("A", "claude-1")), 2)
        self.assertEqual(len(s.poll("A", "codex-1")), 0)
        self.assertEqual(len(s.poll("A", "copilot-1")), 0)

        # initiator reconciles each response (here: just acks after reading)
        while True:
            c = s.claim("A", "claude-1")
            if c is None:
                break
            s.ack("A", "claude-1", c["claim_message_id"], c["claim_token"])

        # initiator converges (decision is log-only, creates no work)
        dec = s.decide("A", "claude-1", "Going with normalized schema. Decision logged.")
        self.assertEqual(dec["state"], "converged")
        self.assertEqual(s.get_project("A")["state"], "converged")

        st = s.status("A")
        self.assertEqual(st["state"], "converged")
        self.assertEqual(st["pending"], {})  # everything handled
        # Codex finding #2: a converged project reports no open threads
        self.assertEqual(st["open_threads"], [])

    def test_nested_replies_stay_in_one_thread(self):
        """Codex finding: a multi-hop convergence (review_request -> response ->
        proposal -> rebuttal) must stay in the original thread, not fork."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")

        rr = s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        root_thread = [m for m in s.log("A")
                       if m["message_id"] == rr["message_id"]][0]["thread_id"]

        # codex responds to the review request
        c1 = s.claim("A", "codex-1")
        s.complete("A", "codex-1", c1["claim_message_id"], c1["claim_token"],
                   "response", "here's my critique", round_=1)

        # claude claims codex's response and replies with a proposal
        c2 = s.claim("A", "claude-1")
        s.complete("A", "claude-1", c2["claim_message_id"], c2["claim_token"],
                   "proposal", "spec v2, accepting points 1-2", round_=2,
                   role="initiator")

        # codex claims the proposal and rebuts
        c3 = s.claim("A", "codex-1")
        s.complete("A", "codex-1", c3["claim_message_id"], c3["claim_token"],
                   "rebuttal", "point 2 still wrong because…", round_=2)

        threads = {m["thread_id"] for m in s.log("A")}
        self.assertEqual(threads, {root_thread})  # all four messages, one thread

    def test_open_threads_before_and_after_decision(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        rr = s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        self.assertEqual(len(s.status("A")["open_threads"]), 1)  # review thread open
        # decision attached to the review thread also closes it explicitly
        s.decide("A", "claude-1", "decided", thread_id=rr["message_id"])
        self.assertEqual(s.status("A")["open_threads"], [])


class TestIdempotency(Base):
    def test_redelivery_same_key_dedupes(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        p = s.post("A", "claude-1", "codex-1", "review_request", "rev",
                   round_=1)["message_id"]

        # an identical logical write retried with the same key is a safe no-op
        a = s.post("A", "codex-1", "claude-1", "response", "crit",
                   parent=p, round_=1, idempotency_key="K")
        b = s.post("A", "codex-1", "claude-1", "response", "crit",
                   parent=p, round_=1, idempotency_key="K")
        self.assertFalse(a["duplicate"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(a["message_id"], b["message_id"])

        responses = [m for m in s.log("A") if m["type"] == "response"]
        self.assertEqual(len(responses), 1)

    def test_different_round_not_deduped(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        p = "parent-msg"
        r1 = s.post("A", "codex-1", "broadcast", "response", "round1",
                    round_=1, idempotency_key=f"codex-1:resp:{p}:r1")
        r2 = s.post("A", "codex-1", "broadcast", "response", "round2",
                    round_=2, idempotency_key=f"codex-1:resp:{p}:r2")
        self.assertFalse(r1["duplicate"])
        self.assertFalse(r2["duplicate"])
        self.assertNotEqual(r1["message_id"], r2["message_id"])


class TestBroadcastFanout(Base):
    def test_each_reviewer_gets_own_row(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.join("A", "copilot-1")
        rr = s.post("A", "claude-1", "broadcast", "review_request", "rev", round_=1)

        # both reviewers have exactly one pending row; sender has none
        self.assertEqual(len(s.poll("A", "codex-1")), 1)
        self.assertEqual(len(s.poll("A", "copilot-1")), 1)
        self.assertEqual(len(s.poll("A", "claude-1")), 0)

        # one reviewer claiming does NOT remove the other's row
        s.claim("A", "codex-1")
        self.assertEqual(len(s.poll("A", "codex-1")), 0)
        self.assertEqual(len(s.poll("A", "copilot-1")), 1)  # untouched


class TestLateJoinBackfill(Base):
    def test_reviewer_joining_after_broadcast_gets_the_work(self):
        """Codex finding: start -> broadcast -> join must NOT drop the review."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        rr = s.post("A", "claude-1", "broadcast", "review_request", "rev", round_=1)
        # no reviewers yet -> warning, zero recipients
        self.assertIn("warning", rr)

        # codex joins AFTER the broadcast and is backfilled
        res = s.join("A", "codex-1")
        self.assertEqual(res["backfilled"], 1)
        self.assertEqual(len(s.poll("A", "codex-1")), 1)

        # a second late joiner also gets it
        res2 = s.join("A", "copilot-1")
        self.assertEqual(res2["backfilled"], 1)
        self.assertEqual(len(s.poll("A", "copilot-1")), 1)

    def test_backfill_skips_decided_threads_and_own_messages(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        rr = s.post("A", "claude-1", "broadcast", "review_request", "rev", round_=1)
        s.decide("A", "claude-1", "done", thread_id=rr["message_id"])
        # joining after the thread is decided backfills nothing
        self.assertEqual(s.join("A", "codex-1")["backfilled"], 0)
        self.assertEqual(len(s.poll("A", "codex-1")), 0)


class TestThreadingViaPost(Base):
    def test_post_parent_stays_in_root_thread(self):
        """Codex finding: post --parent <response> must inherit the parent's
        thread, not fork to thread=parent."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        rr = s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        root = [m for m in s.log("A")
                if m["message_id"] == rr["message_id"]][0]["thread_id"]

        # codex responds
        resp = s.post("A", "codex-1", "claude-1", "response", "critique",
                      parent=rr["message_id"], round_=1)
        # claude rebuts the RESPONSE via post --parent (not complete)
        reb = s.post("A", "claude-1", "codex-1", "rebuttal", "disagree",
                     parent=resp["message_id"], round_=2)

        threads = {m["thread_id"] for m in s.log("A")}
        self.assertEqual(threads, {root})  # all in one thread, no fork


class TestClaimCollision(Base):
    def test_direct_message_claimed_once(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "question", "q1", round_=1)

        first = s.claim("A", "codex-1")
        second = s.claim("A", "codex-1")
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # nothing left to claim

    def test_concurrent_claimers_no_double(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        # 20 direct messages for one agent
        for i in range(20):
            s.post("A", "claude-1", "codex-1", "question", f"q{i}", round_=1)

        claimed_ids = []
        lock = threading.Lock()

        def worker():
            st = self.fresh_store()
            try:
                while True:
                    c = st.claim("A", "codex-1")
                    if c is None:
                        return
                    with lock:
                        claimed_ids.append(c["claim_message_id"])
                    time.sleep(0.001)
            finally:
                st.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        # every message claimed exactly once, none lost, none duplicated
        self.assertEqual(len(claimed_ids), 20)
        self.assertEqual(len(set(claimed_ids)), 20)


class TestAtomicComplete(Base):
    def test_complete_posts_and_acks_together(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        claimed = s.claim("A", "codex-1")

        s.complete("A", "codex-1", claimed["claim_message_id"], claimed["claim_token"],
                   "response", "done", round_=1)

        # input row is done (no redelivery) AND response exists — atomically
        self.assertEqual(len(s.poll("A", "codex-1")), 0)
        responses = [m for m in s.log("A") if m["type"] == "response"]
        self.assertEqual(len(responses), 1)

    def test_key_collision_raises_and_does_not_ack(self):
        """Codex finding #1: a key reused for a DIFFERENT logical write must not be
        silently treated as a duplicate — that would ack the claimed work item
        without ever creating the response, losing review work while reporting success.
        The collision must raise, roll back, and leave the work reclaimable."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)

        # an unrelated earlier message already burned key "K"
        s.post("A", "codex-1", "claude-1", "response", "unrelated",
               parent="some-other-msg", round_=1, idempotency_key="K")

        claimed = s.claim("A", "codex-1")
        # completing the review with the colliding key must RAISE, not ack
        with self.assertRaises(CollabError):
            s.complete("A", "codex-1", claimed["claim_message_id"],
                       claimed["claim_token"], "response", "real critique",
                       round_=1, idempotency_key="K")

        # the row was rolled back, not acked — the worker still owns it and can
        # retry with a correct key; the review work is not lost
        ok = s.complete("A", "codex-1", claimed["claim_message_id"],
                        claimed["claim_token"], "response", "real critique",
                        round_=1, idempotency_key="K2")
        self.assertFalse(ok["duplicate"])
        resp = [m for m in s.log("A") if m["type"] == "response"
                and m["parent_message_id"] == claimed["claim_message_id"]]
        self.assertEqual(len(resp), 1)

    def test_complete_posts_and_acks_atomically(self):
        """The claimed row is acked iff the response is created — all or nothing."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        claimed = s.claim("A", "codex-1")
        s.complete("A", "codex-1", claimed["claim_message_id"],
                   claimed["claim_token"], "response", "x", round_=1)
        self.assertEqual(len(s.poll("A", "codex-1")), 0)
        self.assertEqual(len([m for m in s.log("A") if m["type"] == "response"]), 1)


class TestFencing(Base):
    def test_stale_token_cannot_complete(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)

        # worker 1 claims, then its lease is force-expired and the row reclaimed
        c1 = s.claim("A", "codex-1")
        # expire the lease manually
        s.conn.execute(
            "UPDATE inbox SET leased_until=? WHERE message_id=? AND recipient=?",
            ("2000-01-01T00:00:00.000000Z", c1["claim_message_id"], "codex-1"),
        )
        # sweeper returns it to pending, worker 2 reclaims with a new token
        c2 = s.claim("A", "codex-1")
        self.assertEqual(c1["claim_message_id"], c2["claim_message_id"])
        self.assertNotEqual(c1["claim_token"], c2["claim_token"])

        # worker 1 (stale token) must be fenced out
        with self.assertRaises(CollabError):
            s.complete("A", "codex-1", c1["claim_message_id"], c1["claim_token"],
                       "response", "stale work", round_=1)

        # worker 2 (current token) succeeds
        ok = s.complete("A", "codex-1", c2["claim_message_id"], c2["claim_token"],
                        "response", "fresh work", round_=1)
        self.assertFalse(ok["duplicate"])

    def test_stale_token_cannot_ack(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "question", "q", round_=1)
        c1 = s.claim("A", "codex-1")
        s.conn.execute(
            "UPDATE inbox SET leased_until=? WHERE message_id=? AND recipient=?",
            ("2000-01-01T00:00:00.000000Z", c1["claim_message_id"], "codex-1"),
        )
        c2 = s.claim("A", "codex-1")
        with self.assertRaises(CollabError):
            s.ack("A", "codex-1", c1["claim_message_id"], c1["claim_token"])
        # current owner can ack
        self.assertEqual(
            s.ack("A", "codex-1", c2["claim_message_id"], c2["claim_token"])["acked"],
            c2["claim_message_id"],
        )


class TestWatcher(Base):
    def _devnull(self):
        return io.StringIO()

    def test_watch_processes_and_responds_handsoff(self):
        """Phase 3: the watcher claims work, invokes the agent over stdin, and
        posts the agent's stdout back as a response — no human relaying."""
        s = self.s
        s.start("A", "queue schema", "agree", "claude-1")
        a = s.put_artifact("A", "spec.md", b"# v1\nnormalize?", "claude-1")
        s.post("A", "claude-1", "codex-1", "review_request", "review the spec",
               round_=1, refs={"artifact": a["artifact"]})

        env = dict(os.environ)  # default FAKE_AGENT_MODE=ok
        n = watch(s, "A", "codex-1", [sys.executable, FAKE_AGENT],
                  once=True, lease_min=10, log_fh=self._devnull())
        self.assertEqual(n, 1)

        responses = [m for m in s.log("A") if m["type"] == "response"]
        self.assertEqual(len(responses), 1)
        self.assertIn("REVIEW", responses[0]["body"])
        self.assertIn("with artifact", responses[0]["body"])  # agent saw the artifact
        self.assertEqual(responses[0]["from_agent"], "codex-1")
        self.assertEqual(responses[0]["to_agent"], "claude-1")  # reply-to-sender
        self.assertEqual(len(s.poll("A", "codex-1")), 0)        # inbox drained

    def test_watch_failure_leaves_work_for_redelivery(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        env_was = os.environ.get("FAKE_AGENT_MODE")
        os.environ["FAKE_AGENT_MODE"] = "fail"
        try:
            n = watch(s, "A", "codex-1", [sys.executable, FAKE_AGENT],
                      once=True, lease_min=10, log_fh=self._devnull())
        finally:
            if env_was is None:
                os.environ.pop("FAKE_AGENT_MODE", None)
            else:
                os.environ["FAKE_AGENT_MODE"] = env_was
        self.assertEqual(n, 0)  # nothing completed
        self.assertEqual([m for m in s.log("A") if m["type"] == "response"], [])
        # the claim is still out (status 'claimed'); it returns to pending on sweep
        s.conn.execute("UPDATE inbox SET leased_until='2000-01-01T00:00:00.000000Z' "
                       "WHERE recipient='codex-1'")
        s.sweep("A")
        self.assertEqual(len(s.poll("A", "codex-1")), 1)  # reclaimable, not lost

    def test_watch_heartbeat_keeps_long_review_alive(self):
        """A review that runs longer than the lease must NOT be redelivered: the
        background heartbeat extends the lease while the agent works."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)

        env_was = os.environ.get("FAKE_AGENT_SLEEP")
        os.environ["FAKE_AGENT_SLEEP"] = "4"   # agent takes 4s
        try:
            # lease is 0.05 min = 3s, SHORTER than the 4s review; heartbeat (every 1s)
            # must keep it alive or complete() would raise 'lease expired'.
            n = watch(s, "A", "codex-1", [sys.executable, FAKE_AGENT],
                      once=True, lease_min=0.05, log_fh=self._devnull())
        finally:
            if env_was is None:
                os.environ.pop("FAKE_AGENT_SLEEP", None)
            else:
                os.environ["FAKE_AGENT_SLEEP"] = env_was

        self.assertEqual(n, 1)
        responses = [m for m in s.log("A") if m["type"] == "response"]
        self.assertEqual(len(responses), 1)
        # claimed exactly once — heartbeat prevented a sweeper redelivery
        row = s.conn.execute(
            "SELECT deliveries, status FROM inbox WHERE recipient='codex-1'"
        ).fetchone()
        self.assertEqual(row["deliveries"], 1)
        self.assertEqual(row["status"], "done")


class TestWatcherHardening(Base):
    def _devnull(self):
        return io.StringIO()

    def _setup_review(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        return s

    def _set_env(self, **kv):
        """Set env vars, returning a restore callable."""
        prev = {k: os.environ.get(k) for k in kv}
        os.environ.update({k: str(v) for k, v in kv.items()})

        def restore():
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return restore

    def test_hung_agent_is_killed_on_timeout(self):
        """Codex finding #1: a hung agent must be killed, not hold the lease forever."""
        s = self._setup_review()
        restore = self._set_env(FAKE_AGENT_MODE="hang")
        try:
            start = time.time()
            n = watch(s, "A", "codex-1", [sys.executable, FAKE_AGENT],
                      once=True, lease_min=10, agent_timeout=1.0,
                      max_deliveries=5, log_fh=self._devnull())
            elapsed = time.time() - start
        finally:
            restore()
        self.assertEqual(n, 0)                 # nothing completed
        self.assertLess(elapsed, 8)            # killed promptly, didn't hang
        self.assertEqual([m for m in s.log("A") if m["type"] == "response"], [])

    def test_poison_message_stalls_after_max_deliveries(self):
        """Codex finding #2: failures are bounded — the message is taken out of
        rotation after max_deliveries instead of retrying forever."""
        s = self._setup_review()
        restore = self._set_env(FAKE_AGENT_MODE="fail")
        try:
            # delivery 1: fails, below bound -> left for redelivery
            watch(s, "A", "codex-1", [sys.executable, FAKE_AGENT], once=True,
                  lease_min=10, max_deliveries=2, log_fh=self._devnull())
            s.conn.execute("UPDATE inbox SET leased_until='2000-01-01T00:00:00.000000Z' "
                           "WHERE recipient='codex-1'")
            s.sweep("A")
            # delivery 2: fails, hits bound -> stalled
            watch(s, "A", "codex-1", [sys.executable, FAKE_AGENT], once=True,
                  lease_min=10, max_deliveries=2, log_fh=self._devnull())
        finally:
            restore()
        row = s.conn.execute(
            "SELECT status, deliveries FROM inbox WHERE recipient='codex-1'"
        ).fetchone()
        self.assertEqual(row["deliveries"], 2)
        self.assertEqual(row["status"], "stalled")
        # stalled work is out of rotation: not reclaimable, not swept back
        s.conn.execute("UPDATE inbox SET leased_until='2000-01-01T00:00:00.000000Z' "
                       "WHERE recipient='codex-1'")
        s.sweep("A")
        self.assertIsNone(s.claim("A", "codex-1"))
        # surfaced: status reports the stalled row, and the log has an audit entry
        st = s.status("A")
        self.assertEqual(len(st["stalled"]), 1)
        self.assertEqual(st["stalled"][0]["recipient"], "codex-1")
        self.assertTrue(any(m["type"] == "status" and "stalled" in (m["body"] or "")
                            for m in s.log("A")))
        # the status audit must NOT create a phantom open thread: the only open
        # thread is the original (actionable) review thread
        review_thread = [m["thread_id"] for m in s.log("A")
                         if m["type"] == "review_request"][0]
        self.assertEqual(st["open_threads"], [review_thread])

    def test_mark_stalled_is_fenced_by_token(self):
        """Codex finding #1: a stale worker must not stall the current owner's row."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        c1 = s.claim("A", "codex-1")
        # lease expires, another worker reclaims with a fresh token
        s.conn.execute("UPDATE inbox SET leased_until='2000-01-01T00:00:00.000000Z' "
                       "WHERE recipient='codex-1'")
        c2 = s.claim("A", "codex-1")
        self.assertNotEqual(c1["claim_token"], c2["claim_token"])
        # stale worker cannot stall it
        with self.assertRaises(CollabError):
            s.mark_stalled("A", c1["claim_message_id"], "codex-1", c1["claim_token"])
        # current owner can
        s.mark_stalled("A", c2["claim_message_id"], "codex-1", c2["claim_token"])
        row = s.conn.execute(
            "SELECT status FROM inbox WHERE recipient='codex-1'").fetchone()
        self.assertEqual(row["status"], "stalled")

    def test_complete_lease_loss_does_not_crash_watcher(self):
        """Codex finding #3: if the lease is lost at complete time, the watcher
        logs and continues instead of crashing."""
        s = self._setup_review()
        # agent succeeds, but completing raises (simulated lost lease)
        orig_complete = s.complete

        def boom(*a, **k):
            raise CollabError("lease lost (simulated)")
        s.complete = boom
        try:
            n = watch(s, "A", "codex-1", [sys.executable, FAKE_AGENT],
                      once=True, lease_min=10, log_fh=self._devnull())
        finally:
            s.complete = orig_complete
        self.assertEqual(n, 0)  # returned cleanly, no exception escaped


class TestV02Usability(Base):
    def test_join_as_initiator_id_is_refused(self):
        """The identity-collision hard stop: a reviewer joining under the id that is
        already the initiator (two tools sharing one id) must RAISE, not silently
        register a self-collision. This is the bug that made wait/claim always empty."""
        s = self.s
        s.start("A", "t", "g", "claude-1")           # claude-1 is initiator
        with self.assertRaises(CollabError) as ctx:
            s.join("A", "claude-1")                  # same id tries to join as reviewer
        self.assertIn("INITIATOR", str(ctx.exception))
        # the project is unchanged: still just the one initiator
        parts = {p["agent_id"]: p["role"] for p in s.participants("A")}
        self.assertEqual(parts, {"claude-1": "initiator"})

    def test_distinct_reviewer_joins_fine(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        res = s.join("A", "codex-1")                 # distinct id -> OK
        self.assertEqual(res["role"], "reviewer")

    def test_doctor_flags_single_participant_collision(self):
        s = self.s
        s.start("A", "t", "g", "codex-1")
        d = s.doctor("A", "codex-1")    # only the initiator present (the failure shape)
        self.assertTrue(any("Only one participant" in h for h in d["hints"]))

    def test_doctor_flags_no_review_request(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")          # two distinct agents, but nothing posted
        d = s.doctor("A", "codex-1")
        self.assertFalse(d["ready_for_review"])
        self.assertTrue(any("No review request" in h for h in d["hints"]))

    def test_review_verb_makes_a_reviewable_project(self):
        s = self.s
        p = os.path.join(self.tmp, "spec.md")
        with open(p, "w") as fh:
            fh.write("# spec\nfixed window?\n")
        out = s.review("A", "claude-1", p, topic="x", goal="y",
                       focus="is fixed-window right?")
        self.assertEqual(out["artifact"], "spec.md@v1")
        # a reviewer who joins now is backfilled the request -> genuinely reviewable
        s.join("A", "codex-1")
        self.assertEqual(len(s.poll("A", "codex-1")), 1)
        self.assertTrue(s.doctor("A", "codex-1")["ready_for_review"])

    def test_review_requires_a_real_file(self):
        s = self.s
        with self.assertRaises(CollabError):
            s.review("A", "claude-1", os.path.join(self.tmp, "nope.md"))

    def test_list_projects(self):
        s = self.s
        self.assertEqual(s.list_projects()["count"], 0)
        s.start("A", "t", "g", "claude-1")
        s.start("B", "t", "g", "claude-1")
        s.join("B", "codex-1")
        lp = s.list_projects()
        self.assertEqual(lp["count"], 2)
        names = {p["project"]: p for p in lp["projects"]}
        self.assertEqual(set(names), {"A", "B"})
        self.assertEqual(names["B"]["participants"], 2)

    def test_claim_wait_returns_immediately_when_work_exists(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        t0 = time.monotonic()
        c = s.claim("A", "codex-1", wait=5, poll_interval=0.1)
        self.assertIsNotNone(c)
        self.assertLess(time.monotonic() - t0, 1)  # didn't actually wait

    def test_claim_wait_times_out_when_empty(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        t0 = time.monotonic()
        c = s.claim("A", "codex-1", wait=0.3, poll_interval=0.1)
        self.assertIsNone(c)
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)  # it blocked ~the window

    def test_claim_wait_picks_up_work_posted_during_window(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")

        def post_later():
            time.sleep(0.2)
            st = self.fresh_store()
            try:
                st.post("A", "claude-1", "codex-1", "review_request", "late", round_=1)
            finally:
                st.close()

        th = threading.Thread(target=post_later)
        th.start()
        c = s.claim("A", "codex-1", wait=3, poll_interval=0.1)
        th.join()
        self.assertIsNotNone(c)
        self.assertEqual(c["type"], "review_request")

    def test_decide_clears_outstanding_pending(self):
        """A converged project must not leave permanent 'pending' notifications in a
        participant's inbox (the post-convergence noise from the real run)."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        rr = s.post("A", "claude-1", "broadcast", "review_request", "rev", round_=1)
        # claude sends codex an unrelated actionable message that codex never claims
        s.post("A", "claude-1", "codex-1", "response", "fyi", round_=1)
        self.assertGreater(sum(s.status("A")["pending"].values()), 0)
        s.decide("A", "claude-1", "done", thread_id=rr["message_id"])
        st = s.status("A")
        self.assertEqual(st["state"], "converged")
        self.assertEqual(st["pending"], {})        # nothing left pending
        self.assertEqual(st["open_threads"], [])

    def test_decide_closes_claimed_row_inflight_complete_fails(self):
        """Terminal race: if a reviewer has a row claimed (lease held) and the initiator
        decides, decide() marks that row done. The reviewer's in-flight complete() must
        then fail with a clear terminal error -- a binding decision wins over late work,
        and a response cannot be posted into an already-converged thread."""
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        rr = s.post("A", "claude-1", "broadcast", "review_request", "rev", round_=1)
        # codex claims the review_request and is "mid-review" (lease held)
        claimed = s.claim("A", "codex-1")
        self.assertEqual(claimed["type"], "review_request")
        # initiator converges the thread while codex's row is still claimed
        s.decide("A", "claude-1", "done", thread_id=rr["message_id"])
        # codex's now-stale complete must fail because the row is terminal (done)
        with self.assertRaises(CollabError) as ctx:
            s.complete("A", "codex-1", claimed["claim_message_id"], claimed["claim_token"],
                       "response", "late review", round_=1,
                       parent=claimed["claim_message_id"])
        self.assertIn("is done", str(ctx.exception))
        # project stays clean: converged, no pending, no open threads
        st = s.status("A")
        self.assertEqual(st["state"], "converged")
        self.assertEqual(st["pending"], {})
        self.assertEqual(st["open_threads"], [])

    def test_delete_project(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        s.delete_project("A")
        self.assertEqual(s.list_projects()["count"], 0)
        # gone from every table
        self.assertEqual(
            s.conn.execute("SELECT COUNT(*) AS n FROM messages WHERE project='A'"
                           ).fetchone()["n"], 0)
        with self.assertRaises(CollabError):
            s.get_project("A")
        # deleting a missing project errors
        with self.assertRaises(CollabError):
            s.delete_project("nope")


class TestArtifacts(Base):
    def test_versioning_and_hash_verification(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        a1 = s.put_artifact("A", "spec.md", b"v1 content", "claude-1")
        a2 = s.put_artifact("A", "spec.md", b"v2 content", "claude-1")
        self.assertEqual(a1["version"], 1)
        self.assertEqual(a2["version"], 2)

        row, data = s.get_artifact("A", "spec.md")  # latest
        self.assertEqual(data, b"v2 content")
        row1, data1 = s.get_artifact("A", "spec.md", version=1)
        self.assertEqual(data1, b"v1 content")

        # identical content dedupes to one blob
        a3 = s.put_artifact("A", "other.md", b"v1 content", "claude-1")
        self.assertEqual(a3["sha256"], a1["sha256"])


class TestVersionConsistency(unittest.TestCase):
    """Guard against the multi-manifest version drift seen in the 0.2.9 run (one manifest
    bumped, the others left behind). Skips when run outside the repo tree."""

    def test_manifest_versions_agree(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        claude = os.path.join(root, "plugins/agent-collab/.claude-plugin/plugin.json")
        codex = os.path.join(root, "plugins/agent-collab/.codex-plugin/plugin.json")
        market = os.path.join(root, ".claude-plugin/marketplace.json")
        if not (os.path.exists(claude) and os.path.exists(codex)
                and os.path.exists(market)):
            self.skipTest("manifests not present (running outside the repo tree)")
        cv = json.load(open(claude))["version"]
        xv = json.load(open(codex))["version"]
        mv = json.load(open(market))["plugins"][0]["version"]
        self.assertEqual(
            {cv, xv, mv}, {cv},
            f"manifest version drift: claude={cv} codex={xv} marketplace={mv}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPresenceAndInbox(Base):
    """Phase-4 ergonomics: presence classification, directed-message footgun warnings,
    the inbox view/drain, and log filters."""

    @staticmethod
    def _ago(**kw):
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone.utc) - timedelta(**kw)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")

    def test_presence_classification(self):
        from collab import presence, now_iso
        self.assertEqual(presence(None)[0], "unknown")
        self.assertEqual(presence("not-a-timestamp")[0], "unknown")
        self.assertEqual(presence(now_iso())[0], "online")
        self.assertEqual(presence(self._ago(minutes=10))[0], "idle")
        self.assertEqual(presence(self._ago(hours=2))[0], "offline")

    def test_status_exposes_presence(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        parts = {p["agent"]: p for p in s.status("A")["participants"]}
        self.assertEqual(parts["codex-1"]["presence"], "online")
        self.assertIn("last_seen_age_s", parts["codex-1"])

    def test_directed_status_warns_and_creates_no_inbox(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        rr = s.post("A", "claude-1", "codex-1", "status", "fyi")
        self.assertIn("warning", rr)
        self.assertIn("log-only", rr["warning"])
        self.assertEqual(len(s.poll("A", "codex-1")), 0)  # no inbox row

    def test_directed_actionable_to_offline_warns_but_queues(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        with s.write_tx():
            s.conn.execute(
                "UPDATE participants SET last_heartbeat=? WHERE project=? AND agent_id=?",
                (self._ago(hours=2), "A", "codex-1"))
        rr = s.post("A", "claude-1", "codex-1", "review_request", "rev")
        self.assertIn("warning", rr)
        self.assertEqual(rr.get("recipient_presence"), "offline")
        self.assertEqual(len(s.poll("A", "codex-1")), 1)  # still queued durably

    def test_online_directed_actionable_has_no_warning(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        rr = s.post("A", "claude-1", "codex-1", "review_request", "rev")
        self.assertNotIn("warning", rr)

    def test_inbox_view_and_drain(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev one")
        s.post("A", "claude-1", "codex-1", "question", "q two")
        view = s.inbox_view("A", "codex-1")
        self.assertEqual(view["pending"], 2)
        self.assertEqual(view["items"][0]["type"], "review_request")
        self.assertEqual(view["items"][0]["from"], "claude-1")
        drained = s.inbox_drain("A", "codex-1")
        self.assertEqual(drained["drained"], 2)
        self.assertEqual(s.inbox_view("A", "codex-1")["pending"], 0)
        # drain is idempotent on an empty inbox
        self.assertEqual(s.inbox_drain("A", "codex-1")["drained"], 0)

    def test_log_filters(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev")
        s.post("A", "claude-1", "codex-1", "status", "fyi")
        self.assertEqual(len(s.log("A", actionable_only=True)), 1)
        self.assertEqual(len(s.log("A", to_agent="codex-1")), 2)
        self.assertEqual(len(s.log("A", from_agent="codex-1")), 0)


class TestPayloadBinding(Base):
    """The watcher feeds the agent on stdin by default, or as an argument when the
    exec_argv contains the `{}` placeholder — so a prompt-as-arg CLI (GitHub Copilot's
    `copilot -p <text>`) works without a wrapper, while `codex exec` (stdin) is
    unchanged."""

    def test_default_is_stdin(self):
        argv, stdin_text = _bind_payload(["codex", "exec"], "REVIEW THIS")
        self.assertEqual(argv, ["codex", "exec"])
        self.assertEqual(stdin_text, "REVIEW THIS")

    def test_placeholder_becomes_argument_and_no_stdin(self):
        argv, stdin_text = _bind_payload(
            ["copilot", "--allow-all-tools", "-p", "{}"], "REVIEW THIS")
        self.assertEqual(argv, ["copilot", "--allow-all-tools", "-p", "REVIEW THIS"])
        self.assertIsNone(stdin_text)  # prompt is an arg now; nothing on stdin

    def test_placeholder_embedded_in_token(self):
        argv, stdin_text = _bind_payload(["tool", "--prompt={}"], "HELLO")
        self.assertEqual(argv, ["tool", "--prompt=HELLO"])
        self.assertIsNone(stdin_text)

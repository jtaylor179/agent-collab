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

from collab import Store, CollabError, watch, _bind_payload, _agent_payload

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


class TestApproverRole(Base):
    """v0.3.5: an approver reviews like a reviewer, and decide() is gated on every
    approver having posted an `approval` message (unless force=True)."""

    def _project_with_approver(self):
        s = self.s
        s.start("A", "spec", "converge", "claude-1")
        s.join("A", "codex-1")                     # plain reviewer
        s.grant_role("A", "claude-1", "copilot-1", "approver")  # owner grants approver
        s.put_artifact("A", "spec.md", b"# v1\n", "claude-1")
        s.post("A", "claude-1", "broadcast", "review_request", "review spec.md@v1",
               round_=1, refs={"artifact": "spec.md@v1"})
        return s

    def test_approver_gets_broadcast_fanout(self):
        s = self._project_with_approver()
        claimed = s.claim("A", "copilot-1")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["type"], "review_request")

    def test_late_joining_approver_is_backfilled(self):
        s = self.s
        s.start("A", "spec", "converge", "claude-1")
        s.post("A", "claude-1", "broadcast", "review_request", "review this", round_=1)
        out = s.grant_role("A", "claude-1", "copilot-1", "approver")
        self.assertEqual(out["role"], "approver")
        self.assertEqual(out["backfilled"], 1)

    def test_decide_blocked_until_approver_signs_off(self):
        s = self._project_with_approver()
        # A regular response from the approver is NOT a sign-off.
        claimed = s.claim("A", "copilot-1")
        s.complete("A", "copilot-1", claimed["claim_message_id"],
                   claimed["claim_token"], "response", "one objection", round_=1)
        with self.assertRaises(CollabError) as cm:
            s.decide("A", "claude-1", "converging")
        self.assertIn("copilot-1", str(cm.exception))
        # An explicit approval unblocks decide.
        s.post("A", "copilot-1", "claude-1", "approval", "objection resolved; approving")
        out = s.decide("A", "claude-1", "converging")
        self.assertEqual(out["state"], "converged")
        self.assertEqual(out["approvals"], {"copilot-1": True})

    def test_complete_with_type_approval_counts_as_sign_off(self):
        s = self._project_with_approver()
        claimed = s.claim("A", "copilot-1")
        s.complete("A", "copilot-1", claimed["claim_message_id"],
                   claimed["claim_token"], "approval", "reviewed and approved", round_=1)
        out = s.decide("A", "claude-1", "done")
        self.assertEqual(out["state"], "converged")

    def test_force_overrides_missing_approvals(self):
        s = self._project_with_approver()
        out = s.decide("A", "claude-1", "shipping anyway", force=True)
        self.assertEqual(out["state"], "converged")
        self.assertEqual(out["forced_over_missing_approvals"], ["copilot-1"])

    def test_status_and_doctor_surface_approval_gate(self):
        s = self._project_with_approver()
        self.assertEqual(s.status("A")["approvals"], {"copilot-1": False})
        doc = s.doctor("A", "claude-1")
        self.assertTrue(any("gated" in h for h in doc["hints"]))
        # the missing approver is told directly
        doc2 = s.doctor("A", "copilot-1")
        self.assertTrue(any("You are one of the missing approvers" in h
                            for h in doc2["hints"]))

    def test_no_approvers_decide_unaffected(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        out = s.decide("A", "claude-1", "done")
        self.assertEqual(out["state"], "converged")
        self.assertNotIn("approvals", out)

    def test_observer_gets_no_fanout(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1", role="observer")
        s.post("A", "claude-1", "broadcast", "review_request", "r", round_=1)
        self.assertIsNone(s.claim("A", "codex-1"))

    def test_initiator_id_cannot_join_as_approver(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        with self.assertRaises(CollabError):
            s.join("A", "claude-1", role="approver")


class TestWatcherApprover(Base):
    """v0.3.6: a hands-off approver (watcher-driven copilot/agy/codex) signs off by
    leading its output with the APPROVED marker; the watcher then posts an
    `approval` instead of a response, which is what unblocks decide()."""

    def _fake_mode(self, mode):
        env_was = os.environ.get("FAKE_AGENT_MODE")
        os.environ["FAKE_AGENT_MODE"] = mode
        def restore():
            if env_was is None:
                os.environ.pop("FAKE_AGENT_MODE", None)
            else:
                os.environ["FAKE_AGENT_MODE"] = env_was
        self.addCleanup(restore)

    def _project(self, approver_role):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        if approver_role in ("approver", "orchestrator"):
            s.grant_role("A", "claude-1", "copilot-1", approver_role)  # owner grants
        else:
            s.join("A", "copilot-1", role=approver_role)
        s.put_artifact("A", "spec.md", b"# v1\n", "claude-1")
        s.post("A", "claude-1", "broadcast", "review_request", "review spec.md@v1",
               round_=1, refs={"artifact": "spec.md@v1"})
        return s

    def test_approved_marker_posts_approval_and_unblocks_decide(self):
        s = self._project("approver")
        self._fake_mode("approve")
        n = watch(s, "A", "copilot-1", [sys.executable, FAKE_AGENT],
                  once=True, lease_min=10, log_fh=io.StringIO())
        self.assertEqual(n, 1)
        approvals = [m for m in s.log("A") if m["type"] == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertTrue(approvals[0]["body"].startswith("APPROVED"))
        out = s.decide("A", "claude-1", "done")
        self.assertEqual(out["state"], "converged")
        self.assertEqual(out["approvals"], {"copilot-1": True})

    def test_approver_objection_stays_response_and_gate_holds(self):
        s = self._project("approver")
        # default fake mode writes an objection (no APPROVED marker)
        n = watch(s, "A", "copilot-1", [sys.executable, FAKE_AGENT],
                  once=True, lease_min=10, log_fh=io.StringIO())
        self.assertEqual(n, 1)
        self.assertEqual([m["type"] for m in s.log("A") if m["from_agent"] == "copilot-1"],
                         ["response"])
        with self.assertRaises(CollabError):
            s.decide("A", "claude-1", "done")

    def test_reviewer_approved_output_is_not_promoted(self):
        # the marker only means something from an approver
        s = self._project("reviewer")
        self._fake_mode("approve")
        watch(s, "A", "copilot-1", [sys.executable, FAKE_AGENT],
              once=True, lease_min=10, log_fh=io.StringIO())
        types = [m["type"] for m in s.log("A") if m["from_agent"] == "copilot-1"]
        self.assertEqual(types, ["response"])

    def test_approver_payload_carries_approver_instructions(self):
        s = self._project("approver")
        claimed = s.claim("A", "copilot-1")
        payload = json.loads(_agent_payload(s, "A", "copilot-1", claimed))
        self.assertIn("APPROVER", payload["instructions"])
        self.assertIn("APPROVED", payload["instructions"])


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


EXPIRED = "2000-01-01T00:00:00.000000Z"


class TestInFlightAndReclaim(Base):
    """A watcher that claims a review then dies mid-run leaves the inbox row stuck in
    'claimed'. It is invisible to poll/inbox (which show only 'pending'), so status must
    surface it and reclaim must recover it without waiting out the lease."""

    def _claimed(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        c = s.claim("A", "codex-1")
        return s, c

    def test_status_surfaces_in_flight_claim(self):
        s, c = self._claimed()
        st = s.status("A")
        # poll/pending see nothing — the row is 'claimed', not 'pending'
        self.assertEqual(st["pending"], {})
        self.assertEqual(len(st["in_flight"]), 1)
        row = st["in_flight"][0]
        self.assertEqual(row["message_id"], c["claim_message_id"])
        self.assertEqual(row["recipient"], "codex-1")
        self.assertFalse(row["orphaned"])  # lease still live

    def test_orphaned_flag_set_when_lease_expired(self):
        s, c = self._claimed()
        s.conn.execute("UPDATE inbox SET leased_until=? WHERE recipient='codex-1'",
                       (EXPIRED,))
        row = s.status("A")["in_flight"][0]
        self.assertTrue(row["orphaned"])
        # doctor tells the human it's abandoned and how to recover it
        doc = s.doctor("A", "codex-1")
        self.assertEqual(len(doc["in_flight_for_you"]), 1)
        self.assertTrue(any("reclaim" in h for h in doc["hints"]))

    def test_reclaim_only_expired_by_default(self):
        s, c = self._claimed()
        # live lease is NOT reclaimed without --force
        self.assertEqual(s.reclaim("A")["reclaimed"], 0)
        s.conn.execute("UPDATE inbox SET leased_until=? WHERE recipient='codex-1'",
                       (EXPIRED,))
        res = s.reclaim("A")
        self.assertEqual(res["reclaimed"], 1)
        self.assertEqual(res["message_ids"], [c["claim_message_id"]])
        # back to pending -> a fresh watcher/claim can pick it up
        self.assertEqual(len(s.poll("A", "codex-1")), 1)
        self.assertEqual(s.status("A")["in_flight"], [])

    def test_reclaim_force_recovers_live_lease(self):
        s, c = self._claimed()
        res = s.reclaim("A", force=True)  # watcher known dead; don't wait the lease
        self.assertEqual(res["reclaimed"], 1)
        self.assertTrue(res["forced"])
        self.assertEqual(len(s.poll("A", "codex-1")), 1)

    def test_reclaim_scoped_by_agent(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.join("A", "copilot-1")
        s.post("A", "claude-1", "codex-1", "review_request", "r1", round_=1)
        s.post("A", "claude-1", "copilot-1", "review_request", "r2", round_=1)
        s.claim("A", "codex-1")
        s.claim("A", "copilot-1")
        res = s.reclaim("A", agent="codex-1", force=True)
        self.assertEqual(res["reclaimed"], 1)
        # only codex-1's row came back; copilot-1's is still in flight
        self.assertEqual(len(s.poll("A", "codex-1")), 1)
        self.assertEqual(len(s.poll("A", "copilot-1")), 0)

    def test_reclaimed_row_fences_out_the_dead_worker(self):
        """The whole point of the token: if the 'dead' watcher was only wedged and
        wakes up, its complete() on the reclaimed row must be rejected, not double-post."""
        s, c1 = self._claimed()
        s.reclaim("A", force=True)
        c2 = s.claim("A", "codex-1")  # fresh watcher reclaims with a new token
        self.assertNotEqual(c1["claim_token"], c2["claim_token"])
        with self.assertRaises(CollabError):
            s.complete("A", "codex-1", c1["claim_message_id"], c1["claim_token"],
                       "response", "stale work from a woken zombie", round_=1)
        # the live owner still completes normally
        ok = s.complete("A", "codex-1", c2["claim_message_id"], c2["claim_token"],
                        "response", "fresh work", round_=1)
        self.assertFalse(ok["duplicate"])


class TestNextAction(Base):
    """`next` collapses the board into ONE recommended action so a self-paced loop
    advances a multi-step plan hands-off instead of needing a human to re-kick it."""

    def _setup(self, reviewers=("codex-1",)):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        for r in reviewers:
            s.join("A", r)
        a = s.put_artifact("A", "spec.md", b"v1", "claude-1")
        s.post("A", "claude-1", "broadcast", "review_request", "review v1",
               round_=1, refs={"artifact": a["artifact"]})
        return s

    def test_broadcast_when_no_request_sent(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        self.assertEqual(s.next_action("A", "claude-1")["action"], "broadcast")

    def test_wait_while_reviewer_outstanding(self):
        s = self._setup(("codex-1", "copilot-1"))
        # only codex has replied; still waiting on copilot
        c = s.claim("A", "codex-1")
        s.complete("A", "codex-1", c["claim_message_id"], c["claim_token"],
                   "response", "codex: ok", round_=1)
        s.inbox_drain("A", "claude-1")  # handle codex's reply so drain doesn't mask wait
        nx = s.next_action("A", "claude-1")
        self.assertEqual(nx["action"], "wait")
        self.assertEqual(nx["responded"], ["codex-1"])
        self.assertEqual([a["agent"] for a in nx["awaiting"]], ["copilot-1"])

    def test_decide_when_all_reviewers_in(self):
        s = self._setup(("codex-1",))
        c = s.claim("A", "codex-1")
        s.complete("A", "codex-1", c["claim_message_id"], c["claim_token"],
                   "response", "codex: ok", round_=1)
        # initiator drained the reply, now all reviewers are in -> decide
        s.inbox_drain("A", "claude-1")
        nx = s.next_action("A", "claude-1")
        self.assertEqual(nx["action"], "decide")
        self.assertEqual(nx["responded"], ["codex-1"])

    def test_drain_takes_priority_over_decide(self):
        s = self._setup(("codex-1",))
        c = s.claim("A", "codex-1")
        s.complete("A", "codex-1", c["claim_message_id"], c["claim_token"],
                   "response", "codex: ok", round_=1)
        # the response is sitting in claude-1's inbox unhandled
        nx = s.next_action("A", "claude-1")
        self.assertEqual(nx["action"], "drain")
        self.assertEqual(nx["pending_for_you"], 1)

    def test_reclaim_takes_priority_when_orphaned(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        c = s.claim("A", "codex-1")  # codex's watcher claims, then "dies"
        s.conn.execute("UPDATE inbox SET leased_until='2000-01-01T00:00:00.000000Z' "
                       "WHERE recipient='codex-1'")
        nx = s.next_action("A", "codex-1")
        self.assertEqual(nx["action"], "reclaim")
        self.assertEqual(nx["orphaned_for_you"], [c["claim_message_id"]])

    def test_done_when_converged(self):
        s = self._setup(("codex-1",))
        s.inbox_drain("A", "codex-1")
        s.decide("A", "claude-1", "ship it")
        self.assertEqual(s.next_action("A", "claude-1")["action"], "done")

    def test_offline_reviewer_flagged_in_why(self):
        s = self._setup(("codex-1",))
        # force codex's heartbeat far into the past -> offline
        s.conn.execute("UPDATE participants SET last_heartbeat='2000-01-01T00:00:00.000000Z' "
                       "WHERE agent_id='codex-1'")
        nx = s.next_action("A", "claude-1")
        self.assertEqual(nx["action"], "wait")
        self.assertIn("offline", nx["why"])


DEAD = "2000-01-01T00:00:00.000000Z"


class TestOrchestratedPlan(Base):
    """Option B (ADR-0001): an orchestrated plan = one project with a claimable task
    queue. Interchangeable workers pull tasks (work-stealing); only trusted reviewers
    (approvers) can accept; the orchestrator converges when every task is accepted."""

    def _plan(self, workers=("w1", "w2"), approvers=("claude-1",)):
        s = self.s
        s.start("P", "plan", "ship it", "orch-1", role="orchestrator")
        for w in workers:
            s.join("P", w, role="worker")
        for a in approvers:
            s.grant_role("P", "orch-1", a, "approver")  # orchestrator grants approver
        return s

    def _post_task(self, s, body="do subtask X"):
        return s.post("P", "orch-1", "broadcast", "task", body, round_=1)["message_id"]

    # --- fanout -----------------------------------------------------------------
    def test_task_fans_out_to_workers_not_reviewers(self):
        s = self._plan()
        self._post_task(s)
        self.assertEqual(len(s.poll("P", "w1")), 1)
        self.assertEqual(len(s.poll("P", "w2")), 1)
        self.assertEqual(len(s.poll("P", "claude-1")), 0)  # approver gets no tasks

    def test_review_request_still_goes_to_approvers_not_workers(self):
        s = self._plan()
        s.post("P", "orch-1", "broadcast", "review_request", "review this", round_=1)
        self.assertEqual(len(s.poll("P", "claude-1")), 1)
        self.assertEqual(len(s.poll("P", "w1")), 0)  # workers get no reviews

    # --- work-stealing ----------------------------------------------------------
    def test_first_worker_wins_sibling_preempted(self):
        s = self._plan()
        self._post_task(s)
        c = s.claim("P", "w1")
        self.assertEqual(c["type"], "task")
        self.assertIsNone(s.claim("P", "w2"))  # w2 can't also do it
        self.assertEqual(len(s.poll("P", "w2")), 0)

    def test_dead_worker_task_restolen_by_another(self):
        s = self._plan()
        self._post_task(s)
        c1 = s.claim("P", "w1")            # w1 grabs it, then "dies"
        s.conn.execute("UPDATE inbox SET leased_until=? WHERE claimed_by='w1'", (DEAD,))
        # sweep returns it to pending AND restores w2's preempted sibling
        c2 = s.claim("P", "w2")
        self.assertIsNotNone(c2)
        self.assertEqual(c2["claim_message_id"], c1["claim_message_id"])
        self.assertEqual(c2["claimed_by"] if "claimed_by" in c2.keys() else "w2", "w2")

    def test_reclaim_force_reopens_task_to_pool(self):
        s = self._plan()
        self._post_task(s)
        s.claim("P", "w1")
        s.reclaim("P", force=True)         # orchestrator knows w1 is dead
        self.assertIsNotNone(s.claim("P", "w2"))  # w2 can now steal it

    # --- trusted-reviewer gate (FR2) --------------------------------------------
    def test_only_approver_can_accept(self):
        s = self._plan(approvers=("claude-1",))
        s.join("P", "rev-1", role="reviewer")  # a plain reviewer, not trusted to accept
        for who in ("orch-1", "w1", "rev-1"):
            with self.assertRaises(CollabError):
                s.post("P", who, "broadcast", "approval", "LGTM")
        # the trusted reviewer can
        ok = s.post("P", "claude-1", "broadcast", "approval", "LGTM")
        self.assertFalse(ok["duplicate"])

    def test_non_participant_cannot_approve(self):
        s = self._plan()
        with self.assertRaises(CollabError):
            s.post("P", "stranger-9", "broadcast", "approval", "LGTM")

    # --- roll-up + convergence --------------------------------------------------
    def _drive_to_accepted(self, s, body="do X"):
        tid = self._post_task(s, body)
        c = s.claim("P", "w1")
        # worker submits its result as a review_request to the trusted reviewer
        s.complete("P", "w1", c["claim_message_id"], c["claim_token"],
                   "review_request", "result: did X", to_agent="broadcast")
        rc = s.claim("P", "claude-1")
        s.complete("P", "claude-1", rc["claim_message_id"], rc["claim_token"],
                   "approval", "accepted")
        return tid

    def test_rollup_state_transitions(self):
        s = self._plan()
        tid = self._post_task(s)
        self.assertEqual(s._task_rollup("P")[0]["state"], "todo")
        c = s.claim("P", "w1")
        self.assertEqual(s._task_rollup("P")[0]["state"], "claimed")
        s.complete("P", "w1", c["claim_message_id"], c["claim_token"],
                   "review_request", "result", to_agent="broadcast")
        self.assertEqual(s._task_rollup("P")[0]["state"], "submitted")
        rc = s.claim("P", "claude-1")
        s.complete("P", "claude-1", rc["claim_message_id"], rc["claim_token"],
                   "approval", "ok")
        row = s._task_rollup("P")[0]
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(row["accepted_by"], "claude-1")

    def test_decide_blocked_until_all_tasks_accepted(self):
        s = self._plan()
        self._post_task(s, "task A")
        self._drive_to_accepted(s, "task B")   # one accepted, one still todo
        with self.assertRaises(CollabError):
            s.decide("P", "orch-1", "converge")
        # force overrides and records what was skipped
        out = s.decide("P", "orch-1", "converge", force=True)
        self.assertEqual(out["state"], "converged")
        self.assertIn("forced_over_unaccepted_tasks", out)

    def test_decide_allowed_when_all_accepted(self):
        s = self._plan()
        self._drive_to_accepted(s)
        out = s.decide("P", "orch-1", "converge")
        self.assertEqual(out["state"], "converged")
        self.assertEqual(out["tasks_total"], 1)

    # --- next_action for the new roles ------------------------------------------
    def test_next_worker_do_task_then_wait(self):
        s = self._plan()
        self._post_task(s)
        self.assertEqual(s.next_action("P", "w1")["action"], "do-task")
        s.claim("P", "w1")
        self.assertEqual(s.next_action("P", "w2")["action"], "wait")  # sibling preempted

    def test_next_orchestrator_lifecycle(self):
        s = self._plan()
        self.assertEqual(s.next_action("P", "orch-1")["action"], "broadcast")
        self._post_task(s)
        self.assertEqual(s.next_action("P", "orch-1")["action"], "wait")  # in progress
        self._drive_to_accepted(s, "another") if False else None
        # accept the one task
        c = s.claim("P", "w1")
        s.complete("P", "w1", c["claim_message_id"], c["claim_token"],
                   "review_request", "r", to_agent="broadcast")
        rc = s.claim("P", "claude-1")
        s.complete("P", "claude-1", rc["claim_message_id"], rc["claim_token"],
                   "approval", "ok")
        self.assertEqual(s.next_action("P", "orch-1")["action"], "decide")
        s.decide("P", "orch-1", "go")
        self.assertEqual(s.next_action("P", "orch-1")["action"], "done")


class TestAcceptPolicy(Base):
    """Configurable per-plan acceptance: who counts as the final reviewer —
    any (default) | all | final:<id>. Governs when a task is accepted and thus when
    the orchestrator can converge the plan."""

    def _plan(self, policy="any", approvers=("a1", "a2")):
        s = self.s
        s.start("P", "t", "g", "orch-1", role="orchestrator", accept_policy=policy)
        s.join("P", "w1", role="worker")
        for a in approvers:
            s.grant_role("P", "orch-1", a, "approver")  # orchestrator grants approver
        return s

    def _task(self, s):
        return s.post("P", "orch-1", "broadcast", "task", "do X", round_=1)["message_id"]

    def _submit(self, s):
        """A worker actually does the task and SUBMITS a result (claim -> complete) —
        acceptance requires a real submission (a task can't be silently acked)."""
        c = s.claim("P", "w1")
        s.complete("P", "w1", c["claim_message_id"], c["claim_token"],
                   "review_request", "result", to_agent="broadcast")

    def _approve(self, s, approver, task_id):
        s.post("P", approver, "broadcast", "approval", "ok", thread_id=task_id)

    def test_default_policy_is_any(self):
        s = self.s
        s.start("P", "t", "g", "orch-1", role="orchestrator")
        self.assertEqual(s.status("P")["accept_policy"], "any")

    def test_any_one_approver_accepts(self):
        s = self._plan("any")
        t = self._task(s)
        self._submit(s)
        self._approve(s, "a1", t)
        self.assertEqual(s._task_rollup("P")[0]["state"], "accepted")
        self.assertEqual(s.decide("P", "orch-1", "go")["state"], "converged")

    def test_all_requires_every_approver(self):
        s = self._plan("all")
        t = self._task(s)
        self._submit(s)
        self._approve(s, "a1", t)
        self.assertNotEqual(s._task_rollup("P")[0]["state"], "accepted")  # a2 missing
        with self.assertRaises(CollabError):
            s.decide("P", "orch-1", "go")
        self._approve(s, "a2", t)
        self.assertEqual(s._task_rollup("P")[0]["state"], "accepted")
        self.assertEqual(s.decide("P", "orch-1", "go")["state"], "converged")

    def test_final_requires_designated_reviewer(self):
        s = self._plan("final:a2")
        t = self._task(s)
        self._submit(s)
        self._approve(s, "a1", t)  # a non-final approver's OK is non-binding
        self.assertNotEqual(s._task_rollup("P")[0]["state"], "accepted")
        with self.assertRaises(CollabError):
            s.decide("P", "orch-1", "go")
        self._approve(s, "a2", t)  # the designated final reviewer
        row = s._task_rollup("P")[0]
        self.assertEqual(row["state"], "accepted")
        self.assertEqual(row["accepted_by"], "a2")
        self.assertEqual(s.decide("P", "orch-1", "go")["state"], "converged")

    def test_set_and_get_policy(self):
        s = self._plan("any")
        self.assertEqual(
            s.set_accept_policy("P", "orch-1", "final:a1")["accept_policy"], "final:a1")
        self.assertEqual(s.status("P")["accept_policy"], "final:a1")

    def test_invalid_policy_rejected(self):
        s = self.s
        with self.assertRaises(CollabError):
            s.start("P", "t", "g", "o", role="orchestrator", accept_policy="bogus")
        s.start("Q", "t", "g", "o", role="orchestrator")
        with self.assertRaises(CollabError):
            s.set_accept_policy("Q", "o", "final:")  # empty id


class TestCodexReviewFixes(Base):
    """v0.4.1: fixes for the issues Codex found reviewing v0.4.0 (dogfood BLOCK)."""

    def _plan(self):
        s = self.s
        s.start("P", "t", "g", "orch-1", role="orchestrator")
        s.join("P", "wA", role="worker")
        return s

    # #1 late-join backfill must not break work-stealing exclusivity
    def test_late_join_after_claim_is_preempted(self):
        s = self._plan()
        tid = s.post("P", "orch-1", "broadcast", "task", "x", round_=1)["message_id"]
        s.claim("P", "wA")                     # wA holds it
        s.join("P", "wB", role="worker")       # wB joins LATE
        self.assertIsNone(s.claim("P", "wB"))  # must NOT be able to also do it
        st = s.conn.execute("SELECT status FROM inbox WHERE message_id=? AND recipient=?",
                            (tid, "wB")).fetchone()["status"]
        self.assertEqual(st, "preempted")

    def test_late_join_after_done_cannot_redo(self):
        s = self._plan()
        tid = s.post("P", "orch-1", "broadcast", "task", "x", round_=1)["message_id"]
        c = s.claim("P", "wA")
        s.complete("P", "wA", c["claim_message_id"], c["claim_token"],
                   "review_request", "result", to_agent="broadcast")  # task submitted/done
        s.join("P", "wB", role="worker")
        self.assertIsNone(s.claim("P", "wB"))  # cannot redo completed work

    def test_task_cannot_be_acked(self):
        s = self._plan()
        s.post("P", "orch-1", "broadcast", "task", "x", round_=1)
        c = s.claim("P", "wA")
        with self.assertRaises(CollabError):   # a task needs complete/release, not ack
            s.ack("P", "wA", c["claim_message_id"], c["claim_token"])

    # round-7 fix: completing a task requires a genuine, non-empty result — not a
    # log-only/empty message that would fake a submission.
    def test_task_complete_requires_real_result(self):
        s = self._plan()
        s.post("P", "orch-1", "broadcast", "task", "x", round_=1)
        c = s.claim("P", "wA")
        cm, tok = c["claim_message_id"], c["claim_token"]
        with self.assertRaises(CollabError):                       # log-only type
            s.complete("P", "wA", cm, tok, "heartbeat", "")
        with self.assertRaises(CollabError):                       # empty result body
            s.complete("P", "wA", cm, tok, "review_request", "   ")
        # a real, non-empty result is accepted
        ok = s.complete("P", "wA", cm, tok, "review_request", "here is my work",
                        to_agent="broadcast")
        self.assertFalse(ok["duplicate"])

    def test_task_complete_rejects_reused_idempotency_key(self):
        s = self._plan()
        s.post("P", "orch-1", "broadcast", "task", "A", round_=1)
        tB = s.post("P", "orch-1", "broadcast", "task", "B", round_=1)["message_id"]
        cA = s.claim("P", "wA")                       # task A (lower seq)
        s.complete("P", "wA", cA["claim_message_id"], cA["claim_token"],
                   "review_request", "result A", to_agent="broadcast", idempotency_key="K")
        cB = s.claim("P", "wA")                        # task B
        # reusing A's key posts NO new result for B -> must be rejected
        with self.assertRaises(CollabError):
            s.complete("P", "wA", cB["claim_message_id"], cB["claim_token"],
                       "review_request", "result B", to_agent="broadcast",
                       idempotency_key="K")
        # B stays unsubmitted (not faked into 'submitted'/'accepted')
        state = {t["task"]: t["state"] for t in s._task_rollup("P")}[tB]
        self.assertIn(state, ("todo", "claimed"))

    # #2a acceptance requires a real worker submission, not just an approval
    def test_approval_without_submission_does_not_accept(self):
        s = self._plan()
        s.grant_role("P", "orch-1", "rev", "approver")
        tid = s.post("P", "orch-1", "broadcast", "task", "x", round_=1)["message_id"]
        s.post("P", "rev", "broadcast", "approval", "ok", thread_id=tid)  # premature
        self.assertEqual(s._task_rollup("P")[0]["state"], "todo")
        with self.assertRaises(CollabError):
            s.decide("P", "orch-1", "go")      # gate holds — nothing submitted

    # #2b only the initiator/orchestrator may decide
    def test_only_owner_can_decide(self):
        s = self._plan()
        with self.assertRaises(CollabError):
            s.decide("P", "wA", "go")          # a worker
        with self.assertRaises(CollabError):
            s.decide("P", "stranger", "go")    # a non-participant
        self.assertEqual(s.decide("P", "orch-1", "go")["state"], "converged")

    # #2c authority roles must be granted, not self-assigned
    def test_worker_cannot_self_assign_approver(self):
        s = self._plan()
        with self.assertRaises(CollabError):
            s.join("P", "wA", role="approver")           # self-elevation blocked
        with self.assertRaises(CollabError):
            s.grant_role("P", "wA", "wB", "approver")    # non-owner can't grant
        out = s.grant_role("P", "orch-1", "claude-1", "approver")  # owner can
        self.assertEqual(out["role"], "approver")

    # #2d demoted approver's sign-off stops counting; final:<id> must be an approver
    def test_demoted_approver_approval_stops_counting(self):
        s = self._plan()
        s.grant_role("P", "orch-1", "a1", "approver")
        tid = s.post("P", "orch-1", "broadcast", "task", "x", round_=1)["message_id"]
        c = s.claim("P", "wA")
        s.complete("P", "wA", c["claim_message_id"], c["claim_token"],
                   "review_request", "result", to_agent="broadcast")
        s.post("P", "a1", "broadcast", "approval", "ok", thread_id=tid)
        self.assertEqual(s._task_rollup("P")[0]["state"], "accepted")
        s.grant_role("P", "orch-1", "a1", "worker")      # demote a1
        self.assertNotEqual(s._task_rollup("P")[0]["state"], "accepted")

    def test_final_reviewer_must_be_approver(self):
        s = self._plan()                                  # wA is a worker
        with self.assertRaises(CollabError):
            s.set_accept_policy("P", "orch-1", "final:wA")

    # round-2 fixes: authenticate policy changes + continuous final-reviewer invariant
    def test_policy_change_requires_owner(self):
        s = self._plan()
        s.grant_role("P", "orch-1", "appr", "approver")
        for who in ("wA", "appr", "stranger"):   # worker, approver, non-participant
            with self.assertRaises(CollabError):
                s.set_accept_policy("P", who, "any")
        self.assertEqual(
            s.set_accept_policy("P", "orch-1", "all")["accept_policy"], "all")

    def test_final_reviewer_cannot_be_demoted_or_downgraded(self):
        s = self._plan()
        s.grant_role("P", "orch-1", "fin", "approver")
        s.set_accept_policy("P", "orch-1", "final:fin")
        # can't demote the final reviewer to a non-approver role...
        with self.assertRaises(CollabError):
            s.grant_role("P", "orch-1", "fin", "worker")
        # ...and an id NAMED as final reviewer but not yet joined can't slip in as a
        # non-approver (which would leave an unsatisfiable policy)
        s.set_accept_policy("P", "orch-1", "final:ghost")
        with self.assertRaises(CollabError):
            s.join("P", "ghost", role="reviewer")
        # change the policy away first, THEN reassignment is allowed
        s.set_accept_policy("P", "orch-1", "any")
        self.assertEqual(s.grant_role("P", "orch-1", "fin", "worker")["role"], "worker")

    # round-3 fix: authorization is evaluated against CURRENT role (checked in-tx), so a
    # demoted grantor/decider is rejected — no TOCTOU window on stale role.
    def test_demoted_owner_loses_grant_and_decide(self):
        s = self._plan()
        s.grant_role("P", "orch-1", "orch2", "orchestrator")   # orch-1 grants a 2nd owner
        s.grant_role("P", "orch2", "orch-1", "worker")          # orch2 demotes orch-1
        with self.assertRaises(CollabError):
            s.grant_role("P", "orch-1", "x", "approver")        # demoted -> can't grant
        with self.assertRaises(CollabError):
            s.decide("P", "orch-1", "go")                       # demoted -> can't decide
        self.assertEqual(s.decide("P", "orch2", "go")["state"], "converged")  # current owner can

    # round-5 fix: an approval posted BEFORE the worker submitted must not count — the
    # reviewer has to sign off on the submitted work, not rubber-stamp the spec.
    def test_pre_submission_approval_does_not_count(self):
        s = self._plan()
        s.grant_role("P", "orch-1", "rev", "approver")
        tid = s.post("P", "orch-1", "broadcast", "task", "x", round_=1)["message_id"]
        s.post("P", "rev", "broadcast", "approval", "pre-approve the spec", thread_id=tid)
        c = s.claim("P", "wA")
        s.complete("P", "wA", c["claim_message_id"], c["claim_token"],   # submit AFTER approval
                   "review_request", "result", to_agent="broadcast")
        # the stale pre-approval must not accept the task
        self.assertNotEqual(s._task_rollup("P")[0]["state"], "accepted")
        with self.assertRaises(CollabError):
            s.decide("P", "orch-1", "go")
        # a fresh approval AFTER the submission accepts it
        s.post("P", "rev", "broadcast", "approval", "reviewed the work", thread_id=tid)
        self.assertEqual(s._task_rollup("P")[0]["state"], "accepted")

    # round-11 fix: a role change reconciles the inbox — a promoted worker can't keep its
    # task rows (and thus can't claim+complete then approve the same task).
    def test_promotion_reconciles_inbox(self):
        s = self._plan()
        s.post("P", "orch-1", "broadcast", "task", "x", round_=1)
        self.assertEqual(len(s.poll("P", "wA")), 1)          # wA has the task
        s.grant_role("P", "orch-1", "wA", "approver")        # promote worker -> approver
        self.assertIsNone(s.claim("P", "wA"))                # can't claim its former task
        n = s.conn.execute(
            "SELECT COUNT(*) c FROM inbox i JOIN messages m USING(message_id) "
            "WHERE i.recipient='wA' AND m.type='task'").fetchone()["c"]
        self.assertEqual(n, 0)                               # task rows removed

    def test_role_change_rejected_while_holding_incompatible_claim(self):
        s = self._plan()
        s.post("P", "orch-1", "broadcast", "task", "x", round_=1)
        c = s.claim("P", "wA")                               # wA holds a task claim
        with self.assertRaises(CollabError):
            s.grant_role("P", "orch-1", "wA", "approver")    # can't promote mid-claim
        s.release("P", "wA", c["claim_message_id"], c["claim_token"])
        self.assertEqual(                                    # after release, promotion works
            s.grant_role("P", "orch-1", "wA", "approver")["role"], "approver")

    # round-12 fix: a task's own submitter can NEVER accept it, even after being promoted
    # to approver (recorded permanently as the done row's claimed_by).
    def test_submitter_cannot_approve_own_task_after_promotion(self):
        s = self._plan()
        s.grant_role("P", "orch-1", "other", "approver")     # a separate trusted reviewer
        tid = s.post("P", "orch-1", "broadcast", "task", "x", round_=1)["message_id"]
        c = s.claim("P", "wA")                               # wA does the work
        s.complete("P", "wA", c["claim_message_id"], c["claim_token"],
                   "review_request", "my result", to_agent="broadcast")
        s.grant_role("P", "orch-1", "wA", "approver")        # wA is promoted
        s.post("P", "wA", "broadcast", "approval", "self-approve", thread_id=tid)
        self.assertNotEqual(s._task_rollup("P")[0]["state"], "accepted")  # own approval ignored
        with self.assertRaises(CollabError):
            s.decide("P", "orch-1", "go")
        # a DIFFERENT approver's sign-off does accept it
        s.post("P", "other", "broadcast", "approval", "ok", thread_id=tid)
        self.assertEqual(s._task_rollup("P")[0]["state"], "accepted")

    # round-13 fix: excluding the submitter must not make `all` unsatisfiable, and
    # final:<submitter> must be rejected.
    def test_all_policy_satisfiable_after_submitter_promoted(self):
        s = self.s
        s.start("P", "t", "g", "orch-1", role="orchestrator", accept_policy="all")
        s.join("P", "wA", role="worker")
        s.grant_role("P", "orch-1", "a2", "approver")        # a second, non-submitting approver
        tid = s.post("P", "orch-1", "broadcast", "task", "x", round_=1)["message_id"]
        c = s.claim("P", "wA")
        s.complete("P", "wA", c["claim_message_id"], c["claim_token"],
                   "review_request", "result", to_agent="broadcast")
        s.grant_role("P", "orch-1", "wA", "approver")        # submitter promoted -> approver
        # `all` must require only the ELIGIBLE (non-submitter) approvers: a2 alone accepts
        s.post("P", "wA", "broadcast", "approval", "self", thread_id=tid)   # ignored
        self.assertNotEqual(s._task_rollup("P")[0]["state"], "accepted")
        s.post("P", "a2", "broadcast", "approval", "ok", thread_id=tid)
        self.assertEqual(s._task_rollup("P")[0]["state"], "accepted")

    def test_final_cannot_designate_a_submitter(self):
        s = self._plan()                                     # orch-1, wA worker
        tid = s.post("P", "orch-1", "broadcast", "task", "x", round_=1)["message_id"]
        c = s.claim("P", "wA")
        s.complete("P", "wA", c["claim_message_id"], c["claim_token"],
                   "review_request", "result", to_agent="broadcast")
        s.grant_role("P", "orch-1", "wA", "approver")        # wA is now an approver...
        with self.assertRaises(CollabError):                 # ...but it submitted this task
            s.set_accept_policy("P", "orch-1", "final:wA")

    # round-4 fix: join() reads project state INSIDE its tx, so a join racing a decide()
    # can't backfill pending work into an already-converged project.
    def test_join_after_convergence_does_not_backfill(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.post("A", "claude-1", "broadcast", "review_request", "r", round_=1)
        s.decide("A", "claude-1", "done")            # project converged
        out = s.join("A", "codex-1")                 # late join into a converged project
        self.assertEqual(out["backfilled"], 0)
        self.assertEqual(len(s.poll("A", "codex-1")), 0)  # no pending work injected

    # #4 release/nack returns the claim promptly and restores the task pool
    def test_release_returns_claim_and_is_token_fenced(self):
        s = self.s
        s.start("P", "t", "g", "claude-1")
        s.join("P", "codex-1")
        s.post("P", "claude-1", "codex-1", "review_request", "r", round_=1)
        c = s.claim("P", "codex-1")
        with self.assertRaises(CollabError):
            s.release("P", "codex-1", c["claim_message_id"], "bad-token")
        s.release("P", "codex-1", c["claim_message_id"], c["claim_token"])
        self.assertEqual(len(s.poll("P", "codex-1")), 1)  # immediately reclaimable

    def test_release_restores_task_pool(self):
        s = self._plan()
        s.join("P", "wB", role="worker")
        s.post("P", "orch-1", "broadcast", "task", "x", round_=1)
        cA = s.claim("P", "wA")
        self.assertIsNone(s.claim("P", "wB"))             # sibling preempted
        s.release("P", "wA", cA["claim_message_id"], cA["claim_token"])
        self.assertIsNotNone(s.claim("P", "wB"))          # pool reopened to wB

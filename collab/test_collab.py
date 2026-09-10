#!/usr/bin/env python3
"""Phase 1 exit-criteria tests for collab.py.

Covers: full convergence flow, redelivery/idempotency, broadcast fan-out,
claim-collision (concurrent), crash-after-post-before-ack (atomic complete),
and stale-worker fencing (claim_token).
"""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from collab import (Store, CollabError, watch, _bind_payload, _agent_payload,
                    _validate_profile_data, build_parser, main)

COLLAB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(COLLAB_DIR)
PLUGIN_BIN = os.path.join(
    REPO_ROOT, "plugins", "agent-collab", "skills", "agent-collab", "bin")
FAKE_AGENT = os.path.join(COLLAB_DIR, "fake_agent.py")
WATCH_LAUNCHER = os.path.join(PLUGIN_BIN, "collab-watch.sh")
LEGACY_SCHEMA = os.path.join(COLLAB_DIR, "fixtures", "schema-pre-orchestrated.sql")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="collab_test_")
        self.s = Store(self.tmp)

    def tearDown(self):
        self.s.close()

    def fresh_store(self):
        """A second connection to the same root (simulates another process)."""
        return Store(self.tmp)


class TestStoreMigrations(unittest.TestCase):
    """Old project databases are durable user data, so migrations are a release
    contract rather than an implementation detail."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="collab_migration_")
        self.db_path = os.path.join(self.tmp, "collab.db")

    def _install_legacy_fixture(self):
        with open(LEGACY_SCHEMA, encoding="utf-8") as fh:
            schema = fh.read()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(schema)
        finally:
            conn.close()

    def test_pre_orchestrated_database_upgrades_and_backfills(self):
        self._install_legacy_fixture()

        store = Store(self.tmp)
        try:
            project_cols = {
                r["name"] for r in store.conn.execute("PRAGMA table_info(projects)")}
            inbox_cols = {
                r["name"] for r in store.conn.execute("PRAGMA table_info(inbox)")}
            self.assertIn("accept_policy", project_cols)
            self.assertIn("done_seq", inbox_cols)
            self.assertEqual(store.get_project("legacy")["accept_policy"], "any")
            done = store.conn.execute(
                "SELECT done_seq FROM inbox WHERE message_id='legacy-message' "
                "AND recipient='codex-1'").fetchone()
            self.assertEqual(done["done_seq"], 0)
        finally:
            store.close()

    def test_two_processes_can_race_the_same_legacy_migration(self):
        self._install_legacy_fixture()
        code = (
            "import sys; "
            f"sys.path.insert(0, {COLLAB_DIR!r}); "
            "from collab import Store; "
            "s=Store(sys.argv[1]); s.close()"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, self.tmp],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        results = [p.communicate(timeout=30) + (p.returncode,) for p in procs]
        self.assertEqual(
            [r[2] for r in results], [0, 0],
            "\n".join(r[1] for r in results if r[1]))

        store = Store(self.tmp)
        try:
            self.assertIn(
                "accept_policy",
                {r["name"] for r in store.conn.execute(
                    "PRAGMA table_info(projects)")})
            self.assertIn(
                "done_seq",
                {r["name"] for r in store.conn.execute(
                    "PRAGMA table_info(inbox)")})
        finally:
            store.close()

    def test_wal_failure_falls_back_to_delete_journal(self):
        real_connect = sqlite3.connect

        class WalFails(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
                    raise sqlite3.OperationalError("WAL unavailable")
                return super().execute(sql, *args, **kwargs)

        def connect(*args, **kwargs):
            return real_connect(*args, factory=WalFails, **kwargs)

        with mock.patch("collab.sqlite3.connect", side_effect=connect):
            store = Store(self.tmp)
        try:
            mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "delete")
        finally:
            store.close()


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

    def test_direct_claude_watch_preflights_before_join_or_claim(self):
        """The direct CLI form must not burn a delivery when its keychain is hidden."""
        s = self.s
        s.start("A", "queue schema", "agree", "codex-1")
        s.post("A", "codex-1", "claude-1", "review_request", "review", round_=1)
        failed_status = subprocess.CompletedProcess(
            ["claude", "auth", "status"], 1,
            stdout=b'{"loggedIn":false}', stderr=b"")

        with mock.patch("subprocess.run", return_value=failed_status):
            with self.assertRaisesRegex(CollabError, "Claude authentication is unavailable"):
                watch(s, "A", "claude-1", ["claude", "--print"], once=True,
                      log_fh=self._devnull())

        self.assertEqual(len(s.poll("A", "claude-1")), 1)
        self.assertNotIn(
            "claude-1", {p["agent_id"] for p in s.participants("A")})

    def test_direct_claude_watch_allows_explicit_nonstandard_auth_bypass(self):
        s = self.s
        s.start("A", "queue schema", "agree", "codex-1")
        s.post("A", "codex-1", "claude-1", "review_request", "review", round_=1)

        with mock.patch.dict(os.environ, {"COLLAB_CLAUDE_AUTH_PREFLIGHT": "0"}):
            with mock.patch("subprocess.run") as auth_status:
                with mock.patch(
                        "collab._run_agent_with_heartbeat",
                        return_value=(0, "review complete", "")):
                    n = watch(s, "A", "claude-1", ["claude", "--print"],
                              once=True, log_fh=self._devnull())

        self.assertEqual(n, 1)
        auth_status.assert_not_called()
        self.assertEqual(len(s.poll("A", "claude-1")), 0)

    def test_watch_preserves_agent_stdout_without_trimming(self):
        s = self.s
        s.start("A", "exact output", "preserve it", "claude-1")
        s.post("A", "claude-1", "codex-1", "question", "return exact output",
               round_=1)
        exact = ' \n{"answer":1}\n '
        command = [
            sys.executable, "-c",
            f"import sys; sys.stdout.write({exact!r})",
        ]

        n = watch(s, "A", "codex-1", command, once=True, lease_min=10,
                  log_fh=self._devnull())

        self.assertEqual(n, 1)
        responses = [m for m in s.log("A") if m["type"] == "response"]
        self.assertEqual(responses[0]["body"], exact)

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

    def test_output_admission_accepts_without_mutating_response_and_binds_input(self):
        """A validator may admit only; even stdout that looks like a replacement is
        ignored, while its envelope binds the original claim/artifact and response."""
        s = self.s
        s.start("A", "exact output", "preserve it", "claude-1")
        artifact = s.put_artifact(
            "A", "spec.md", b"# immutable\nvalidator binding\n", "claude-1")
        s.post(
            "A", "claude-1", "codex-1", "question", "return exact output",
            round_=1, refs={"artifact": artifact["artifact"]},
            idempotency_key="source-assignment-1")
        exact = (
            ' \n{"answer":"caf\u00e9","nul":"\x00",'
            '"recipient_agent":"untrusted-worker-claim"}\r\n ')
        agent = [
            sys.executable, "-c",
            f"import sys; sys.stdout.write({exact!r})",
        ]
        capture = os.path.join(self.tmp, "admission-envelope.json")
        validator = [
            sys.executable, "-c",
            "import json,sys; "
            "d=json.load(sys.stdin); "
            "open(sys.argv[1],'w',encoding='utf-8').write(json.dumps(d)); "
            "sys.stdout.write('THIS MUST NOT REPLACE THE RESPONSE')",
            capture,
        ]

        n = watch(
            s, "A", "codex-1", agent, once=True, lease_min=10,
            output_admission_argv=validator, log_fh=self._devnull())

        self.assertEqual(n, 1)
        response = [m for m in s.log("A") if m["type"] == "response"][0]
        self.assertEqual(response["body"].encode("utf-8"), exact.encode("utf-8"))
        with open(capture, encoding="utf-8") as fh:
            envelope = json.load(fh)
        self.assertEqual(
            envelope["schema"], "collab-watcher-output-admission/1")
        self.assertEqual(envelope["response"], exact)
        assignment = envelope["assignment"]
        self.assertEqual(assignment["project"], "A")
        self.assertEqual(assignment["recipient_agent"], "codex-1")
        self.assertEqual(
            assignment["claim_message_id"], assignment["message_id"])
        self.assertEqual(
            assignment["idempotency_key"], "source-assignment-1")
        self.assertEqual(assignment["type"], "question")
        self.assertEqual(assignment["round"], 1)
        self.assertEqual(assignment["artifact_ref"], artifact["artifact"])
        self.assertEqual(
            json.loads(assignment["refs_json"]),
            {"artifact": artifact["artifact"]})
        # The opaque worker output tries to assert a different identity, but it
        # cannot affect the assignment object supplied by the watcher/broker.
        self.assertIn(
            '"recipient_agent":"untrusted-worker-claim"', exact)
        payload = json.loads(envelope["agent_payload"])
        self.assertEqual(payload["message"]["body"], "return exact output")
        self.assertEqual(payload["artifact"], {
            "ref": artifact["artifact"],
            "content": "# immutable\nvalidator binding\n",
        })


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

    def test_nonzero_agent_diagnostic_includes_bounded_stdout_and_stderr(self):
        """Claude prints auth failures to stdout. A watcher that records only stderr
        turns the actionable error into a blank five-delivery stall."""
        s = self._setup_review()
        command = [
            sys.executable, "-c",
            "import sys; "
            "sys.stdout.write('Not logged in - use host keychain'); "
            "sys.stderr.write('auth preflight failed'); "
            "sys.exit(1)",
        ]
        log = io.StringIO()

        n = watch(
            s, "A", "codex-1", command, once=True, lease_min=10,
            max_deliveries=1, log_fh=log)

        self.assertEqual(n, 0)
        self.assertIn("stdout: Not logged in", log.getvalue())
        self.assertIn("stderr: auth preflight failed", log.getvalue())
        audit = [m for m in s.log("A") if m["type"] == "status"][-1]["body"]
        self.assertIn("stdout: Not logged in", audit)
        self.assertIn("stderr: auth preflight failed", audit)

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

    def test_output_admission_rejection_redelivers_then_stalls_with_diagnostics(self):
        s = self._setup_review()
        agent = [
            sys.executable, "-c",
            "import sys; sys.stdout.write('opaque response')",
        ]
        validator = [
            sys.executable, "-c",
            "import sys; sys.stderr.write('schema mismatch at $.result'); sys.exit(7)",
        ]
        log = io.StringIO()

        n = watch(
            s, "A", "codex-1", agent, once=True, lease_min=10,
            max_deliveries=2, output_admission_argv=validator, log_fh=log)

        self.assertEqual(n, 0)
        self.assertEqual([m for m in s.log("A") if m["type"] == "response"], [])
        row = s.conn.execute(
            "SELECT status, deliveries FROM inbox WHERE recipient='codex-1'"
        ).fetchone()
        self.assertEqual(dict(row), {"status": "pending", "deliveries": 1})
        self.assertIn("output admission rejected", log.getvalue())
        self.assertIn("schema mismatch", log.getvalue())

        watch(
            s, "A", "codex-1", agent, once=True, lease_min=10,
            max_deliveries=2, output_admission_argv=validator, log_fh=log)

        row = s.conn.execute(
            "SELECT status, deliveries FROM inbox WHERE recipient='codex-1'"
        ).fetchone()
        self.assertEqual(dict(row), {"status": "stalled", "deliveries": 2})
        self.assertEqual([m for m in s.log("A") if m["type"] == "response"], [])
        audit = [m for m in s.log("A") if m["type"] == "status"][-1]["body"]
        self.assertIn("output admission rejected", audit)
        self.assertIn("schema mismatch at $.result", audit)

    def test_output_admission_timeout_and_exec_error_fail_closed(self):
        for validator, expected in (
            ([sys.executable, "-c", "import time; time.sleep(2)"],
             "validator exceeded"),
            ([os.path.join(self.tmp, "missing-validator")],
             "could not execute validator"),
        ):
            with self.subTest(expected=expected):
                s = self.fresh_store()
                project = f"P-{expected.split()[0]}"
                s.start(project, "t", "g", "claude-1")
                s.post(
                    project, "claude-1", "codex-1", "question", "q", round_=1)
                log = io.StringIO()
                try:
                    n = watch(
                        s, project, "codex-1",
                        [sys.executable, "-c",
                         "import sys; sys.stdout.write('opaque response')"],
                        once=True, lease_min=10,
                        output_admission_argv=validator,
                        output_admission_timeout=0.05,
                        log_fh=log)
                    self.assertEqual(n, 0)
                    self.assertEqual(
                        [m for m in s.log(project) if m["type"] == "response"], [])
                    self.assertEqual(len(s.poll(project, "codex-1")), 1)
                    self.assertIn(expected, log.getvalue())
                finally:
                    s.close()

    def test_output_admission_is_not_run_for_empty_agent_output(self):
        s = self._setup_review()
        marker = os.path.join(self.tmp, "validator-ran")
        validator = [
            sys.executable, "-c",
            "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
            marker,
        ]

        n = watch(
            s, "A", "codex-1",
            [sys.executable, "-c", "pass"],
            once=True, lease_min=10,
            output_admission_argv=validator,
            log_fh=self._devnull())

        self.assertEqual(n, 0)
        self.assertFalse(os.path.exists(marker))
        self.assertEqual([m for m in s.log("A") if m["type"] == "response"], [])
        self.assertEqual(len(s.poll("A", "codex-1")), 1)

    def test_malformed_output_admission_config_fails_before_claim(self):
        import subprocess

        s = self._setup_review()
        bin_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "collab.py")
        bad_values = ("not-json", "{}", "[]", '[""]', "[1]")
        for bad in bad_values:
            with self.subTest(bad=bad):
                result = subprocess.run(
                    [
                        sys.executable, bin_path, "--root", self.tmp,
                        "watch", "--project", "A", "--agent", "codex-1",
                        "--output-admission-argv", bad,
                        "--once", "--exec", "/bin/cat",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("output-admission argv", result.stderr)
        for bad_timeout in ("0", "301", "forever"):
            with self.subTest(timeout=bad_timeout):
                result = subprocess.run(
                    [
                        sys.executable, bin_path, "--root", self.tmp,
                        "watch", "--project", "A", "--agent", "codex-1",
                        "--output-admission-argv", '["/usr/bin/true"]',
                        "--output-admission-timeout", bad_timeout,
                        "--once", "--exec", "/bin/cat",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("output-admission timeout", result.stderr)
        result = subprocess.run(
            [
                sys.executable, bin_path, "--root", self.tmp,
                "watch", "--project", "A", "--agent", "codex-1",
                "--output-admission-timeout", "20",
                "--once", "--exec", "/bin/cat",
            ],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "--output-admission-timeout requires --output-admission-argv",
            result.stderr)
        # Parser/config errors occur before Store.claim; the queued delivery is intact.
        row = s.conn.execute(
            "SELECT status, deliveries FROM inbox WHERE recipient='codex-1'"
        ).fetchone()
        self.assertEqual(dict(row), {"status": "pending", "deliveries": 0})

        # A JSON argv containing spaces survives argparse REMAINDER and coexists
        # safely with a separate --exec argv.
        validator_json = json.dumps([
            sys.executable, "-c",
            "import json,sys; "
            "d=json.load(sys.stdin); "
            "assert d['assignment']['recipient_agent']=='codex-1'; "
            "assert d['response']",
        ])
        result = subprocess.run(
            [
                sys.executable, bin_path, "--root", self.tmp,
                "watch", "--project", "A", "--agent", "codex-1",
                "--output-admission-argv", validator_json,
                "--once", "--exec", "/bin/cat",
            ],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"processed": 1})
        self.assertEqual(
            len([m for m in s.log("A") if m["type"] == "response"]), 1)


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
        root = REPO_ROOT
        claude = os.path.join(root, "plugins/agent-collab/.claude-plugin/plugin.json")
        codex = os.path.join(root, "plugins/agent-collab/.codex-plugin/plugin.json")
        market = os.path.join(root, ".claude-plugin/marketplace.json")
        if not (os.path.exists(claude) and os.path.exists(codex)
                and os.path.exists(market)):
            self.skipTest("manifests not present (running outside the repo tree)")
        with open(claude, encoding="utf-8") as fh:
            cv = json.load(fh)["version"]
        with open(codex, encoding="utf-8") as fh:
            xv = json.load(fh)["version"]
        with open(market, encoding="utf-8") as fh:
            mv = json.load(fh)["plugins"][0]["version"]
        self.assertEqual(
            {cv, xv, mv}, {cv},
            f"manifest version drift: claude={cv} codex={xv} marketplace={mv}")

    def test_bundled_cli_copies_match_canonical(self):
        root = REPO_ROOT
        canonical = os.path.join(root, "collab", "collab.py")
        bundled = (
            os.path.join(
                root,
                "plugins/agent-collab/skills/agent-collab/bin/collab.py"),
            os.path.join(root, "demo", "bin", "collab.py"),
        )
        if not all(os.path.exists(path) for path in (canonical, *bundled)):
            self.skipTest("bundled CLI copies not present")
        with open(canonical, "rb") as fh:
            expected = fh.read()
        for path in bundled:
            with self.subTest(path=path), open(path, "rb") as fh:
                self.assertEqual(
                    fh.read(), expected,
                    f"bundled collab.py drifted from {canonical}")

    def test_release_guard_checks_versions_and_packaged_content(self):
        import importlib.util

        path = os.path.join(REPO_ROOT, "check_version.py")
        if not os.path.exists(path):
            self.skipTest("release guard not present")
        spec = importlib.util.spec_from_file_location("agent_collab_check_version", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        versions = module.collect()
        self.assertEqual(len(set(versions.values())), 1, versions)
        self.assertEqual(module.content_drift(), [])


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


class TestAgentPayloadModes(Base):
    """Watcher instructions follow the message contract instead of forcing every
    actionable item through adversarial-review framing."""

    def test_structured_output_task_gets_execution_instructions(self):
        s = self.s
        s.start("P", "structured work", "produce a result", "orch-1",
                role="orchestrator")
        s.join("P", "worker-1", role="worker")
        s.put_artifact("P", "input.json", b'{"value": 7}\n', "orch-1")
        body = (
            'Return exactly one JSON object matching {"answer": <integer>}. '
            "Do not include prose or Markdown fences.")
        s.post("P", "orch-1", "broadcast", "task", body, round_=1,
               refs={"artifact": "input.json@v1"})

        claimed = s.claim("P", "worker-1")
        payload = json.loads(_agent_payload(s, "P", "worker-1", claimed))

        self.assertEqual(payload["message"]["type"], "task")
        self.assertEqual(payload["message"]["body"], body)
        self.assertEqual(payload["artifact"]["content"], '{"value": 7}\n')
        self.assertIn("Execute the task", payload["instructions"])
        self.assertIn("output contract as authoritative", payload["instructions"])
        self.assertNotIn("strongest substantive objection", payload["instructions"])
        self.assertNotIn("your review to stdout", payload["instructions"])

    def test_review_request_keeps_adversarial_review_discipline(self):
        s = self.s
        s.start("R", "design", "review it", "owner-1")
        s.join("R", "reviewer-1")
        s.post("R", "owner-1", "reviewer-1", "review_request",
               "Review the proposed design.", round_=1)

        claimed = s.claim("R", "reviewer-1")
        payload = json.loads(_agent_payload(s, "R", "reviewer-1", claimed))

        self.assertIn("strongest substantive objection", payload["instructions"])
        self.assertIn("reviewing the GOAL", payload["instructions"])
        self.assertNotIn("Execute the task", payload["instructions"])

    def test_question_gets_direct_answer_instructions(self):
        s = self.s
        s.start("Q", "question", "answer it", "owner-1")
        s.join("Q", "reviewer-1")
        s.post("Q", "owner-1", "reviewer-1", "question",
               "Answer with exactly yes or no.", round_=1)

        claimed = s.claim("Q", "reviewer-1")
        payload = json.loads(_agent_payload(s, "Q", "reviewer-1", claimed))

        self.assertIn("Answer the question directly", payload["instructions"])
        self.assertIn("requested output format as authoritative",
                      payload["instructions"])
        self.assertNotIn("strongest substantive objection", payload["instructions"])


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

    def _stalled(self):
        s, claimed = self._claimed()
        s.mark_stalled(
            "A", claimed["claim_message_id"], "codex-1",
            claimed["claim_token"])
        return s, claimed

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

    def test_retry_stalled_requeues_exact_row_and_resets_delivery_budget(self):
        s, stalled = self._stalled()
        s.conn.execute(
            "UPDATE inbox SET deliveries=5 WHERE message_id=? AND recipient=?",
            (stalled["claim_message_id"], "codex-1"))

        result = s.retry_stalled(
            "A", stalled["claim_message_id"], "codex-1")

        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["message_id"], stalled["claim_message_id"])
        row = s.conn.execute(
            "SELECT status, claimed_by, claim_token, leased_until, deliveries "
            "FROM inbox WHERE message_id=? AND recipient=?",
            (stalled["claim_message_id"], "codex-1")).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertIsNone(row["claimed_by"])
        self.assertIsNone(row["claim_token"])
        self.assertIsNone(row["leased_until"])
        self.assertEqual(row["deliveries"], 0)
        self.assertEqual(s.status("A")["stalled"], [])
        self.assertIsNotNone(s.claim("A", "codex-1"))

    def test_retry_stalled_requires_an_exact_stalled_row(self):
        s, stalled = self._stalled()
        with self.assertRaises(CollabError):
            s.retry_stalled("A", "missing", "codex-1")
        with self.assertRaises(CollabError):
            s.retry_stalled("A", stalled["claim_message_id"], "copilot-1")

    def test_doctor_surfaces_stalled_retry_command(self):
        s, stalled = self._stalled()
        doc = s.doctor("A", "codex-1")
        self.assertEqual(
            doc["stalled_for_you"], [stalled["claim_message_id"]])
        self.assertTrue(any(
            "retry --project A" in hint and "--agent codex-1" in hint
            for hint in doc["hints"]))


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

    def test_retry_takes_priority_when_work_is_stalled(self):
        s = self.s
        s.start("A", "t", "g", "claude-1")
        s.join("A", "codex-1")
        s.post("A", "claude-1", "codex-1", "review_request", "rev", round_=1)
        claimed = s.claim("A", "codex-1")
        s.mark_stalled(
            "A", claimed["claim_message_id"], "codex-1",
            claimed["claim_token"])

        nx = s.next_action("A", "codex-1")

        self.assertEqual(nx["action"], "retry")
        self.assertEqual(nx["stalled_for_you"], [claimed["claim_message_id"]])
        self.assertIn("retry --project A", nx["why"])

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

    def test_retry_stalled_task_reopens_it_to_the_worker_pool(self):
        s = self._plan()
        tid = self._post_task(s)
        claimed = s.claim("P", "w1")
        self.assertIsNone(s.claim("P", "w2"))
        s.mark_stalled("P", tid, "w1", claimed["claim_token"])

        s.retry_stalled("P", tid, "w1")

        stolen = s.claim("P", "w2")
        self.assertIsNotNone(stolen)
        self.assertEqual(stolen["claim_message_id"], tid)

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



def _prof(mode="review", **kw):
    """A VALID minimal profile for `mode`; kw overrides/extends (may make it invalid on
    purpose for negative tests)."""
    d = {"schema_version": 1, "mode": mode}
    if mode == "review":
        d["reviewers"] = ["codex-1"]
    elif mode == "orchestrated":
        d["workers"] = ["codex-1"]
        d["approvers"] = ["claude-1"]
    d.update(kw)
    return json.dumps(d)


class TestArtifactPutIdentity(Base):
    """`artifact put --by` was the one actor flag that ignored $COLLAB_AGENT.

    Every other identity flag (--agent on start/join/claim/..., --from on
    post/complete/decide, --by on grant/policy) defaults to the env var, so a
    session that exports COLLAB_AGENT once can omit it everywhere -- except
    here, where argparse hard-failed with 'the following arguments are
    required: --by'.
    """

    def _cli(self, *args, env_agent=None):
        import contextlib
        buf = io.StringIO()
        old = os.environ.get("COLLAB_AGENT")
        if env_agent is None:
            os.environ.pop("COLLAB_AGENT", None)
        else:
            os.environ["COLLAB_AGENT"] = env_agent
        try:
            with contextlib.redirect_stdout(buf):
                rc = main(["--root", self.tmp, *args])
        finally:
            if old is None:
                os.environ.pop("COLLAB_AGENT", None)
            else:
                os.environ["COLLAB_AGENT"] = old
        return rc, buf.getvalue()

    def _artifact_file(self):
        path = os.path.join(self.tmp, "work.txt")
        with open(path, "w") as fh:
            fh.write("payload")
        return path

    def test_by_defaults_to_collab_agent(self):
        self.s.start("p", "t", "g", "claude-1")
        rc, out = self._cli("artifact", "put", "--project", "p",
                            "--name", "work.txt", "--file", self._artifact_file(),
                            env_agent="claude-1")
        self.assertEqual(rc, 0)
        self.assertIn("work.txt@v1", out)
        self.assertEqual(self.s.get_artifact("p", "work.txt")[0]["created_by"],
                         "claude-1")

    def test_explicit_by_still_wins_over_env(self):
        self.s.start("p", "t", "g", "claude-1")
        rc, _ = self._cli("artifact", "put", "--project", "p", "--by", "codex-1",
                          "--name", "work.txt", "--file", self._artifact_file(),
                          env_agent="claude-1")
        self.assertEqual(rc, 0)
        self.assertEqual(self.s.get_artifact("p", "work.txt")[0]["created_by"],
                         "codex-1")

    def test_missing_identity_is_a_clean_error_not_an_argparse_crash(self):
        self.s.start("p", "t", "g", "claude-1")
        import contextlib
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc, _ = self._cli("artifact", "put", "--project", "p",
                              "--name", "work.txt", "--file", self._artifact_file())
        self.assertEqual(rc, 1)
        self.assertIn("no author identity", json.loads(err.getvalue())["error"])


class TestDoctorWithoutProject(Base):
    """`doctor` is the standard opening move, but --project was required=True -- so it
    could not run before any project existed, contradicting its own docstring ("must run
    even when identity is missing, since diagnosing a missing/duplicate COLLAB_AGENT is
    one of its jobs")."""

    def _cli(self, *args, env_agent=None):
        import contextlib
        buf = io.StringIO()
        old = os.environ.get("COLLAB_AGENT")
        if env_agent is None:
            os.environ.pop("COLLAB_AGENT", None)
        else:
            os.environ["COLLAB_AGENT"] = env_agent
        try:
            with contextlib.redirect_stdout(buf):
                rc = main(["--root", self.tmp, *args])
        finally:
            if old is None:
                os.environ.pop("COLLAB_AGENT", None)
            else:
                os.environ["COLLAB_AGENT"] = old
        return rc, buf.getvalue()

    def test_cli_doctor_runs_with_no_project_at_all(self):
        rc, out = self._cli("doctor", env_agent="claude-1")
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertIsNone(d["project"])
        self.assertEqual(d["projects"], [])
        self.assertTrue(any("no projects exist" in h.lower() for h in d["hints"]))

    def test_no_project_lists_existing_projects(self):
        self.s.start("A", "t", "g", "claude-1")
        d = self.s.doctor(None, "claude-1")
        self.assertEqual([p["project"] for p in d["projects"]], ["A"])
        self.assertTrue(any("--project" in h for h in d["hints"]))

    def test_no_project_still_flags_missing_identity(self):
        d = self.s.doctor(None, None)
        self.assertTrue(any("COLLAB_AGENT" in h for h in d["hints"]))

    def test_project_scoped_doctor_is_unchanged(self):
        self.s.start("A", "t", "g", "claude-1")
        d = self.s.doctor("A", "claude-1")
        self.assertTrue(d["project_exists"])
        self.assertEqual(d["your_role"], "initiator")


class TestRoundBudgetIsReal(Base):
    """max_rounds was stored and reported as `round_budget` but read by nothing, so the
    design's "infinite disagreement loops are bounded by max_rounds" was never true --
    `next` would say 'wait' forever."""

    def _open_round(self, project, round_):
        """Initiator broadcasts a review_request at `round_`; reviewer stays silent."""
        self.s.post(project, "claude-1", "broadcast", "review_request",
                    f"round {round_}", round_=round_)

    def test_status_reports_position_against_the_budget(self):
        self.s.start("A", "t", "g", "claude-1", max_rounds=3)
        self.s.join("A", "codex-1", "reviewer")
        self._open_round("A", 2)
        st = self.s.status("A")
        self.assertEqual(st["round_budget"], 3)
        self.assertEqual(st["current_round"], 2)
        self.assertFalse(st["rounds_exhausted"])

    def test_next_says_wait_while_budget_remains(self):
        self.s.start("A", "t", "g", "claude-1", max_rounds=6)
        self.s.join("A", "codex-1", "reviewer")
        self._open_round("A", 1)
        self.assertEqual(self.s.next_action("A", "claude-1")["action"], "wait")

    def test_next_escalates_once_budget_is_spent(self):
        self.s.start("A", "t", "g", "claude-1", max_rounds=2)
        self.s.join("A", "codex-1", "reviewer")
        self._open_round("A", 2)
        n = self.s.next_action("A", "claude-1")
        self.assertTrue(n["rounds_exhausted"])
        self.assertEqual(n["action"], "escalate")
        self.assertIn("do not keep looping", n["why"])

    def test_exhaustion_never_blocks_posting(self):
        # Surfacing the budget must not strand a live project mid-round.
        self.s.start("A", "t", "g", "claude-1", max_rounds=1)
        self.s.join("A", "codex-1", "reviewer")
        self._open_round("A", 1)
        self.assertTrue(self.s.status("A")["rounds_exhausted"])
        self.s.post("A", "codex-1", "broadcast", "response", "still fine", round_=2)
        self.assertEqual(self.s.status("A")["current_round"], 2)


class TestProfiles(Base):
    """v0.4.2: global named setup profiles (validated JSON objects) so a bare
    `agent-collab` can offer use-last / pick-from-list."""

    # --- Store-level ------------------------------------------------------------
    def test_save_get_roundtrip(self):
        s = self.s
        s.save_profile("team", _prof("orchestrated", accept_policy="final:claude-1",
                                     workers=["copilot-1"], approvers=["claude-1"],
                                     models={"copilot-1": "gpt-5.6-terra"},
                                     efforts={"copilot-1": "xhigh"}))
        p = s.get_profile("team")
        self.assertEqual(p["data"]["mode"], "orchestrated")
        self.assertEqual(p["data"]["accept_policy"], "final:claude-1")
        self.assertEqual(p["data"]["models"]["copilot-1"], "gpt-5.6-terra")
        self.assertEqual(p["data"]["efforts"]["copilot-1"], "xhigh")

    def test_save_overwrites(self):
        s = self.s
        s.save_profile("t", _prof(focus="a"))
        s.save_profile("t", _prof(focus="b"))
        self.assertEqual(s.get_profile("t")["data"]["focus"], "b")
        self.assertEqual(s.list_profiles()["count"], 1)

    def test_list_most_recently_used_first(self):
        s = self.s
        s.save_profile("a", _prof())
        s.save_profile("b", _prof())
        self.assertEqual([p["name"] for p in s.list_profiles()["profiles"]], ["b", "a"])
        s.get_profile("a", use=True)
        self.assertEqual([p["name"] for p in s.list_profiles()["profiles"]], ["a", "b"])

    def test_use_returns_fresh_last_used(self):
        s = self.s
        s.save_profile("p", _prof())
        before = s.get_profile("p")["last_used_at"]
        got = s.get_profile("p", use=True)
        # the returned last_used_at is the value just written, not the stale pre-update one
        self.assertEqual(got["last_used_at"], s.get_profile("p")["last_used_at"])
        self.assertGreaterEqual(got["last_used_at"], before)

    def test_name_is_normalized(self):
        s = self.s
        s.save_profile("  team  ", _prof())
        self.assertEqual(s.get_profile("team")["name"], "team")  # stored trimmed
        self.assertEqual(s.list_profiles()["count"], 1)

    def test_delete(self):
        s = self.s
        s.save_profile("gone", _prof())
        self.assertEqual(s.delete_profile("gone")["deleted_profile"], "gone")
        self.assertEqual(s.list_profiles()["count"], 0)

    def test_get_and_delete_missing(self):
        s = self.s
        with self.assertRaises(CollabError):
            s.get_profile("nope")
        with self.assertRaises(CollabError):
            s.delete_profile("nope")

    def test_profiles_global_across_connections(self):
        self.s.save_profile("shared", _prof(focus="x"))
        self.assertEqual(self.fresh_store().get_profile("shared")["data"]["focus"], "x")

    def test_existing_db_gains_profiles_table(self):
        # opening a second Store on the same root initializes the profiles table
        cols = [r["name"] for r in self.fresh_store().conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("profiles", cols)

    # --- schema validation ------------------------------------------------------
    def test_invalid_json_rejected(self):
        with self.assertRaises(CollabError):
            self.s.save_profile("bad", "not json")

    def test_non_object_rejected(self):
        for bad in ('[]', '42', '"x"', 'null'):
            with self.assertRaises(CollabError):
                self.s.save_profile("b", bad)

    def test_schema_version_required(self):
        with self.assertRaises(CollabError):
            self.s.save_profile("p", json.dumps({"mode": "review"}))

    def test_schema_version_must_be_int_not_bool(self):
        # True == 1 in Python, so a JSON boolean must be rejected as schema_version
        for sv in (True, 1.0, "1"):
            with self.assertRaises(CollabError):
                self.s.save_profile("p", json.dumps(
                    {"schema_version": sv, "mode": "review", "reviewers": ["codex-1"]}))

    def test_bad_mode_rejected(self):
        with self.assertRaises(CollabError):
            self.s.save_profile("p", json.dumps({"schema_version": 1, "mode": "nope"}))

    def test_unknown_key_rejected(self):
        with self.assertRaises(CollabError):
            self.s.save_profile("p", _prof(bogus="x"))

    def test_forbidden_keys_rejected(self):
        # paths / tasks / secrets / commands must never be stored, even nested
        for bad in (_prof(tasks=["a"]), _prof(models={"token": "sk-123"}),
                    _prof(path="/etc/x"), _prof(roles={"exec": "rm -rf"})):
            with self.assertRaises(CollabError):
                self.s.save_profile("p", bad)

    def test_empty_name_rejected(self):
        with self.assertRaises(CollabError):
            self.s.save_profile("   ", _prof())

    def test_bad_onboarding_and_policy_rejected(self):
        with self.assertRaises(CollabError):
            self.s.save_profile("p", _prof(onboarding="teleport"))
        with self.assertRaises(CollabError):
            self.s.save_profile("p", _prof("orchestrated", accept_policy="bogus"))

    # structural / type / enum validation (round-2 fixes)
    def test_sparse_profile_missing_participants_rejected(self):
        with self.assertRaises(CollabError):              # review, no reviewers
            self.s.save_profile("p", json.dumps({"schema_version": 1, "mode": "review"}))
        with self.assertRaises(CollabError):              # orchestrated, no workers/approvers
            self.s.save_profile("p", json.dumps(
                {"schema_version": 1, "mode": "orchestrated"}))

    def test_scalar_participant_lists_rejected(self):
        with self.assertRaises(CollabError):
            self.s.save_profile("p", _prof(reviewers="codex-1"))     # not a list
        with self.assertRaises(CollabError):
            self.s.save_profile("p", _prof(reviewers=[]))            # empty
        with self.assertRaises(CollabError):
            self.s.save_profile("p", _prof(reviewers=[123]))         # not id strings

    def test_role_access_and_effort_enums_and_maps_validated(self):
        with self.assertRaises(CollabError):                          # bad role value
            self.s.save_profile("p", _prof(roles={"codex-1": "boss"}))
        with self.assertRaises(CollabError):                          # bad access value
            self.s.save_profile("p", _prof(access={"codex-1": "sudo"}))
        with self.assertRaises(CollabError):                          # bad effort value
            self.s.save_profile("p", _prof(efforts={"codex-1": "extreme"}))
        with self.assertRaises(CollabError):                          # models not a map
            self.s.save_profile("p", _prof(models=["gpt"]))
        # valid enums/maps for declared participants pass
        self.s.save_profile("ok", _prof(roles={"codex-1": "approver"},
                                        access={"codex-1": "readonly"},
                                        models={"codex-1": "o1"},
                                        efforts={"codex-1": "high"}))

    def test_non_participant_reference_rejected(self):
        with self.assertRaises(CollabError):     # models references an undeclared id
            self.s.save_profile("p", _prof(models={"stranger-9": "gpt"}))
        with self.assertRaises(CollabError):     # access references an undeclared id
            self.s.save_profile("p", _prof(access={"stranger-9": "edit"}))
        with self.assertRaises(CollabError):     # efforts references an undeclared id
            self.s.save_profile("p", _prof(efforts={"stranger-9": "high"}))

    def test_duplicate_and_noncanonical_ids_rejected(self):
        with self.assertRaises(CollabError):                       # duplicate
            self.s.save_profile("p", _prof(reviewers=["codex-1", "codex-1"]))
        with self.assertRaises(CollabError):                       # whitespace / non-canonical
            self.s.save_profile("p", _prof(reviewers=[" codex-1 "]))

    def test_worker_approver_overlap_rejected(self):
        with self.assertRaises(CollabError):
            self.s.save_profile("p", _prof("orchestrated", workers=["codex-1"],
                                           approvers=["codex-1"]))
        # distinct sets are fine
        self.s.save_profile("ok", _prof("orchestrated", workers=["codex-1"],
                                        approvers=["claude-1"]))

    def test_stray_role_id_rejected(self):
        # a roles key must be a DECLARED participant, not self-declare one
        with self.assertRaises(CollabError):
            self.s.save_profile("p", _prof(reviewers=["codex-1"],
                                           roles={"intruder": "approver"}))

    def test_unhashable_enum_values_rejected(self):
        # unhashable values ([] / {}) in enum maps/fields must raise CollabError, not
        # TypeError from `val in <set>`
        for bad in (_prof(roles={"codex-1": []}), _prof(access={"codex-1": {}}),
                    _prof(efforts={"codex-1": []}), _prof(mode=[]),
                    _prof(onboarding={})):
            with self.assertRaises(CollabError):
                self.s.save_profile("p", bad)

    def test_non_string_accept_policy_rejected(self):
        # numeric/null/array/object policy must raise CollabError, not Type/AttributeError
        for pol in (7, None, [1], {"a": 1}):
            with self.assertRaises(CollabError):
                self.s.save_profile("p", json.dumps(
                    {"schema_version": 1, "mode": "orchestrated", "workers": ["w1"],
                     "approvers": ["a1"], "accept_policy": pol}))

    def test_final_policy_must_name_a_profile_approver(self):
        with self.assertRaises(CollabError):     # final:<id> not in approvers
            self.s.save_profile("p", _prof("orchestrated", workers=["w1"],
                                           approvers=["claude-1"],
                                           accept_policy="final:ghost"))
        # final naming an actual approver passes
        self.s.save_profile("ok", _prof("orchestrated", workers=["w1"],
                                        approvers=["claude-1"],
                                        accept_policy="final:claude-1"))

    # --- CLI-level --------------------------------------------------------------
    def _cli(self, *args, stdin=None):
        import io
        import contextlib
        buf = io.StringIO()
        old = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(buf):
                rc = main(["--root", self.tmp, *args])
        finally:
            sys.stdin = old
        return rc, buf.getvalue()

    def test_cli_inline_file_stdin(self):
        import os
        rc, _ = self._cli("profile", "save", "--name", "inl", "--data", _prof())
        self.assertEqual(rc, 0)
        path = os.path.join(self.tmp, "p.json")
        with open(path, "w") as fh:
            fh.write(_prof(focus="file"))
        rc, _ = self._cli("profile", "save", "--name", "fil", "--data-file", path)
        self.assertEqual(rc, 0)
        rc, _ = self._cli("profile", "save", "--name", "std", "--data-file", "-",
                          stdin=_prof(focus="stdin"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.s.get_profile("std")["data"]["focus"], "stdin")

    def test_cli_conflicting_inputs_rejected(self):
        # --data and --data-file are mutually exclusive (argparse errors -> SystemExit)
        with self.assertRaises(SystemExit):
            self._cli("profile", "save", "--name", "x", "--data", _prof(),
                      "--data-file", "-")

    def test_cli_show_use_and_delete_guard(self):
        self._cli("profile", "save", "--name", "p", "--data", _prof())
        rc, out = self._cli("profile", "show", "--name", "p", "--use")
        self.assertEqual(rc, 0)
        self.assertIn('"data"', out)
        # delete without --yes fails; with --yes succeeds
        rc, _ = self._cli("profile", "delete", "--name", "p")
        self.assertEqual(rc, 1)
        rc, _ = self._cli("profile", "delete", "--name", "p", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual(self.s.list_profiles()["count"], 0)


class TestDocumentedCLIContract(unittest.TestCase):
    """SKILL.md tells agents not to probe --help, so its fenced signatures are part
    of the executable contract and must stay aligned with argparse."""

    @staticmethod
    def _all_options(parser):
        import argparse

        options = set()
        for action in parser._actions:
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    options.update(TestDocumentedCLIContract._all_options(child))
        return options

    def test_fenced_reference_mentions_only_real_verbs_and_flags(self):
        import argparse
        import re

        skill = os.path.join(
            REPO_ROOT, "plugins", "agent-collab", "skills", "agent-collab",
            "SKILL.md")
        with open(skill, encoding="utf-8") as fh:
            text = fh.read()
        match = re.search(
            r"## Command reference .*?\n.*?```\n(.*?)\n```", text, re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md command reference fence not found")
        block = match.group(1)

        parser = build_parser()
        subparsers = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction))
        documented_verbs = {
            line.split()[0]
            for line in block.splitlines()
            if line and not line[0].isspace() and not line.startswith("#")
        }
        self.assertEqual(
            documented_verbs - set(subparsers.choices), set(),
            "SKILL.md documents a verb argparse does not provide")
        documented_flags = set(re.findall(r"(?<![\w])--[a-z][a-z-]*", block))
        self.assertEqual(
            documented_flags - self._all_options(parser), set(),
            "SKILL.md documents a flag argparse does not provide")

    def test_documented_signature_examples_parse(self):
        examples = (
            ["review", "--project", "P", "--file", "work.md", "--focus", "tests"],
            ["start", "--project", "P", "--max-rounds", "6"],
            ["artifact", "put", "--project", "P", "--name", "work.md",
             "--file", "work.md", "--by", "codex-1"],
            ["artifact", "get", "--project", "P", "--name", "work.md",
             "--version", "1", "--out", "copy.md"],
            ["post", "--project", "P", "--type", "review_request", "--round", "1",
             "--artifact", "work.md@v1", "--body", "review"],
            ["join", "--project", "P", "--role", "reviewer"],
            ["projects"],
            ["status", "--project", "P"],
            ["next", "--project", "P", "--agent", "codex-1"],
            ["doctor", "--project", "P"],
            ["poll", "--project", "P", "--agent", "codex-1"],
            ["claim", "--project", "P", "--wait", "1", "--poll-interval", "0.1"],
            ["complete", "--project", "P", "--claim-message", "M",
             "--claim-token", "T", "--type", "response", "--body", "done"],
            ["ack", "--project", "P", "--message", "M", "--claim-token", "T"],
            ["release", "--project", "P", "--message", "M", "--claim-token", "T"],
            ["grant", "--project", "P", "--by", "owner", "--agent", "reviewer",
             "--role", "approver"],
            ["extend", "--project", "P", "--message", "M", "--claim-token", "T",
             "--lease-min", "10"],
            ["decide", "--project", "P", "--thread", "T", "--body", "done"],
            ["log", "--project", "P", "--since", "1", "--follow"],
            ["delete", "--project", "P", "--yes"],
            ["watch", "--project", "P", "--agent", "codex-1",
             "--exec", "/bin/cat"],
            ["reclaim", "--project", "P", "--agent", "codex-1", "--force"],
            ["retry", "--project", "P", "--message", "M", "--agent", "codex-1"],
            ["policy", "--project", "P", "--set", "all"],
            ["profile", "save", "--name", "team", "--data", "{}"],
            ["profile", "list"],
            ["profile", "show", "--name", "team", "--use"],
            ["profile", "delete", "--name", "team", "--yes"],
        )
        parser = build_parser()
        for argv in examples:
            with self.subTest(argv=argv):
                parser.parse_args(argv)


class TestCollabWatchLauncher(unittest.TestCase):
    """Contract tests for the shell launcher: root selection, aliases, Claude auth
    preflight, and the exact argv passed to the watcher."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="collab_launcher_")
        self.repo = os.path.join(self.tmp, "repo")
        self.fake_bin = os.path.join(self.tmp, "bin")
        self.capture = os.path.join(self.tmp, "capture.txt")
        os.makedirs(self.repo)
        os.makedirs(self.fake_bin)
        self._write_executable(
            "python3",
            """#!/bin/sh
{
  printf 'ROOT=%s\n' "$COLLAB_ROOT"
  printf 'PWD=%s\n' "$PWD"
  for arg in "$@"; do printf 'ARG=%s\n' "$arg"; done
} > "$COLLAB_CAPTURE"
""")
        self._write_executable(
            "claude",
            """#!/bin/sh
if [ "${1:-}" = "auth" ] && [ "${2:-}" = "status" ]; then
  if [ "${FAKE_CLAUDE_AUTH:-ok}" = "fail" ]; then
    printf '%s\n' '{"loggedIn":false,"authMethod":"none"}'
    exit 1
  fi
  printf '%s\n' '{"loggedIn":true,"authMethod":"claude.ai"}'
  exit 0
fi
exit 0
""")
        self._write_executable(
            "agent",
            """#!/bin/sh
if [ "${1:-}" = "status" ]; then
  if [ "${FAKE_CURSOR_AUTH:-ok}" = "fail" ]; then
    printf '%s\n' '{"status":"unauthenticated","isAuthenticated":false}'
    exit 0
  fi
  printf '%s\n' '{"status":"authenticated","isAuthenticated":true}'
  exit 0
fi
exit 0
""")

    def _write_executable(self, name, body):
        path = os.path.join(self.fake_bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def _env(self, **updates):
        env = dict(os.environ)
        for key in (
                "COLLAB_ROOT", "COLLAB_WATCH_ARGS", "COLLAB_WATCH_DETACH",
                "COLLAB_WATCH_LOG", "COLLAB_CLAUDE_EXEC_ARGS",
                "COLLAB_CODEX_EXEC_ARGS", "COLLAB_CLAUDE_AUTH_PREFLIGHT",
                "COLLAB_CURSOR_AUTH_PREFLIGHT", "CURSOR_BIN", "CURSOR_API_KEY",
                "CURSOR_MODEL", "CURSOR_READONLY", "CURSOR_AGENT_MODE",
                "CLAUDE_MODEL"):
            env.pop(key, None)
        env.update({
            "PATH": self.fake_bin + os.pathsep + env.get("PATH", ""),
            "COLLAB_CAPTURE": self.capture,
        })
        env.update(updates)
        return env

    def _run(self, agent, repo=None, **env):
        if os.path.exists(self.capture):
            os.unlink(self.capture)
        return subprocess.run(
            [WATCH_LAUNCHER, agent, "P", repo or self.repo],
            capture_output=True, text=True, env=self._env(**env), timeout=15)

    def _captured(self):
        with open(self.capture, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        values = {"args": []}
        for line in lines:
            key, value = line.split("=", 1)
            if key == "ARG":
                values["args"].append(value)
            else:
                values[key.lower()] = value
        return values

    def test_root_defaults_to_resolved_repo_and_explicit_root_wins(self):
        nested = os.path.join(self.repo, "nested")
        os.makedirs(nested)
        repo_arg = os.path.join(nested, "..") + os.sep

        out = self._run("codex", repo=repo_arg)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = self._captured()
        self.assertEqual(got["root"], os.path.join(self.repo, ".collab"))
        self.assertEqual(got["pwd"], self.repo)

        custom = os.path.join(self.tmp, "shared-bus")
        out = self._run("codex", COLLAB_ROOT=custom)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self._captured()["root"], custom)

    def test_agent_aliases_map_to_exact_watcher_exec_argv(self):
        cases = {
            "copilot": [os.path.join(PLUGIN_BIN, "copilot-exec.sh")],
            "codex": ["codex", "exec", "-c", "service_tier=fast"],
            "claude": [
                "claude", "--print", "--permission-mode", "dontAsk",
                "--no-chrome", "--no-session-persistence",
                "--model", "claude-sonnet-5"],
            "cursor": [os.path.join(PLUGIN_BIN, "cursor-exec.sh")],
            "agy": [os.path.join(PLUGIN_BIN, "antigravity-exec.sh")],
        }
        for alias, expected_exec in cases.items():
            with self.subTest(alias=alias):
                out = self._run(alias)
                self.assertEqual(out.returncode, 0, out.stderr)
                args = self._captured()["args"]
                marker = args.index("--exec")
                self.assertEqual(args[marker + 1:], expected_exec)

    def test_claude_auth_failure_stops_before_the_bus_can_be_claimed(self):
        out = self._run("claude", FAKE_CLAUDE_AUTH="fail")

        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(self.capture))
        self.assertIn(
            "Claude authentication is unavailable in this execution context",
            out.stderr)
        self.assertIn('"loggedIn":false', out.stderr)
        self.assertIn("No collab message was claimed", out.stderr)

    def test_claude_auth_preflight_can_be_explicitly_bypassed(self):
        out = self._run(
            "claude", FAKE_CLAUDE_AUTH="fail", COLLAB_CLAUDE_AUTH_PREFLIGHT="0")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.exists(self.capture))

    def test_claude_friendly_model_names_map_to_cli_ids(self):
        cases = {
            "sonnet 5": "claude-sonnet-5",
            "Sonnet 5": "claude-sonnet-5",
            "sonnet": "claude-sonnet-5",
            "claude-sonnet-5": "claude-sonnet-5",
            "opus": "claude-opus-5",
            "opus 5": "claude-opus-5",
            "fable": "claude-fable-5",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                out = self._run("claude", CLAUDE_MODEL=name)
                self.assertEqual(out.returncode, 0, out.stderr)
                args = self._captured()["args"]
                marker = args.index("--exec")
                exec_argv = args[marker + 1:]
                self.assertEqual(exec_argv[exec_argv.index("--model") + 1], expected)
                self.assertEqual(exec_argv.count("--model"), 1)

    def test_claude_exec_args_model_wins_over_claude_model(self):
        out = self._run(
            "claude",
            CLAUDE_MODEL="sonnet 5",
            COLLAB_CLAUDE_EXEC_ARGS="--model opus")
        self.assertEqual(out.returncode, 0, out.stderr)
        args = self._captured()["args"]
        exec_argv = args[args.index("--exec") + 1:]
        self.assertEqual(exec_argv[exec_argv.index("--model") + 1], "opus")
        self.assertEqual(exec_argv.count("--model"), 1)

    def test_missing_claude_binary_stops_before_the_bus_can_be_claimed(self):
        os.unlink(os.path.join(self.fake_bin, "claude"))
        out = self._run(
            "claude", PATH=self.fake_bin + os.pathsep + "/usr/bin:/bin")

        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(self.capture))
        self.assertIn("Claude Code is unavailable on PATH", out.stderr)
        self.assertIn("Install Claude Code", out.stderr)

    def test_cursor_auth_failure_stops_before_the_bus_can_be_claimed(self):
        out = self._run("cursor", FAKE_CURSOR_AUTH="fail")

        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(self.capture))
        self.assertIn(
            "Cursor authentication is unavailable in this execution context",
            out.stderr)
        self.assertIn('"isAuthenticated":false', out.stderr)
        self.assertIn("No collab message was claimed", out.stderr)

    def test_cursor_auth_preflight_can_be_explicitly_bypassed(self):
        out = self._run(
            "cursor", FAKE_CURSOR_AUTH="fail",
            COLLAB_CURSOR_AUTH_PREFLIGHT="0")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.exists(self.capture))

    def test_cursor_api_key_skips_login_status_check(self):
        out = self._run(
            "cursor", FAKE_CURSOR_AUTH="fail", CURSOR_API_KEY="cursor_test")

        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.exists(self.capture))

    def test_missing_cursor_binary_stops_before_the_bus_can_be_claimed(self):
        os.unlink(os.path.join(self.fake_bin, "agent"))
        out = self._run(
            "cursor",
            PATH=self.fake_bin + os.pathsep + "/usr/bin:/bin",
            HOME=self.tmp)

        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(self.capture))
        self.assertIn("Cursor CLI is unavailable on PATH", out.stderr)
        self.assertIn("No collab message was claimed", out.stderr)

    def test_missing_repo_fails_before_watcher_start(self):
        out = self._run("codex", repo=os.path.join(self.tmp, "missing"))
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(self.capture))


class TestCursorExecAdapter(unittest.TestCase):
    """Cursor CLI adapter: print-mode argv, model/readonly knobs, empty stdin."""

    def _invoke(self, env_overrides=None, extra_args=None, stdin="review payload"):
        adapter = os.path.join(PLUGIN_BIN, "cursor-exec.sh")
        with tempfile.TemporaryDirectory(prefix="cursor_adapter_test_") as tmp:
            fake = os.path.join(tmp, "agent")
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write(
                    "#!/usr/bin/env python3\n"
                    "import json, sys\n"
                    "print(json.dumps(sys.argv[1:]))\n")
            os.chmod(fake, 0o755)
            env = os.environ.copy()
            env["PATH"] = tmp + os.pathsep + env.get("PATH", "")
            env["HOME"] = tmp
            env["COLLAB_CWD"] = tmp
            for key in (
                    "CURSOR_BIN", "CURSOR_MODEL", "CURSOR_READONLY",
                    "CURSOR_AGENT_MODE", "CURSOR_API_KEY"):
                env.pop(key, None)
            env.update(env_overrides or {})
            return subprocess.run(
                [adapter, *(extra_args or [])],
                input=stdin,
                capture_output=True, text=True, env=env, timeout=10)

    def _run(self, env_overrides=None, extra_args=None, stdin="review payload"):
        out = self._invoke(env_overrides, extra_args, stdin)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_defaults_to_plan_mode_composer_and_text_print(self):
        args = self._run()
        self.assertEqual(args[0], "--print")
        self.assertEqual(args[args.index("--output-format") + 1], "text")
        self.assertIn("--trust", args)
        self.assertEqual(args[args.index("--mode") + 1], "plan")
        self.assertEqual(args[args.index("--model") + 1], "composer-2.5")
        self.assertNotIn("--force", args)
        self.assertEqual(args[-1], "review payload")

    def test_model_is_overridable(self):
        args = self._run({"CURSOR_MODEL": "gpt-5"})
        self.assertEqual(args[args.index("--model") + 1], "gpt-5")

    def test_friendly_model_names_map_to_cli_ids(self):
        cases = {
            "grok 4.6": "cursor-grok-4.6-high",
            "Grok 4.6": "cursor-grok-4.6-high",
            "grok-4.6": "cursor-grok-4.6-high",
            "grok 4.6 fast": "cursor-grok-4.6-high-fast",
            "composer 2.5": "composer-2.5",
            "composer 2.5 fast": "composer-2.5-fast",
            "cursor-grok-4.6-high": "cursor-grok-4.6-high",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                args = self._run({"CURSOR_MODEL": name})
                self.assertEqual(args[args.index("--model") + 1], expected)

    def test_edit_mode_adds_force_and_drops_plan(self):
        args = self._run({"CURSOR_READONLY": "0"})
        self.assertIn("--force", args)
        self.assertNotIn("--mode", args)

    def test_agent_mode_override_can_select_ask(self):
        args = self._run({"CURSOR_AGENT_MODE": "ask"})
        self.assertEqual(args[args.index("--mode") + 1], "ask")
        self.assertNotIn("--force", args)

    def test_empty_stdin_fails_closed(self):
        out = self._invoke(stdin="")
        self.assertEqual(out.returncode, 2)
        self.assertIn("empty stdin", out.stderr)

    def test_missing_binary_fails_before_prompt(self):
        out = self._invoke({
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent-cursor-home",
        })
        self.assertEqual(out.returncode, 1)
        self.assertIn("Cursor CLI not found", out.stderr)


class TestCopilotExecAdapter(unittest.TestCase):
    """Copilot starts on the preferred model/effort defaults and accepts per-run
    overrides without requiring a different watcher command."""

    @staticmethod
    def _git(repo, *args):
        import subprocess

        return subprocess.run(
            ["git", "-C", repo, *args],
            check=True, capture_output=True, text=True,
        ).stdout

    def _create_repo(self, path):
        os.makedirs(path)
        self._git(path, "init", "--quiet")
        self._git(path, "config", "user.name", "Adapter Test")
        self._git(path, "config", "user.email", "adapter@example.invalid")
        with open(os.path.join(path, "tracked.txt"), "w") as fh:
            fh.write("committed\n")
        self._git(path, "add", "tracked.txt")
        self._git(path, "commit", "--quiet", "-m", "base")

    def _invoke(self, env_overrides=None, extra_args=None):
        import subprocess

        adapter = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "plugins",
            "agent-collab", "skills", "agent-collab", "bin", "copilot-exec.sh")
        with tempfile.TemporaryDirectory(prefix="copilot_adapter_test_") as tmp:
            repo = os.path.join(tmp, "review-repo")
            self._create_repo(repo)
            fake = os.path.join(tmp, "copilot")
            with open(fake, "w") as fh:
                fh.write(
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "content = json.dumps(sys.argv[1:])\n"
                    "print(json.dumps({'type': 'assistant.message', "
                    "'data': {'content': content}}))\n"
                    "raise SystemExit(int(os.environ.get('FAKE_COPILOT_EXIT', '0')))\n")
            os.chmod(fake, 0o755)
            env = os.environ.copy()
            env["PATH"] = tmp + os.pathsep + env["PATH"]
            env.pop("COPILOT_MODEL", None)
            env.pop("COPILOT_REASONING_EFFORT", None)
            env.pop("COPILOT_READONLY", None)
            env.pop("COPILOT_CUSTOM_INSTRUCTIONS", None)
            env.update(env_overrides or {})
            out = subprocess.run(
                [adapter, "-C", repo, *(extra_args or [])],
                input="review payload",
                capture_output=True, text=True, env=env, timeout=10)
        return out

    def _run(self, env_overrides=None, extra_args=None):
        out = self._invoke(env_overrides, extra_args)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_defaults_to_claude_48_with_high_effort(self):
        args = self._run()
        self.assertEqual(args[args.index("--model") + 1], "claude-opus-4.8")
        self.assertEqual(
            args[args.index("--reasoning-effort") + 1], "high")
        self.assertEqual(args[args.index("--stream") + 1], "off")
        self.assertEqual(args[args.index("--output-format") + 1], "json")
        self.assertNotIn("--no-custom-instructions", args)
        self.assertIn("--deny-tool", args)
        c_values = [
            args[index + 1] for index, arg in enumerate(args) if arg == "-C"]
        self.assertEqual(len(c_values), 1)
        self.assertIn("agent-collab-copilot.", c_values[0])
        self.assertEqual(args[-2:], ["-p", "review payload"])

    def test_caller_cannot_override_adapter_transport_flags(self):
        args = self._run(
            extra_args=["--stream", "on", "--output-format", "text"])
        stream_values = [
            args[index + 1] for index, arg in enumerate(args) if arg == "--stream"]
        format_values = [
            args[index + 1]
            for index, arg in enumerate(args)
            if arg == "--output-format"
        ]
        self.assertEqual(stream_values, ["on", "off"])
        self.assertEqual(format_values, ["text", "json"])
        self.assertEqual(args[-2:], ["-p", "review payload"])

    def test_model_and_effort_are_independently_overridable(self):
        args = self._run({
            "COPILOT_MODEL": "gpt-5.6-terra",
            "COPILOT_REASONING_EFFORT": "max",
        })
        self.assertEqual(args[args.index("--model") + 1], "gpt-5.6-terra")
        self.assertEqual(
            args[args.index("--reasoning-effort") + 1], "max")

    def test_exact_output_mode_can_disable_custom_instructions(self):
        args = self._run({"COPILOT_CUSTOM_INSTRUCTIONS": "0"})
        self.assertIn("--no-custom-instructions", args)
        self.assertEqual(args[args.index("--stream") + 1], "off")

    def test_edit_mode_uses_the_callers_live_repository(self):
        args = self._run({"COPILOT_READONLY": "0"})
        self.assertNotIn("--deny-tool", args)
        c_values = [
            args[index + 1] for index, arg in enumerate(args) if arg == "-C"]
        self.assertEqual(len(c_values), 1)

    def test_readonly_snapshot_survives_destructive_shell_commands(self):
        import subprocess

        adapter = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "plugins",
            "agent-collab", "skills", "agent-collab", "bin", "copilot-exec.sh")
        with tempfile.TemporaryDirectory(prefix="copilot_adapter_attack_") as tmp:
            repo = os.path.join(tmp, "source-repo")
            self._create_repo(repo)

            tracked = os.path.join(repo, "tracked.txt")
            with open(tracked, "w") as fh:
                fh.write("staged\n")
            self._git(repo, "add", "tracked.txt")
            with open(tracked, "w") as fh:
                fh.write("staged plus unstaged\n")
            with open(os.path.join(repo, "staged-only.txt"), "w") as fh:
                fh.write("staged-only\n")
            self._git(repo, "add", "staged-only.txt")
            os.makedirs(os.path.join(repo, "notes"))
            untracked = os.path.join(repo, "notes", "review.txt")
            with open(untracked, "w") as fh:
                fh.write("untracked review evidence\n")
            odd_untracked_relative = os.path.join(
                "notes", "line\nand\ttab.txt")
            with open(os.path.join(repo, odd_untracked_relative), "w") as fh:
                fh.write("odd filename evidence\n")

            source_head = self._git(repo, "rev-parse", "HEAD")
            source_status = self._git(repo, "status", "--porcelain=v1")
            source_cached = self._git(repo, "diff", "--cached", "--binary")
            source_unstaged = self._git(repo, "diff", "--binary")

            fake = os.path.join(tmp, "copilot")
            with open(fake, "w") as fh:
                fh.write(
                    "#!/usr/bin/env python3\n"
                    "import json, os, subprocess, sys\n"
                    "args = sys.argv[1:]\n"
                    "c_indexes = [i for i, value in enumerate(args) if value == '-C']\n"
                    "repo = args[c_indexes[-1] + 1]\n"
                    "def git(*values):\n"
                    "    return subprocess.run(['git', '-C', repo, *values], "
                    "check=True, capture_output=True, text=True).stdout\n"
                    "payload = {\n"
                    "    'repo': repo,\n"
                    "    'cwd': os.getcwd(),\n"
                    "    'head': git('rev-parse', 'HEAD'),\n"
                    "    'status': git('status', '--porcelain=v1'),\n"
                    "    'cached': git('diff', '--cached', '--binary'),\n"
                    "    'unstaged': git('diff', '--binary'),\n"
                    "    'untracked': open(os.path.join(repo, 'notes', 'review.txt')).read(),\n"
                    "    'odd_untracked': open(os.path.join(\n"
                    "        repo, os.environ['ODD_UNTRACKED'])).read(),\n"
                    "    'remotes': git('remote', '-v'),\n"
                    "}\n"
                    "attack = subprocess.run(\n"
                    "    ['git', '-C', os.environ['ATTACK_REPO'], "
                    "'reset', '--hard', 'HEAD'],\n"
                    "    capture_output=True)\n"
                    "payload['live_attack_returncode'] = attack.returncode\n"
                    "payload['oldpwd'] = os.environ.get('OLDPWD')\n"
                    "subprocess.run(['git', '-C', repo, 'reset', '--hard', 'HEAD'], "
                    "check=True, capture_output=True)\n"
                    "subprocess.run(['git', '-C', repo, 'clean', '-fdx'], "
                    "check=True, capture_output=True)\n"
                    "print(json.dumps({'type': 'assistant.message', "
                    "'data': {'content': json.dumps(payload)}}))\n")
            os.chmod(fake, 0o755)

            env = os.environ.copy()
            env["PATH"] = tmp + os.pathsep + env["PATH"]
            env["COPILOT_READONLY"] = "1"
            env["ATTACK_REPO"] = repo
            env["ODD_UNTRACKED"] = odd_untracked_relative
            out = subprocess.run(
                [adapter, "-C", repo],
                input="review payload",
                capture_output=True, text=True, env=env, timeout=20,
            )

            self.assertEqual(out.returncode, 0, out.stderr)
            observed = json.loads(out.stdout)
            self.assertNotEqual(observed["repo"], repo)
            self.assertEqual(
                os.path.realpath(observed["cwd"]),
                os.path.realpath(observed["repo"]),
            )
            self.assertFalse(os.path.exists(observed["repo"]))
            self.assertEqual(observed["head"], source_head)
            self.assertEqual(observed["status"], source_status)
            self.assertEqual(observed["cached"], source_cached)
            self.assertEqual(observed["unstaged"], source_unstaged)
            self.assertEqual(
                observed["untracked"], "untracked review evidence\n")
            self.assertEqual(
                observed["odd_untracked"], "odd filename evidence\n")
            self.assertEqual(observed["remotes"], "")
            self.assertNotEqual(observed["live_attack_returncode"], 0)
            self.assertIsNone(observed["oldpwd"])

            # The fake Copilot reset and cleaned its snapshot. The live source
            # still has the exact pre-review HEAD, diffs, and untracked evidence.
            self.assertEqual(self._git(repo, "rev-parse", "HEAD"), source_head)
            self.assertEqual(
                self._git(repo, "status", "--porcelain=v1"), source_status)
            self.assertEqual(
                self._git(repo, "diff", "--cached", "--binary"), source_cached)
            self.assertEqual(
                self._git(repo, "diff", "--binary"), source_unstaged)
            with open(untracked) as fh:
                self.assertEqual(fh.read(), "untracked review evidence\n")

    def test_readonly_rejects_additional_live_directory(self):
        out = self._invoke(extra_args=["--add-dir", "/tmp/other-source"])

        self.assertEqual(out.returncode, 2)
        self.assertIn(
            "does not permit --add-dir",
            out.stderr,
        )

    def test_readonly_fails_closed_when_source_changes_during_capture(self):
        import shutil
        import subprocess

        adapter = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "plugins",
            "agent-collab", "skills", "agent-collab", "bin", "copilot-exec.sh")
        with tempfile.TemporaryDirectory(prefix="copilot_adapter_race_") as tmp:
            repo = os.path.join(tmp, "source-repo")
            self._create_repo(repo)
            fake_bin = os.path.join(tmp, "bin")
            os.makedirs(fake_bin)
            counter = os.path.join(tmp, "cached-diff-count")
            marker = os.path.join(tmp, "copilot-started")

            git_wrapper = os.path.join(fake_bin, "git")
            with open(git_wrapper, "w") as fh:
                fh.write(
                    "#!/usr/bin/env python3\n"
                    "import os, subprocess, sys\n"
                    "args = sys.argv[1:]\n"
                    "result = subprocess.run(\n"
                    "    [os.environ['REAL_GIT'], *args],\n"
                    "    stdout=subprocess.PIPE, stderr=subprocess.PIPE)\n"
                    "sys.stdout.buffer.write(result.stdout)\n"
                    "sys.stderr.buffer.write(result.stderr)\n"
                    "target = ['diff', '--cached', '--binary', '--full-index', "
                    "'--no-ext-diff']\n"
                    "if result.returncode == 0 and args[-len(target):] == target:\n"
                    "    count_path = os.environ['RACE_COUNTER']\n"
                    "    try:\n"
                    "        count = int(open(count_path).read()) + 1\n"
                    "    except FileNotFoundError:\n"
                    "        count = 1\n"
                    "    open(count_path, 'w').write(str(count))\n"
                    "    if count == 2:\n"
                    "        open(os.path.join(os.environ['RACE_REPO'], "
                    "'tracked.txt'), 'w').write('changed during snapshot\\n')\n"
                    "raise SystemExit(result.returncode)\n")
            os.chmod(git_wrapper, 0o755)

            fake_copilot = os.path.join(fake_bin, "copilot")
            with open(fake_copilot, "w") as fh:
                fh.write(
                    "#!/usr/bin/env python3\n"
                    "import os\n"
                    "open(os.environ['COPILOT_MARKER'], 'w').write('started')\n")
            os.chmod(fake_copilot, 0o755)

            env = os.environ.copy()
            env["PATH"] = fake_bin + os.pathsep + env["PATH"]
            env["REAL_GIT"] = shutil.which("git")
            env["RACE_COUNTER"] = counter
            env["RACE_REPO"] = repo
            env["COPILOT_MARKER"] = marker
            env["COPILOT_READONLY"] = "1"
            out = subprocess.run(
                [adapter, "-C", repo],
                input="review payload",
                capture_output=True, text=True, env=env, timeout=20,
            )

            self.assertEqual(out.returncode, 1, out.stderr)
            self.assertEqual(out.stdout, "")
            self.assertIn(
                "source repository changed while creating the read-only snapshot",
                out.stderr,
            )
            self.assertFalse(os.path.exists(marker))
            with open(os.path.join(repo, "tracked.txt")) as fh:
                self.assertEqual(fh.read(), "changed during snapshot\n")

    def test_snapshot_state_rejects_intent_to_add(self):
        import subprocess

        state_helper = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "plugins",
            "agent-collab", "skills", "agent-collab", "bin",
            "copilot-snapshot-state.py")
        with tempfile.TemporaryDirectory(prefix="copilot_adapter_ita_") as tmp:
            repo = os.path.join(tmp, "source-repo")
            self._create_repo(repo)
            with open(os.path.join(repo, "intent.txt"), "w") as fh:
                fh.write("intent-to-add\n")
            self._git(repo, "add", "-N", "intent.txt")

            out = subprocess.run(
                [sys.executable, state_helper, repo],
                capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(out.returncode, 2)
            self.assertEqual(out.stdout, "")
            self.assertIn("does not support intent-to-add", out.stderr)

    def test_invalid_custom_instruction_setting_fails_closed(self):
        import subprocess

        adapter = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "plugins",
            "agent-collab", "skills", "agent-collab", "bin", "copilot-exec.sh")
        env = os.environ.copy()
        env["COPILOT_CUSTOM_INSTRUCTIONS"] = "sometimes"
        out = subprocess.run(
            [adapter], input="review payload", capture_output=True, text=True,
            env=env, timeout=10)
        self.assertEqual(out.returncode, 2)
        self.assertIn("COPILOT_CUSTOM_INSTRUCTIONS", out.stderr)

    def test_nonzero_copilot_exit_withholds_valid_looking_content(self):
        out = self._invoke({"FAKE_COPILOT_EXIT": "7"})
        self.assertEqual(out.returncode, 7)
        self.assertEqual(out.stdout, "")
        self.assertIn("response withheld", out.stderr)


class TestCopilotJsonlExtractor(unittest.TestCase):
    def _run(self, records):
        import subprocess

        extractor = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "plugins",
            "agent-collab", "skills", "agent-collab", "bin",
            "copilot-jsonl-extract.py")
        return subprocess.run(
            [sys.executable, extractor], input=records, capture_output=True,
            timeout=10)

    def test_emits_long_assistant_content_byte_for_byte(self):
        content = (
            '{"schema":"example/1","token":"' + ("x" * 20000) +
            '","nested":{"values":[1,2,3]}}\n'
        )
        records = (
            json.dumps({"type": "session.start", "data": {"id": "abc"}}) + "\n" +
            json.dumps({"type": "assistant.message",
                        "data": {"content": content}}) + "\n" +
            json.dumps({"type": "session.end", "data": {}}) + "\n"
        ).encode()

        out = self._run(records)

        self.assertEqual(out.returncode, 0, out.stderr.decode())
        self.assertEqual(out.stdout, content.encode())

    def test_malformed_envelope_withholds_all_content(self):
        records = (
            json.dumps({"type": "assistant.message",
                        "data": {"content": '{"answer":1}'}}) +
            "\n{not-json}\n"
        ).encode()

        out = self._run(records)

        self.assertEqual(out.returncode, 2)
        self.assertEqual(out.stdout, b"")
        self.assertIn(b"malformed Copilot JSONL", out.stderr)

    def test_multiple_top_level_messages_select_last_response(self):
        records = "\n".join(
            json.dumps({
                "type": "assistant.message",
                "data": {"content": content},
            })
            for content in ("progress one", "progress two", "final answer")
        )
        out = self._run((records + "\n").encode())
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        self.assertEqual(out.stdout, b"final answer")

    def test_intermediate_tool_and_subagent_messages_are_not_final(self):
        records = (
            json.dumps({
                "type": "assistant.message",
                "data": {
                    "content": "",
                    "toolRequests": [{"toolCallId": "t1", "name": "read"}],
                },
            }) + "\n" +
            json.dumps({
                "type": "assistant.message",
                "data": {"content": "subagent result", "parentToolCallId": "t1"},
            }) + "\n" +
            json.dumps({
                "type": "assistant.message",
                "data": {"content": '{"answer":1}'},
            }) + "\n"
        ).encode()

        out = self._run(records)

        self.assertEqual(out.returncode, 0, out.stderr.decode())
        self.assertEqual(out.stdout, b'{"answer":1}')

    def test_missing_or_non_string_content_fails_closed(self):
        cases = (
            json.dumps({"type": "session.end", "data": {}}).encode(),
            json.dumps({"type": "assistant.message",
                        "data": {"content": {"answer": 1}}}).encode(),
        )
        for records in cases:
            with self.subTest(records=records):
                out = self._run(records + b"\n")
                self.assertEqual(out.returncode, 2)
                self.assertEqual(out.stdout, b"")


class TestCrossProcessFencing(Base):
    """The production topology is multiple processes sharing SQLite. Pin the two
    terminal races the protocol relies on instead of testing them only in-process."""

    def _cli(self, *args, timeout=30):
        return subprocess.run(
            [sys.executable, os.path.join(COLLAB_DIR, "collab.py"),
             "--root", self.tmp, *args],
            capture_output=True, text=True, timeout=timeout)

    def test_reclaim_fences_a_stale_completion_from_another_process(self):
        self.s.start("P", "t", "g", "claude-1")
        self.s.join("P", "codex-1")
        posted = self.s.post(
            "P", "claude-1", "codex-1", "review_request", "review", round_=1)
        stale = self.s.claim("P", "codex-1")
        self.s.reclaim("P", force=True)
        current = self.s.claim("P", "codex-1")

        out = self._cli(
            "complete", "--project", "P", "--from", "codex-1",
            "--claim-message", stale["claim_message_id"],
            "--claim-token", stale["claim_token"], "--type", "response",
            "--round", "1", "--body", "zombie response")

        self.assertNotEqual(out.returncode, 0)
        self.assertIn("claim_token mismatch", out.stderr)
        self.s.complete(
            "P", "codex-1", current["claim_message_id"],
            current["claim_token"], "response", "fresh response", round_=1)
        responses = [m for m in self.s.log("P") if m["type"] == "response"]
        self.assertEqual([m["body"] for m in responses], ["fresh response"])
        self.assertEqual(posted["message_id"], stale["claim_message_id"])

    def test_decide_closes_claim_while_watcher_process_is_running(self):
        self.s.start("P", "t", "g", "claude-1")
        self.s.join("P", "codex-1")
        request = self.s.post(
            "P", "claude-1", "codex-1", "review_request", "review", round_=1)
        agent = (
            "import sys,time; sys.stdin.read(); time.sleep(1); "
            "sys.stdout.write('late review')"
        )
        proc = subprocess.Popen(
            [sys.executable, os.path.join(COLLAB_DIR, "collab.py"),
             "--root", self.tmp, "watch", "--project", "P",
             "--agent", "codex-1", "--once", "--exec",
             sys.executable, "-c", agent],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 10
        while time.time() < deadline and not self.s.in_flight("P", "codex-1"):
            time.sleep(0.05)
        self.assertTrue(self.s.in_flight("P", "codex-1"))

        self.s.decide(
            "P", "claude-1", "close while reviewer runs",
            thread_id=request["message_id"])
        stdout, stderr = proc.communicate(timeout=15)

        self.assertEqual(proc.returncode, 0, stderr)
        self.assertIn('"processed": 0', stdout)
        self.assertIn("thread closed by decision", stderr)
        self.assertEqual(
            [m for m in self.s.log("P") if m["type"] == "response"], [])
        self.assertEqual(self.s.status("P")["open_threads"], [])


class TestDetachedWatcher(Base):
    """v0.4.5: `watch --detach` must SPAWN (fork+exec, new session), not continue in a
    forked child. The old double-fork daemonize died on a signal the moment the forked
    child touched sqlite3's native state (macOS fork-without-exec hazard) — silently, with
    an empty log and no traceback, and nondeterministically, which is how it hid."""

    def _run(self, *args, timeout=60):
        import subprocess
        bin_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collab.py")
        return subprocess.run(
            [sys.executable, bin_path, "--root", self.tmp, *args],
            capture_output=True, text=True, timeout=timeout)

    def test_detached_watcher_actually_processes_work(self):
        s = self.s
        s.start("P", "t", "g", "claude-1")
        s.post("P", "claude-1", "codex-1", "review_request", "review this", round_=1)
        out = self._run("watch", "--project", "P", "--agent", "codex-1",
                        "--detach", "--once", "--exec", "/bin/cat")
        self.assertEqual(out.returncode, 0, out.stderr)
        info = json.loads(out.stdout)
        self.assertTrue(info["detached"])
        self.assertIsInstance(info["pid"], int)      # a real spawned child, not a fork
        # the detached child must do the work: poll for codex-1's response
        deadline = time.time() + 45
        responses = []
        while time.time() < deadline:
            responses = [m for m in self.fresh_store().log("P")
                         if m["type"] == "response" and m["from_agent"] == "codex-1"]
            if responses:
                break
            time.sleep(0.5)
        try:
            self.assertTrue(responses,
                            "detached watcher never posted a response; log: "
                            + _read_log(info["log"]))
            # and it logged, rather than dying silently with an empty log
            self.assertIn("claimed", _read_log(info["log"]))
        finally:
            import subprocess
            subprocess.run(["pkill", "-f", f"watch --project P --agent codex-1"],
                           capture_output=True)


def _read_log(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return "(no log file)"


class TestTestModuleEntrypoint(unittest.TestCase):
    def test_unittest_main_guard_is_the_last_top_level_statement(self):
        import ast

        with open(__file__, encoding="utf-8") as fh:
            module = ast.parse(fh.read())

        def is_main_guard(node):
            return (
                isinstance(node, ast.If)
                and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__")

        guards = [node for node in module.body if is_main_guard(node)]
        self.assertEqual(len(guards), 1)
        self.assertIs(
            module.body[-1], guards[0],
            "placing unittest.main() above test classes makes direct execution "
            "silently skip every class below it")


if __name__ == "__main__":
    unittest.main(verbosity=2)

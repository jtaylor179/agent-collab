#!/usr/bin/env python3
"""collab — a durable, multi-agent collaboration message bus (Phase 1, SQLite backend).

Single-file CLI + importable library. Pure stdlib. Implements the protocol in
agent-collab-design.md v3:

  - projects / participants / blobs / artifacts / messages / inbox tables
  - WAL mode, monotonic per-project `seq` assigned under a write transaction
  - unified `inbox` (one row per recipient) for both direct and broadcast routing
  - atomic `claim` with a regenerated `claim_token` (lease fencing)
  - atomic `complete` (insert response + ack input) in a single transaction
  - idempotency_key dedupe: one response per reviewer per parent per round
  - content-hashed blob store; versioned artifact pointers
  - lease sweeper (expired claims return to pending)

Data layout (workspace-local):
  <root>/collab.db          all projects (namespaced by the `project` column)
  <root>/blobs/<sha256>     immutable content store
  default <root> = ./.collab  (override with --root or COLLAB_ROOT)
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone

DEFAULT_LEASE_MIN = 10
ROLES = ("initiator", "reviewer", "observer")
MSG_TYPES = (
    "question", "review_request", "response", "rebuttal",
    "proposal", "decision", "status", "heartbeat",
)
# Only these create work-queue (inbox) rows requiring a claim+complete/ack.
# decision/status/heartbeat are log-only notifications consumed by reading the log.
ACTIONABLE = ("question", "review_request", "response", "rebuttal", "proposal")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  name TEXT PRIMARY KEY, topic TEXT, goal TEXT,
  state TEXT NOT NULL DEFAULT 'open',
  next_seq INTEGER NOT NULL DEFAULT 1,
  max_rounds INTEGER DEFAULT 6,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS participants (
  project TEXT, agent_id TEXT, role TEXT,
  last_heartbeat TEXT,
  PRIMARY KEY (project, agent_id)
);
CREATE TABLE IF NOT EXISTS blobs (
  sha256 TEXT PRIMARY KEY, path TEXT, bytes INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  project TEXT, name TEXT, version INTEGER,
  sha256 TEXT REFERENCES blobs(sha256),
  created_by TEXT, created_at TEXT,
  PRIMARY KEY (project, name, version)
);
CREATE TABLE IF NOT EXISTS messages (
  message_id TEXT PRIMARY KEY,
  idempotency_key TEXT,
  project TEXT, thread_id TEXT, round INTEGER, parent_message_id TEXT,
  seq INTEGER, from_agent TEXT, to_agent TEXT, role TEXT, type TEXT,
  refs_json TEXT, body TEXT, created_at TEXT,
  UNIQUE (project, idempotency_key)
);
CREATE TABLE IF NOT EXISTS inbox (
  message_id TEXT, recipient TEXT,
  status TEXT DEFAULT 'pending',
  claimed_by TEXT, claim_token TEXT,
  leased_until TEXT, deliveries INTEGER DEFAULT 0,
  PRIMARY KEY (message_id, recipient)
);
CREATE INDEX IF NOT EXISTS idx_inbox ON inbox(recipient, status);
CREATE INDEX IF NOT EXISTS idx_msg_seq ON messages(project, seq);
"""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_id() -> str:
    return uuid.uuid4().hex


class CollabError(Exception):
    """User-facing error (lease lost, unknown project, etc.)."""


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.blobs_dir = os.path.join(self.root, "blobs")
        os.makedirs(self.blobs_dir, exist_ok=True)
        self.db_path = os.path.join(self.root, "collab.db")
        self.conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        try:
            # WAL gives the best concurrency, but some mounted/network filesystems
            # can't support its shared-memory file; fall back to a rollback journal.
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.executescript(SCHEMA)
            except sqlite3.OperationalError:
                self.conn.execute("PRAGMA journal_mode=DELETE")
                self.conn.executescript(SCHEMA)
        except sqlite3.OperationalError as e:
            # neither mode worked => the filesystem doesn't support SQLite file
            # locking at all (common with some network/FUSE/synced mounts).
            raise CollabError(
                f"cannot open the collab database at {self.db_path} ({e}). "
                "COLLAB_ROOT is on a filesystem that does not support SQLite file "
                "locking (common with some mounted/synced/network folders). Point "
                "COLLAB_ROOT at a local-disk path, e.g. "
                "export COLLAB_ROOT=\"$HOME/.collab\"."
            )

    def close(self):
        with contextlib.suppress(Exception):
            self.conn.close()

    # -- transaction context (BEGIN IMMEDIATE serializes writers across processes)
    @contextlib.contextmanager
    def write_tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # -- projects -----------------------------------------------------------
    def get_project(self, project: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE name=?", (project,)
        ).fetchone()
        if not row:
            raise CollabError(f"unknown project: {project}")
        return row

    def start(self, project, topic, goal, agent, role="initiator", max_rounds=6):
        ts = now_iso()
        with self.write_tx():
            exists = self.conn.execute(
                "SELECT 1 FROM projects WHERE name=?", (project,)
            ).fetchone()
            if exists:
                raise CollabError(f"project already exists: {project}")
            self.conn.execute(
                "INSERT INTO projects(name,topic,goal,state,next_seq,max_rounds,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (project, topic, goal, "gathering", 1, max_rounds, ts, ts),
            )
            self.conn.execute(
                "INSERT INTO participants(project,agent_id,role,last_heartbeat) "
                "VALUES(?,?,?,?)",
                (project, agent, role, ts),
            )
        return {"project": project, "state": "gathering", "initiator": agent}

    def join(self, project, agent, role="reviewer"):
        """Register a participant. A reviewer joining AFTER a broadcast still needs
        the work: backfill pending inbox rows for every open broadcast they missed
        (actionable, not from them, in a thread not yet closed by a decision).
        Without this, a `start -> broadcast -> join` flow silently drops the review.
        """
        p = self.get_project(project)
        ts = now_iso()
        backfilled = 0
        # Never silently change an existing participant's role. If the initiator tries
        # to "join" as a reviewer (the classic same-identity-on-both-sides mistake),
        # keep them as initiator and surface a warning instead of demoting them — which
        # would otherwise hide the collision from doctor.
        existing = self.conn.execute(
            "SELECT role FROM participants WHERE project=? AND agent_id=?",
            (project, agent),
        ).fetchone()
        warning = None
        if existing:
            effective_role = existing["role"]
            if existing["role"] == "initiator" and role == "reviewer":
                warning = (
                    f"'{agent}' is already the initiator of '{project}'. One agent cannot "
                    "be both initiator and reviewer — the reviewer must be a DIFFERENT "
                    "agent id (e.g. codex-1) with its own COLLAB_AGENT, on the same "
                    "COLLAB_ROOT.")
        else:
            effective_role = role
        with self.write_tx():
            self.conn.execute(
                "INSERT INTO participants(project,agent_id,role,last_heartbeat) "
                "VALUES(?,?,?,?) ON CONFLICT(project,agent_id) DO UPDATE SET "
                "last_heartbeat=excluded.last_heartbeat",
                (project, agent, effective_role, ts),
            )
            if effective_role == "reviewer" and p["state"] != "converged":
                ph = ",".join("?" * len(ACTIONABLE))
                rows = self.conn.execute(
                    f"SELECT m.message_id FROM messages m WHERE m.project=? "
                    f"AND m.to_agent='broadcast' AND m.type IN ({ph}) "
                    f"AND m.from_agent != ? "
                    f"AND m.thread_id NOT IN ("
                    f"  SELECT thread_id FROM messages WHERE project=? AND type='decision') "
                    f"AND NOT EXISTS (SELECT 1 FROM inbox i "
                    f"  WHERE i.message_id=m.message_id AND i.recipient=?)",
                    (project, *ACTIONABLE, agent, project, agent),
                ).fetchall()
                for r in rows:
                    self.conn.execute(
                        "INSERT INTO inbox(message_id,recipient,status,deliveries) "
                        "VALUES(?,?,'pending',0)",
                        (r["message_id"], agent),
                    )
                    backfilled += 1
        res = {"project": project, "agent": agent, "role": effective_role,
               "backfilled": backfilled}
        if warning:
            res["warning"] = warning
        return res

    def participants(self, project, role=None):
        q = "SELECT * FROM participants WHERE project=?"
        args = [project]
        if role:
            q += " AND role=?"
            args.append(role)
        return self.conn.execute(q, args).fetchall()

    def heartbeat(self, project, agent):
        with self.write_tx():
            self.conn.execute(
                "UPDATE participants SET last_heartbeat=? WHERE project=? AND agent_id=?",
                (now_iso(), project, agent),
            )

    def set_state(self, project, state):
        with self.write_tx():
            self.conn.execute(
                "UPDATE projects SET state=?, updated_at=? WHERE name=?",
                (state, now_iso(), project),
            )

    # -- blobs / artifacts --------------------------------------------------
    def put_blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = os.path.join(self.blobs_dir, sha)
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(data)
        with self.write_tx():
            self.conn.execute(
                "INSERT OR IGNORE INTO blobs(sha256,path,bytes,created_at) "
                "VALUES(?,?,?,?)",
                (sha, path, len(data), now_iso()),
            )
        return sha

    def put_artifact(self, project, name, data: bytes, by: str):
        self.get_project(project)
        sha = self.put_blob(data)
        with self.write_tx():
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM artifacts "
                "WHERE project=? AND name=?",
                (project, name),
            ).fetchone()
            version = row["v"] + 1
            self.conn.execute(
                "INSERT INTO artifacts(project,name,version,sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (project, name, version, sha, by, now_iso()),
            )
        return {"artifact": f"{name}@v{version}", "name": name,
                "version": version, "sha256": sha}

    def get_artifact(self, project, name, version=None):
        if version is None:
            row = self.conn.execute(
                "SELECT * FROM artifacts WHERE project=? AND name=? "
                "ORDER BY version DESC LIMIT 1",
                (project, name),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM artifacts WHERE project=? AND name=? AND version=?",
                (project, name, version),
            ).fetchone()
        if not row:
            raise CollabError(f"artifact not found: {name}@{version or 'latest'}")
        with open(os.path.join(self.blobs_dir, row["sha256"]), "rb") as fh:
            data = fh.read()
        # integrity check (hash-verified, R5)
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise CollabError(f"blob integrity check failed for {name}@v{row['version']}")
        return row, data

    # -- messaging ----------------------------------------------------------
    def _recipients(self, project, from_agent, to_agent):
        if to_agent and to_agent != "broadcast":
            return [to_agent]
        # broadcast => every reviewer except the sender
        rows = self.participants(project, role="reviewer")
        return [r["agent_id"] for r in rows if r["agent_id"] != from_agent]

    def _insert_message(self, project, from_agent, to_agent, mtype, body,
                        thread_id=None, round_=None, parent=None,
                        refs=None, idempotency_key=None, role=None):
        """Insert a message + its inbox rows. MUST be called inside write_tx().
        Returns (message_id, was_duplicate)."""
        if idempotency_key:
            dup = self.conn.execute(
                "SELECT * FROM messages WHERE project=? AND idempotency_key=?",
                (project, idempotency_key),
            ).fetchone()
            if dup:
                # A duplicate is only a safe no-op if it is the SAME logical write.
                # A key reused for a *different* write is a collision: raise, so the
                # caller (e.g. complete()) rolls back and never acks lost work.
                if (dup["from_agent"], dup["to_agent"], dup["type"],
                        dup["parent_message_id"], dup["round"]) != (
                        from_agent, to_agent, mtype, parent, round_):
                    raise CollabError(
                        f"idempotency key collision: '{idempotency_key}' was already "
                        f"used for a different logical write (msg {dup['message_id']})")
                return dup["message_id"], True

        # assign seq transactionally
        prow = self.conn.execute(
            "SELECT next_seq FROM projects WHERE name=?", (project,)
        ).fetchone()
        if not prow:
            raise CollabError(f"unknown project: {project}")
        seq = prow["next_seq"]
        self.conn.execute(
            "UPDATE projects SET next_seq=?, updated_at=? WHERE name=?",
            (seq + 1, now_iso(), project),
        )

        message_id = new_id()
        # Thread resolution: explicit thread_id wins; else inherit the PARENT's
        # thread (so a reply to a mid-thread response stays in the root thread,
        # not just thread=parent); else this message starts its own thread.
        if thread_id is None and parent:
            prow_t = self.conn.execute(
                "SELECT thread_id FROM messages WHERE message_id=?", (parent,)
            ).fetchone()
            thread_id = prow_t["thread_id"] if prow_t else parent
        thread_id = thread_id or message_id
        self.conn.execute(
            "INSERT INTO messages(message_id,idempotency_key,project,thread_id,round,"
            "parent_message_id,seq,from_agent,to_agent,role,type,refs_json,body,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (message_id, idempotency_key, project, thread_id, round_, parent, seq,
             from_agent, to_agent, role, mtype,
             json.dumps(refs or {}), body, now_iso()),
        )
        if mtype in ACTIONABLE:
            for rcpt in self._recipients(project, from_agent, to_agent):
                self.conn.execute(
                    "INSERT INTO inbox(message_id,recipient,status,deliveries) "
                    "VALUES(?,?,'pending',0)",
                    (message_id, rcpt),
                )
        return message_id, False

    def post(self, project, from_agent, to_agent, mtype, body, **kw):
        self.get_project(project)
        if mtype not in MSG_TYPES:
            raise CollabError(f"unknown message type: {mtype}")
        with self.write_tx():
            mid, dup = self._insert_message(
                project, from_agent, to_agent, mtype, body, **kw)
        res = {"message_id": mid, "duplicate": dup}
        if to_agent == "broadcast" and mtype in ACTIONABLE and not dup:
            if not self._recipients(project, from_agent, "broadcast"):
                res["warning"] = (
                    "no reviewers have joined yet — this broadcast has zero recipients "
                    "right now. Reviewers who join later are backfilled automatically.")
        return res

    def review(self, project, agent, file_path, name=None, topic="", goal="",
               focus=None, round_=1):
        """One step: put a file up for review. Creates the project if needed, snapshots
        the file as an artifact, and broadcasts a review_request. Requires a real work
        product — this is the path that makes an empty, un-reviewable project impossible.
        """
        if not os.path.isfile(file_path):
            raise CollabError(f"work product not found: {file_path}")
        name = name or os.path.basename(file_path)
        exists = self.conn.execute(
            "SELECT 1 FROM projects WHERE name=?", (project,)
        ).fetchone()
        if not exists:
            self.start(project, topic, goal, agent)
        with open(file_path, "rb") as fh:
            data = fh.read()
        art = self.put_artifact(project, name, data, agent)
        body = focus or f"Please review {art['artifact']}."
        posted = self.post(project, agent, "broadcast", "review_request", body,
                           round_=round_, refs={"artifact": art["artifact"]})
        out = {"project": project, "artifact": art["artifact"],
               "review_request": posted["message_id"], "round": round_}
        if posted.get("warning"):
            out["warning"] = posted["warning"]
        return out

    def poll(self, project, agent):
        """List pending inbox rows for agent (peek, no claim)."""
        return self.conn.execute(
            "SELECT i.*, m.seq, m.type, m.from_agent, m.round, m.parent_message_id "
            "FROM inbox i JOIN messages m USING(message_id) "
            "WHERE i.recipient=? AND m.project=? AND i.status='pending' "
            "ORDER BY m.seq",
            (agent, project),
        ).fetchall()

    def sweep(self, project):
        """Return expired claims to pending. Runs inline before each claim."""
        with self.write_tx():
            cur = self.conn.execute(
                "UPDATE inbox SET status='pending', claimed_by=NULL, claim_token=NULL, "
                "leased_until=NULL "
                "WHERE status='claimed' AND leased_until IS NOT NULL "
                "AND leased_until < ? "
                "AND message_id IN (SELECT message_id FROM messages WHERE project=?)",
                (now_iso(), project),
            )
            return cur.rowcount

    def claim(self, project, agent, lease_min=DEFAULT_LEASE_MIN, wait=0.0,
              poll_interval=2.0):
        """Claim the next pending inbox row for agent. If wait>0, block up to `wait`
        seconds (polling every poll_interval) until something is claimable, then return
        it; return None on timeout. wait=0 is the original non-blocking behavior."""
        got = self._claim_once(project, agent, lease_min)
        if got is not None or wait <= 0:
            return got
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(min(poll_interval, max(0.05, deadline - time.monotonic())))
            got = self._claim_once(project, agent, lease_min)
            if got is not None:
                return got
        return None

    def _claim_once(self, project, agent, lease_min=DEFAULT_LEASE_MIN):
        """Atomically claim the next pending inbox row for agent; mint claim_token."""
        self.sweep(project)
        with self.write_tx():
            cand = self.conn.execute(
                "SELECT i.message_id FROM inbox i JOIN messages m USING(message_id) "
                "WHERE i.recipient=? AND i.status='pending' AND m.project=? "
                "ORDER BY m.seq LIMIT 1",
                (agent, project),
            ).fetchone()
            if not cand:
                return None
            token = new_id()
            leased_until = datetime.now(timezone.utc).timestamp() + lease_min * 60
            leased_iso = datetime.fromtimestamp(
                leased_until, timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self.conn.execute(
                "UPDATE inbox SET status='claimed', claimed_by=?, claim_token=?, "
                "leased_until=?, deliveries=deliveries+1 "
                "WHERE message_id=? AND recipient=?",
                (agent, token, leased_iso, cand["message_id"], agent),
            )
            inb = self.conn.execute(
                "SELECT deliveries FROM inbox WHERE message_id=? AND recipient=?",
                (cand["message_id"], agent),
            ).fetchone()
            msg = self.conn.execute(
                "SELECT * FROM messages WHERE message_id=?", (cand["message_id"],)
            ).fetchone()
        out = dict(msg)
        out["claim_token"] = token
        out["claim_message_id"] = cand["message_id"]
        out["recipient"] = agent
        out["deliveries"] = inb["deliveries"]
        return out

    def mark_stalled(self, project, message_id, recipient, claim_token):
        """Take a poison message out of rotation: a 'stalled' row is never claimed
        again and is not resurrected by the sweeper. Surfaced via status/log.

        Fenced by claim_token (like ack/complete): a stale worker whose lease
        expired and was reclaimed must NOT stall the current owner's row."""
        with self.write_tx():
            self._verify_lease(message_id, recipient, claim_token)
            self.conn.execute(
                "UPDATE inbox SET status='stalled' WHERE message_id=? AND recipient=?",
                (message_id, recipient),
            )
        return {"stalled": message_id, "recipient": recipient}

    def _verify_lease(self, message_id, recipient, claim_token):
        row = self.conn.execute(
            "SELECT * FROM inbox WHERE message_id=? AND recipient=?",
            (message_id, recipient),
        ).fetchone()
        if not row:
            raise CollabError("no such inbox row")
        if row["status"] != "claimed":
            raise CollabError(f"inbox row not in claimed state (is {row['status']})")
        if row["claim_token"] != claim_token:
            raise CollabError("lease lost: claim_token mismatch (reclaimed by another worker)")
        if row["leased_until"] and row["leased_until"] < now_iso():
            raise CollabError("lease expired")
        return row

    def ack(self, project, agent, message_id, claim_token):
        """Mark a claimed inbox row done (for non-reply work). Fenced by token."""
        with self.write_tx():
            self._verify_lease(message_id, agent, claim_token)
            self.conn.execute(
                "UPDATE inbox SET status='done' WHERE message_id=? AND recipient=?",
                (message_id, agent),
            )
        return {"acked": message_id}

    def extend(self, project, agent, message_id, claim_token, lease_min=DEFAULT_LEASE_MIN):
        """Extend the lease on a claimed row. Fenced by token (heartbeat use, Phase 3)."""
        with self.write_tx():
            self._verify_lease(message_id, agent, claim_token)
            leased = datetime.now(timezone.utc).timestamp() + lease_min * 60
            leased_iso = datetime.fromtimestamp(
                leased, timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self.conn.execute(
                "UPDATE inbox SET leased_until=? WHERE message_id=? AND recipient=?",
                (leased_iso, message_id, agent),
            )
        return {"extended": message_id, "leased_until": leased_iso}

    def complete(self, project, agent, claim_message_id, claim_token,
                 mtype, body, to_agent=None, round_=None,
                 parent=None, thread_id=None, refs=None,
                 idempotency_key=None, role="reviewer"):
        """ATOMIC: verify lease -> insert response -> mark claimed row done. One txn.

        This is the verb that makes 'crash-after-post-before-ack' impossible on
        SQLite: the response and the ack commit together or not at all.

        Default routing is reply-to-sender: the response goes to whoever sent the
        claimed message. Default threading keeps the reply in the claimed message's
        thread, so a rebuttal/proposal replying to a response stays in the original
        convergence thread instead of forking a new one. Both are overridable.
        """
        self.get_project(project)
        if mtype not in MSG_TYPES:
            raise CollabError(f"unknown message type: {mtype}")
        parent = parent or claim_message_id
        with self.write_tx():
            self._verify_lease(claim_message_id, agent, claim_token)
            src = self.conn.execute(
                "SELECT from_agent, thread_id FROM messages WHERE message_id=?",
                (claim_message_id,),
            ).fetchone()
            if to_agent is None:
                to_agent = src["from_agent"] if src else "broadcast"
            if thread_id is None:
                thread_id = src["thread_id"] if src else None
            mid, dup = self._insert_message(
                project, agent, to_agent, mtype, body,
                round_=round_, parent=parent, thread_id=thread_id, refs=refs,
                idempotency_key=idempotency_key, role=role)
            self.conn.execute(
                "UPDATE inbox SET status='done' WHERE message_id=? AND recipient=?",
                (claim_message_id, agent),
            )
        return {"message_id": mid, "duplicate": dup, "completed": claim_message_id}

    def decide(self, project, from_agent, body, thread_id=None, parent=None,
               idempotency_key=None):
        """Initiator posts the binding decision and converges the project.

        Pass --thread/--parent to attach the decision to the thread it closes so
        the log threads cleanly; the project-level state also flips to converged.
        """
        with self.write_tx():
            mid, dup = self._insert_message(
                project, from_agent, "broadcast", "decision", body,
                thread_id=thread_id, parent=parent,
                idempotency_key=idempotency_key, role="initiator")
            self.conn.execute(
                "UPDATE projects SET state='converged', updated_at=? WHERE name=?",
                (now_iso(), project),
            )
            # The decision is terminal: close any outstanding inbox work so a converged
            # project doesn't leave permanent 'pending' notifications (e.g. the final
            # response/decision sitting unacked in a reviewer's inbox forever).
            cur = self.conn.execute(
                "UPDATE inbox SET status='done' WHERE status IN ('pending','claimed') "
                "AND message_id IN (SELECT message_id FROM messages WHERE project=?)",
                (project,),
            )
        return {"message_id": mid, "duplicate": dup, "state": "converged",
                "closed_deliveries": cur.rowcount}

    def log(self, project, since_seq=0):
        return self.conn.execute(
            "SELECT * FROM messages WHERE project=? AND seq>? ORDER BY seq",
            (project, since_seq),
        ).fetchall()

    def status(self, project):
        p = self.get_project(project)
        parts = self.participants(project)
        pending = self.conn.execute(
            "SELECT i.recipient, COUNT(*) AS n FROM inbox i "
            "JOIN messages m USING(message_id) "
            "WHERE m.project=? AND i.status='pending' GROUP BY i.recipient",
            (project,),
        ).fetchall()
        stalled = self.conn.execute(
            "SELECT i.recipient, i.message_id, m.type, i.deliveries FROM inbox i "
            "JOIN messages m USING(message_id) "
            "WHERE m.project=? AND i.status='stalled' ORDER BY m.seq",
            (project,),
        ).fetchall()
        msg_count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE project=?", (project,)
        ).fetchone()["n"]
        # A converged project is terminal: nothing is open regardless of per-thread
        # decision linkage. Otherwise, a thread is open until it contains a decision.
        if p["state"] == "converged":
            open_threads = []
        else:
            # A thread is "open" only if it contains an ACTIONABLE message and has no
            # decision. Threads made only of status/heartbeat/decision (e.g. a stall
            # audit entry) are informational and never count as open work.
            ph = ",".join("?" * len(ACTIONABLE))
            open_threads = self.conn.execute(
                f"SELECT DISTINCT thread_id FROM messages WHERE project=? "
                f"AND type IN ({ph}) AND thread_id NOT IN "
                f"(SELECT thread_id FROM messages WHERE project=? AND type='decision')",
                (project, *ACTIONABLE, project),
            ).fetchall()
        return {
            "project": project,
            "root": self.root,
            "state": p["state"],
            "round_budget": p["max_rounds"],
            "messages": msg_count,
            "participants": [
                {"agent": r["agent_id"], "role": r["role"],
                 "last_heartbeat": r["last_heartbeat"]} for r in parts
            ],
            "pending": {r["recipient"]: r["n"] for r in pending},
            "stalled": [
                {"recipient": r["recipient"], "message_id": r["message_id"],
                 "type": r["type"], "deliveries": r["deliveries"]} for r in stalled
            ],
            "open_threads": [r["thread_id"] for r in open_threads],
        }

    def list_projects(self):
        """List every project under this COLLAB_ROOT (projects are per-root)."""
        rows = self.conn.execute(
            "SELECT name, state, created_at, updated_at FROM projects "
            "ORDER BY updated_at DESC"
        ).fetchall()
        projects = []
        for r in rows:
            mc = self.conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE project=?", (r["name"],)
            ).fetchone()["n"]
            pc = self.conn.execute(
                "SELECT COUNT(*) AS n FROM participants WHERE project=?", (r["name"],)
            ).fetchone()["n"]
            projects.append({
                "project": r["name"], "state": r["state"],
                "messages": mc, "participants": pc, "updated_at": r["updated_at"],
            })
        return {"root": self.root, "count": len(projects), "projects": projects}

    def delete_project(self, project):
        """Delete a project and its messages, inbox rows, artifacts, and participants.
        Content blobs are left on disk (they are content-addressed and may be shared by
        other projects; orphans are harmless)."""
        self.get_project(project)  # raises if it doesn't exist
        with self.write_tx():
            self.conn.execute(
                "DELETE FROM inbox WHERE message_id IN "
                "(SELECT message_id FROM messages WHERE project=?)", (project,))
            self.conn.execute("DELETE FROM messages WHERE project=?", (project,))
            self.conn.execute("DELETE FROM artifacts WHERE project=?", (project,))
            self.conn.execute("DELETE FROM participants WHERE project=?", (project,))
            self.conn.execute("DELETE FROM projects WHERE name=?", (project,))
        return {"deleted": project}

    def doctor(self, project, agent):
        """Diagnose setup and tell the caller what to do next. Designed so a skill can
        relay the `hints` to the user in plain language. Never raises on a missing
        project — that's one of the things it checks for."""
        exists = self.conn.execute(
            "SELECT * FROM projects WHERE name=?", (project,)
        ).fetchone()
        out = {
            "root": self.root, "agent": agent, "project": project,
            "project_exists": bool(exists),
            "state": exists["state"] if exists else None,
            "participants": [], "you_registered": False, "your_role": None,
            "pending_for_you": 0, "hints": [],
        }
        h = out["hints"]
        h.append(f"All agents in this project MUST use COLLAB_ROOT={self.root} "
                 "(a local-disk path) and each a DISTINCT agent id.")
        if not agent:
            h.append("No agent identity resolved. Set COLLAB_AGENT (e.g. claude-1 for "
                     "Claude, codex-1 for Codex) or pass --agent.")
        if not exists:
            h.append(f"Project '{project}' does not exist yet. To start it you must "
                     "provide a work product to review (a file). Starting from just a "
                     "name creates nothing useful.")
            return out
        parts = self.participants(project)
        out["participants"] = [{"agent": r["agent_id"], "role": r["role"]} for r in parts]
        mine = next((r for r in parts if r["agent_id"] == agent), None)
        out["you_registered"] = bool(mine)
        out["your_role"] = mine["role"] if mine else None
        out["pending_for_you"] = len(self.poll(project, agent)) if agent else 0
        distinct = {r["agent_id"] for r in parts}
        has_rr = self.conn.execute(
            "SELECT 1 FROM messages WHERE project=? AND type='review_request' LIMIT 1",
            (project,),
        ).fetchone()
        out["ready_for_review"] = bool(has_rr)

        # Collision / single-participant check FIRST — this is the failure you hit.
        if len(distinct) < 2:
            who = ", ".join(sorted(distinct)) or "none"
            h.append(f"Only one participant so far ({who}). Collaboration needs a SECOND "
                     "agent with a DIFFERENT id on the same COLLAB_ROOT. If your other "
                     "agent isn't showing up here, it is either using a different "
                     "COLLAB_ROOT or the same agent id — fix its COLLAB_AGENT/COLLAB_ROOT.")
        if not has_rr:
            h.append("No review request has been broadcast yet — there is nothing for "
                     "reviewers to do. Post a work product and broadcast a review_request "
                     "(the `review` verb / `/collab-review <file>` does this in one step).")
        if agent and not mine:
            h.append(f"You ({agent}) are not in this project yet — join as a reviewer.")
        if out["pending_for_you"]:
            h.append(f"You have {out['pending_for_you']} item(s) to handle: claim each, "
                     "read the referenced artifact, and respond/complete.")
        elif mine and mine["role"] == "reviewer":
            h.append("Nothing pending for you right now.")
        return out


# --------------------------------------------------------------------------- #
# watcher — the hands-off reviewer (Phase 3)
# --------------------------------------------------------------------------- #
def _agent_payload(store, project, agent, claimed):
    """Build the JSON the agent reads on stdin: the claimed message plus the exact
    artifact version it references. Never interpolated into a shell command."""
    refs = json.loads(claimed.get("refs_json") or "{}")
    artifact = None
    ref = refs.get("artifact")
    if ref and "@v" in ref:
        name, ver = ref.rsplit("@v", 1)
        try:
            _row, data = store.get_artifact(project, name, int(ver))
            artifact = {"ref": ref, "content": data.decode("utf-8", "replace")}
        except (CollabError, ValueError) as e:
            artifact = {"ref": ref, "error": str(e)}
    return json.dumps({
        "instructions": (
            f"You are {agent}, an AI reviewer collaborating with other agents over a "
            "shared bus. Read the message and the referenced artifact, then write ONLY "
            "your review to stdout as plain text. Lead with your strongest substantive "
            "objection; if you genuinely agree, say specifically why and name the one "
            "thing you would still change. You are reviewing the GOAL, not just the "
            "artifact as written: if you think the whole approach is wrong, say so "
            "plainly and propose the alternative — challenging the premise is in scope, "
            "not only refining the details. No preamble, no sign-off."),
        "message": {k: claimed.get(k) for k in (
            "type", "from_agent", "round", "body", "thread_id", "parent_message_id")},
        "artifact": artifact,
    }, indent=2)


def _run_agent_with_heartbeat(store, project, agent, claimed, exec_argv,
                              payload, lease_min, agent_timeout=None):
    """Invoke the agent (argv list) with the payload on stdin, extending the lease
    in a background thread so a long review is not falsely redelivered. Kills the
    agent if it exceeds agent_timeout. Returns (returncode, stdout, stderr)."""
    import subprocess
    import threading

    cm, tok = claimed["claim_message_id"], claimed["claim_token"]
    stop = threading.Event()

    def beat():
        # heartbeat on its OWN connection (sqlite objects aren't cross-thread)
        hb = Store(store.root)
        try:
            interval = max(1.0, lease_min * 60.0 / 3.0)
            while not stop.wait(interval):
                try:
                    hb.extend(project, agent, cm, tok, lease_min)
                except CollabError:
                    return  # lease already lost; nothing to extend
        finally:
            hb.close()

    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        proc = subprocess.Popen(
            exec_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(input=payload, timeout=agent_timeout)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            # a hung agent must not hold the lease forever (Codex finding #1):
            # kill it, stop heartbeating, return failure so the claim expires.
            proc.kill()
            out, err = proc.communicate()
            return -1, out, (err or "") + f"\n[watch] agent exceeded {agent_timeout}s; killed"
    finally:
        stop.set()
        t.join(timeout=2)


def watch(store, project, agent, exec_argv, poll_interval=2.0, once=False,
          idle_exit=False, max_items=None, lease_min=DEFAULT_LEASE_MIN,
          reply_type="response", agent_timeout=600.0, max_deliveries=5,
          log_fh=sys.stderr):
    """Poll the bus for work addressed to `agent`; for each claimed message, invoke
    the agent single-shot and post its output back atomically. The agent-agnostic
    answer to 'can it run a daemon': the watcher waits, the agent thinks.

    Robustness: a hung agent is killed after agent_timeout; a message that fails
    max_deliveries times is marked 'stalled' (taken out of rotation, surfaced to
    the human) instead of retrying forever; a lost lease at complete time is logged
    and skipped rather than crashing the daemon."""
    store.get_project(project)
    if agent not in {r["agent_id"] for r in store.participants(project)}:
        store.join(project, agent)  # auto-join as reviewer (idempotent)
    processed = 0
    while True:
        claimed = store.claim(project, agent, lease_min)
        if claimed is None:
            if once or idle_exit:
                break
            time.sleep(poll_interval)
            continue
        cm, tok = claimed["claim_message_id"], claimed["claim_token"]
        rnd = claimed.get("round")
        deliveries = claimed.get("deliveries", 1)
        print(f"[watch] {agent} claimed {claimed['type']} {cm[:8]} "
              f"(round {rnd}, delivery {deliveries})", file=log_fh, flush=True)
        payload = _agent_payload(store, project, agent, claimed)
        rc, out, err = _run_agent_with_heartbeat(
            store, project, agent, claimed, exec_argv, payload, lease_min,
            agent_timeout=agent_timeout)
        review = (out or "").strip()

        if rc != 0 or not review:
            if max_deliveries and deliveries >= max_deliveries:
                try:
                    store.mark_stalled(project, cm, agent, tok)
                    # leave an audit trail in the log so a human notices (status-only,
                    # kept in the stalled message's thread so it doesn't fork a thread)
                    store.post(project, agent, "broadcast", "status",
                               f"stalled message {cm}: agent failed {deliveries} times "
                               f"(last rc={rc})", thread_id=claimed.get("thread_id"))
                    print(f"[watch] {cm[:8]} STALLED after {deliveries} failed deliveries "
                          f"(rc={rc}); taken out of rotation. stderr: "
                          f"{(err or '').strip()[:200]}", file=log_fh, flush=True)
                except CollabError as e:
                    print(f"[watch] could not stall {cm[:8]} ({e}); lease no longer held",
                          file=log_fh, flush=True)
            else:
                # leave the claim to expire so the sweeper redelivers it
                print(f"[watch] agent failed (rc={rc}); leaving for redelivery "
                      f"({deliveries}/{max_deliveries}). stderr: "
                      f"{(err or '').strip()[:200]}", file=log_fh, flush=True)
            if once:
                break
            continue

        try:
            store.complete(project, agent, cm, tok, reply_type, review,
                           round_=rnd, parent=cm,
                           idempotency_key=f"{agent}:{reply_type}:{cm}:r{rnd}")
        except CollabError as e:
            # If the thread converged mid-review, decide() already marked this row done
            # (terminal). It will NOT be redelivered, so don't claim it will be.
            if "is done" in str(e):
                print(f"[watch] {cm[:8]} thread closed by decision; not redelivering "
                      f"({e})", file=log_fh, flush=True)
            else:
                # lease lost/expired despite heartbeat (e.g. machine slept): don't crash
                print(f"[watch] could not post reply to {cm[:8]} ({e}); "
                      f"leaving for redelivery", file=log_fh, flush=True)
            if once:
                break
            continue
        print(f"[watch] {agent} responded to {cm[:8]}", file=log_fh, flush=True)
        processed += 1
        if once or (max_items and processed >= max_items):
            break
    return processed


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _read_body(args):
    """Resolve message body from --body, --body-file, or stdin ('-')."""
    if getattr(args, "body_file", None):
        if args.body_file == "-":
            return sys.stdin.read()
        with open(args.body_file, "r", encoding="utf-8") as fh:
            return fh.read()
    return getattr(args, "body", None) or ""


def _emit(obj):
    if isinstance(obj, list):
        obj = [dict(r) for r in obj]
    elif isinstance(obj, sqlite3.Row):
        obj = dict(obj)
    print(json.dumps(obj, indent=2, default=str))


def _refs(args):
    refs = {}
    if getattr(args, "artifact", None):
        refs["artifact"] = args.artifact
    if getattr(args, "blob", None):
        refs["blob"] = args.blob
    return refs or None


def build_parser():
    p = argparse.ArgumentParser(prog="collab", description="multi-agent collab bus")
    p.add_argument("--root", default=os.environ.get("COLLAB_ROOT", "./.collab"),
                   help="data dir (default ./.collab or $COLLAB_ROOT)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="create a project")
    s.add_argument("--project", required=True)
    s.add_argument("--topic", default="")
    s.add_argument("--goal", default="")
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")
    s.add_argument("--max-rounds", type=int, default=6)

    s = sub.add_parser("join", help="join a project as reviewer")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")
    s.add_argument("--role", default="reviewer", choices=ROLES)

    s = sub.add_parser("post", help="post a message")
    s.add_argument("--project", required=True)
    s.add_argument("--from", dest="from_agent", default=os.environ.get("COLLAB_AGENT"),
                   help="sender id; defaults to $COLLAB_AGENT")
    s.add_argument("--to", dest="to_agent", default="broadcast")
    s.add_argument("--type", dest="mtype", required=True, choices=MSG_TYPES)
    s.add_argument("--round", dest="round_", type=int)
    s.add_argument("--parent")
    s.add_argument("--thread", dest="thread_id", help="thread to attach to")
    s.add_argument("--artifact")
    s.add_argument("--blob")
    s.add_argument("--role")
    s.add_argument("--idempotency-key", dest="idem")
    s.add_argument("--body")
    s.add_argument("--body-file")

    s = sub.add_parser("poll", help="list pending messages for an agent (peek)")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")

    s = sub.add_parser("claim", help="atomically claim next pending message")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")
    s.add_argument("--lease-min", type=float, default=DEFAULT_LEASE_MIN)
    s.add_argument("--wait", type=float, default=0.0,
                   help="block up to N seconds for a message instead of returning "
                        "immediately (0 = no wait)")
    s.add_argument("--poll-interval", type=float, default=2.0,
                   help="seconds between polls while --wait is in effect")

    s = sub.add_parser("complete", help="atomic: post response + ack claimed message")
    s.add_argument("--project", required=True)
    s.add_argument("--from", dest="from_agent", default=os.environ.get("COLLAB_AGENT"),
                   help="sender id; defaults to $COLLAB_AGENT")
    s.add_argument("--claim-message", required=True)
    s.add_argument("--claim-token", required=True)
    s.add_argument("--type", dest="mtype", default="response", choices=MSG_TYPES)
    s.add_argument("--to", dest="to_agent", default=None,
                   help="override reply-to-sender default routing")
    s.add_argument("--round", dest="round_", type=int)
    s.add_argument("--parent")
    s.add_argument("--thread", dest="thread_id",
                   help="override default (inherit claimed message's thread)")
    s.add_argument("--artifact")
    s.add_argument("--blob")
    s.add_argument("--role", default="reviewer")
    s.add_argument("--idempotency-key", dest="idem")
    s.add_argument("--body")
    s.add_argument("--body-file")

    s = sub.add_parser("ack", help="mark a claimed message done (no reply)")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")
    s.add_argument("--message", required=True)
    s.add_argument("--claim-token", required=True)

    s = sub.add_parser("extend", help="extend a lease (heartbeat)")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")
    s.add_argument("--message", required=True)
    s.add_argument("--claim-token", required=True)
    s.add_argument("--lease-min", type=float, default=DEFAULT_LEASE_MIN)

    s = sub.add_parser("heartbeat", help="update participant liveness")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")

    s = sub.add_parser("artifact", help="put/get a versioned work product")
    asub = s.add_subparsers(dest="acmd", required=True)
    ap = asub.add_parser("put")
    ap.add_argument("--project", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--by", required=True)
    ag = asub.add_parser("get")
    ag.add_argument("--project", required=True)
    ag.add_argument("--name", required=True)
    ag.add_argument("--version", type=int)
    ag.add_argument("--out", help="write content to this path instead of stdout")

    s = sub.add_parser("decide", help="post binding decision, converge project")
    s.add_argument("--project", required=True)
    s.add_argument("--from", dest="from_agent", default=os.environ.get("COLLAB_AGENT"),
                   help="sender id; defaults to $COLLAB_AGENT")
    s.add_argument("--thread", dest="thread_id", help="thread this decision closes")
    s.add_argument("--parent", help="message this decision answers (sets thread)")
    s.add_argument("--idempotency-key", dest="idem")
    s.add_argument("--body")
    s.add_argument("--body-file")

    s = sub.add_parser("state", help="get or set project state")
    s.add_argument("--project", required=True)
    s.add_argument("--set", dest="set_state")

    s = sub.add_parser("log", help="print message log")
    s.add_argument("--project", required=True)
    s.add_argument("--since", type=int, default=0)
    s.add_argument("--follow", action="store_true", help="tail new messages live")
    s.add_argument("--interval", type=float, default=2.0,
                   help="poll seconds when --follow (default 2)")

    s = sub.add_parser("projects",
                       help="list all projects under this COLLAB_ROOT")

    s = sub.add_parser("delete",
                       help="delete a project (messages/inbox/artifacts; blobs kept)")
    s.add_argument("--project", required=True)
    s.add_argument("--yes", action="store_true",
                   help="required: confirm permanent deletion")

    s = sub.add_parser("status", help="project summary")
    s.add_argument("--project", required=True)

    s = sub.add_parser(
        "review",
        help="one step: create project (if needed) + snapshot a file + broadcast review")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")
    s.add_argument("--file", required=True, help="the work product to review (required)")
    s.add_argument("--name", help="artifact name (default: the file's basename)")
    s.add_argument("--topic", default="")
    s.add_argument("--goal", default="")
    s.add_argument("--focus", help="what reviewers should focus on")
    s.add_argument("--round", dest="round_", type=int, default=1)

    s = sub.add_parser("doctor",
                       help="diagnose setup + identity and suggest the next step")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")

    s = sub.add_parser("sweep", help="return expired leases to pending")
    s.add_argument("--project", required=True)

    s = sub.add_parser(
        "watch",
        help="hands-off reviewer: poll, claim, invoke the agent, post its reply")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")
    s.add_argument("--poll-interval", type=float, default=2.0)
    s.add_argument("--lease-min", type=float, default=DEFAULT_LEASE_MIN)
    s.add_argument("--agent-timeout", type=float, default=600.0,
                   help="kill the agent if it runs longer than this (seconds)")
    s.add_argument("--max-deliveries", type=int, default=5,
                   help="mark a message 'stalled' after this many failed attempts")
    s.add_argument("--reply-type", default="response", choices=MSG_TYPES)
    s.add_argument("--once", action="store_true", help="process one item then exit")
    s.add_argument("--idle-exit", action="store_true",
                   help="exit when the queue is empty instead of waiting")
    s.add_argument("--max", dest="max_items", type=int,
                   help="exit after N processed items")
    s.add_argument(
        "--exec", dest="exec_argv", nargs=argparse.REMAINDER, required=True,
        help="agent command + args (everything after --exec). "
             "The claimed message is fed on STDIN, never interpolated. "
             "e.g. --exec codex exec")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    # Resolve agent identity: an explicit flag wins, else $COLLAB_AGENT (set by the
    # argparse defaults). Fail clearly if a command needs an identity and none resolved.
    # doctor intentionally omitted: it must run even when identity is missing, since
    # diagnosing a missing/duplicate COLLAB_AGENT is one of its jobs.
    NEED_AGENT = {"start", "join", "poll", "claim", "ack", "extend", "heartbeat",
                  "watch", "review"}
    NEED_FROM = {"post", "complete", "decide"}
    if args.cmd in NEED_AGENT and not getattr(args, "agent", None):
        print(json.dumps({"error": "no agent identity: pass --agent or set "
                          "COLLAB_AGENT (e.g. claude-1 for Claude, codex-1 for Codex)"}),
              file=sys.stderr)
        return 1
    if args.cmd in NEED_FROM and not getattr(args, "from_agent", None):
        print(json.dumps({"error": "no sender identity: pass --from or set "
                          "COLLAB_AGENT"}), file=sys.stderr)
        return 1
    try:
        store = Store(args.root)
    except CollabError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    try:
        cmd = args.cmd
        if cmd == "start":
            _emit(store.start(args.project, args.topic, args.goal, args.agent,
                              max_rounds=args.max_rounds))
        elif cmd == "join":
            _emit(store.join(args.project, args.agent, args.role))
        elif cmd == "post":
            _emit(store.post(args.project, args.from_agent, args.to_agent, args.mtype,
                             _read_body(args), round_=args.round_, parent=args.parent,
                             thread_id=args.thread_id, refs=_refs(args),
                             idempotency_key=args.idem, role=args.role))
        elif cmd == "poll":
            _emit(store.poll(args.project, args.agent))
        elif cmd == "claim":
            res = store.claim(args.project, args.agent, args.lease_min,
                              wait=args.wait, poll_interval=args.poll_interval)
            _emit(res if res is not None else {"claimed": None})
        elif cmd == "complete":
            _emit(store.complete(args.project, args.from_agent, args.claim_message,
                                 args.claim_token, args.mtype, _read_body(args),
                                 to_agent=args.to_agent, round_=args.round_,
                                 parent=args.parent, thread_id=args.thread_id,
                                 refs=_refs(args), idempotency_key=args.idem,
                                 role=args.role))
        elif cmd == "ack":
            _emit(store.ack(args.project, args.agent, args.message, args.claim_token))
        elif cmd == "extend":
            _emit(store.extend(args.project, args.agent, args.message,
                               args.claim_token, args.lease_min))
        elif cmd == "heartbeat":
            store.heartbeat(args.project, args.agent)
            _emit({"heartbeat": args.agent})
        elif cmd == "artifact":
            if args.acmd == "put":
                with open(args.file, "rb") as fh:
                    data = fh.read()
                _emit(store.put_artifact(args.project, args.name, data, args.by))
            else:
                row, data = store.get_artifact(args.project, args.name, args.version)
                if args.out:
                    with open(args.out, "wb") as fh:
                        fh.write(data)
                    _emit({"artifact": f"{row['name']}@v{row['version']}",
                           "sha256": row["sha256"], "out": args.out})
                else:
                    sys.stdout.write(data.decode("utf-8", errors="replace"))
        elif cmd == "decide":
            _emit(store.decide(args.project, args.from_agent, _read_body(args),
                               thread_id=args.thread_id, parent=args.parent,
                               idempotency_key=args.idem))
        elif cmd == "state":
            if args.set_state:
                store.set_state(args.project, args.set_state)
                _emit({"project": args.project, "state": args.set_state})
            else:
                _emit({"project": args.project, "state": store.get_project(args.project)["state"]})
        elif cmd == "log":
            if args.follow:
                store.get_project(args.project)  # fail fast on bad project
                since = args.since
                try:
                    while True:
                        for r in store.log(args.project, since):
                            body = (r["body"] or "").replace("\n", " ")
                            if len(body) > 70:
                                body = body[:67] + "..."
                            print(f"#{r['seq']:<4} {r['created_at'][11:19]} "
                                  f"{r['type']:<14} {r['from_agent']}->{r['to_agent']} "
                                  f"[thr {(r['thread_id'] or '')[:8]}] {body}",
                                  flush=True)
                            since = max(since, r["seq"])
                        time.sleep(args.interval)
                except KeyboardInterrupt:
                    pass
            else:
                _emit(store.log(args.project, args.since))
        elif cmd == "projects":
            _emit(store.list_projects())
        elif cmd == "delete":
            if not args.yes:
                raise CollabError(
                    f"refusing to delete '{args.project}' without --yes (this is "
                    "permanent: removes its messages, inbox, and artifacts)")
            _emit(store.delete_project(args.project))
        elif cmd == "status":
            _emit(store.status(args.project))
        elif cmd == "review":
            _emit(store.review(args.project, args.agent, args.file, name=args.name,
                               topic=args.topic, goal=args.goal, focus=args.focus,
                               round_=args.round_))
        elif cmd == "doctor":
            _emit(store.doctor(args.project, args.agent))
        elif cmd == "sweep":
            _emit({"reset": store.sweep(args.project)})
        elif cmd == "watch":
            if not args.exec_argv:
                raise CollabError("--exec requires an agent command, e.g. --exec codex exec")
            n = watch(store, args.project, args.agent, args.exec_argv,
                      poll_interval=args.poll_interval, once=args.once,
                      idle_exit=args.idle_exit, max_items=args.max_items,
                      lease_min=args.lease_min, reply_type=args.reply_type,
                      agent_timeout=args.agent_timeout,
                      max_deliveries=args.max_deliveries)
            _emit({"processed": n})
        else:
            raise CollabError(f"unknown command: {cmd}")
        return 0
    except CollabError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())

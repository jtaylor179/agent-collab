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
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone

DEFAULT_LEASE_MIN = 10
ROLES = ("initiator", "reviewer", "approver", "observer", "worker", "orchestrator")
# Roles that receive broadcast fan-out and late-join backfill of REVIEW-type work. An
# approver is a reviewer whose sign-off additionally gates decide() and is the ONLY role
# that may accept a task (post `approval`); an observer gets neither inbox work nor a vote.
FANOUT_ROLES = ("reviewer", "approver")
# Roles that receive broadcast fan-out of TASK-type work (the interchangeable worker pool
# that pulls from a plan's shared queue). An orchestrator owns the plan: it posts tasks
# and is the sole caller of the plan's convergence decide(); it is not itself a worker.
WORKER_ROLES = ("worker",)
MSG_TYPES = (
    "question", "review_request", "task", "response", "rebuttal",
    "proposal", "approval", "decision", "status", "heartbeat",
)
# Only these create work-queue (inbox) rows requiring a claim+complete/ack.
# approval/decision/status/heartbeat are log-only notifications consumed by reading
# the log (an approval records sign-off; it demands no work from anyone else).
# `task` is a unit of work to DO (claimed by a worker); the review types are work to REVIEW.
ACTIONABLE = ("question", "review_request", "task", "response", "rebuttal", "proposal")

# Acceptance policy for an orchestrated plan — when is a `task` "accepted"?
#   any        -> any one trusted reviewer (approver) approving accepts it (default)
#   all        -> every approver must approve it
#   final:<id> -> only approver <id>'s approval accepts it (a designated final reviewer)
def _valid_policy(policy):
    if policy in ("any", "all"):
        return policy
    if isinstance(policy, str) and policy.startswith("final:") and policy[6:].strip():
        return policy
    raise CollabError(
        f"invalid accept-policy '{policy}': use 'any', 'all', or 'final:<agent-id>'.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  name TEXT PRIMARY KEY, topic TEXT, goal TEXT,
  state TEXT NOT NULL DEFAULT 'open',
  next_seq INTEGER NOT NULL DEFAULT 1,
  max_rounds INTEGER DEFAULT 6,
  accept_policy TEXT DEFAULT 'any',
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


# Presence thresholds (seconds). An agent only drains its inbox while its process is
# actively polling, so a stale last_heartbeat means it won't see a directed message
# until it re-attaches or runs `watch`. Surfacing this turns "is the other agent
# online?" from guesswork into a field.
PRESENCE_ONLINE_S = 120     # heartbeat within 2 min -> actively attached
PRESENCE_IDLE_S = 1800      # within 30 min -> recently seen, may not be polling now


def _age_seconds(ts):
    """Age in seconds of an ISO-8601 'now_iso()' timestamp, or None if unparseable."""
    if not ts:
        return None
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())


def presence(last_heartbeat):
    """Classify an agent's liveness from its last_heartbeat:
    'online' (<2 min), 'idle' (<30 min), 'offline' (older), or 'unknown' (no/bad ts)."""
    age = _age_seconds(last_heartbeat)
    if age is None:
        return "unknown", None
    if age <= PRESENCE_ONLINE_S:
        return "online", round(age, 1)
    if age <= PRESENCE_IDLE_S:
        return "idle", round(age, 1)
    return "offline", round(age, 1)


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
            self._migrate()
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

    def _migrate(self):
        """Idempotent, additive schema migrations for DBs created before a column
        existed (CREATE TABLE IF NOT EXISTS won't add columns to an existing table).
        Safe to run on every open."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(projects)")}
        if "accept_policy" not in cols:
            self.conn.execute(
                "ALTER TABLE projects ADD COLUMN accept_policy TEXT DEFAULT 'any'")

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

    def start(self, project, topic, goal, agent, role="initiator", max_rounds=6,
              accept_policy="any"):
        accept_policy = _valid_policy(accept_policy)
        ts = now_iso()
        with self.write_tx():
            exists = self.conn.execute(
                "SELECT 1 FROM projects WHERE name=?", (project,)
            ).fetchone()
            if exists:
                raise CollabError(f"project already exists: {project}")
            self.conn.execute(
                "INSERT INTO projects(name,topic,goal,state,next_seq,max_rounds,"
                "accept_policy,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (project, topic, goal, "gathering", 1, max_rounds, accept_policy, ts, ts),
            )
            self.conn.execute(
                "INSERT INTO participants(project,agent_id,role,last_heartbeat) "
                "VALUES(?,?,?,?)",
                (project, agent, role, ts),
            )
        return {"project": project, "state": "gathering", "initiator": agent,
                "accept_policy": accept_policy}

    def set_accept_policy(self, project, policy):
        """Change who the trusted-reviewer acceptance requires: any | all | final:<id>.
        Governs when a task is `accepted` and thus when the plan can converge."""
        self.get_project(project)
        policy = _valid_policy(policy)
        with self.write_tx():
            self.conn.execute(
                "UPDATE projects SET accept_policy=?, updated_at=? WHERE name=?",
                (policy, now_iso(), project))
        return {"project": project, "accept_policy": policy}

    def join(self, project, agent, role="reviewer"):
        """Register a participant. A reviewer joining AFTER a broadcast still needs
        the work: backfill pending inbox rows for every open broadcast they missed
        (actionable, not from them, in a thread not yet closed by a decision).
        Without this, a `start -> broadcast -> join` flow silently drops the review.
        """
        p = self.get_project(project)
        ts = now_iso()
        backfilled = 0
        existing = self.conn.execute(
            "SELECT role FROM participants WHERE project=? AND agent_id=?",
            (project, agent),
        ).fetchone()
        # HARD STOP on the #1 setup mistake: a reviewer "joining" under the id that is
        # already the project's initiator means two different tools share one agent id
        # (e.g. both defaulted to claude-1). Nothing would route — refuse loudly instead
        # of registering a self-collision that looks fine until wait/claim is always empty.
        if existing and existing["role"] == "initiator" and role in FANOUT_ROLES:
            raise CollabError(
                f"'{agent}' is already the INITIATOR of '{project}', so it cannot also "
                f"join as a {role}. This almost always means two tools are using the "
                f"SAME agent id ('{agent}'). Give the reviewer a DISTINCT id: set "
                "COLLAB_AGENT (claude-1 for Claude, codex-1 for Codex, copilot-1 for "
                "Copilot, cursor-1 for Cursor, antigravity-1 for Antigravity) in the reviewer's environment, then join again. If you ARE the "
                "initiator, you don't need to join — just check the project.")
        effective_role = existing["role"] if existing else role
        with self.write_tx():
            self.conn.execute(
                "INSERT INTO participants(project,agent_id,role,last_heartbeat) "
                "VALUES(?,?,?,?) ON CONFLICT(project,agent_id) DO UPDATE SET "
                "last_heartbeat=excluded.last_heartbeat",
                (project, agent, effective_role, ts),
            )
            # Backfill the open broadcasts this role is entitled to: workers get `task`s,
            # reviewers/approvers get the review types. Fanout is by type (see
            # _recipients), so backfill must mirror it or a late joiner misses its work.
            backfill_types = (("task",) if effective_role in WORKER_ROLES
                              else tuple(t for t in ACTIONABLE if t != "task")
                              if effective_role in FANOUT_ROLES else ())
            if backfill_types and p["state"] != "converged":
                ph = ",".join("?" * len(backfill_types))
                rows = self.conn.execute(
                    f"SELECT m.message_id FROM messages m WHERE m.project=? "
                    f"AND m.to_agent='broadcast' AND m.type IN ({ph}) "
                    f"AND m.from_agent != ? "
                    f"AND m.thread_id NOT IN ("
                    f"  SELECT thread_id FROM messages WHERE project=? AND type='decision') "
                    f"AND NOT EXISTS (SELECT 1 FROM inbox i "
                    f"  WHERE i.message_id=m.message_id AND i.recipient=?)",
                    (project, *backfill_types, agent, project, agent),
                ).fetchall()
                for r in rows:
                    self.conn.execute(
                        "INSERT INTO inbox(message_id,recipient,status,deliveries) "
                        "VALUES(?,?,'pending',0)",
                        (r["message_id"], agent),
                    )
                    backfilled += 1
        return {"project": project, "agent": agent, "role": effective_role,
                "backfilled": backfilled}

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

    def _heartbeat_of(self, project, agent):
        row = self.conn.execute(
            "SELECT last_heartbeat FROM participants WHERE project=? AND agent_id=?",
            (project, agent),
        ).fetchone()
        return row["last_heartbeat"] if row else None

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
    def _recipients(self, project, from_agent, to_agent, mtype=None):
        if to_agent and to_agent != "broadcast":
            return [to_agent]
        # Broadcast fan-out is BY TYPE: a `task` goes to the interchangeable worker pool
        # (any worker can pick it up); every other actionable type goes to reviewers/
        # approvers. Observers get neither. The sender never receives its own broadcast.
        target_roles = WORKER_ROLES if mtype == "task" else FANOUT_ROLES
        rows = self.participants(project)
        return [r["agent_id"] for r in rows
                if r["role"] in target_roles and r["agent_id"] != from_agent]

    def _role_of(self, project, agent):
        row = self.conn.execute(
            "SELECT role FROM participants WHERE project=? AND agent_id=?",
            (project, agent),
        ).fetchone()
        return row["role"] if row else None

    def _assert_can_approve(self, project, from_agent):
        """The trusted-reviewer gate (FR2): only an `approver` may accept work by posting
        an `approval`. A worker, orchestrator, plain reviewer, or non-participant cannot —
        so an agent can never certify its own (or anyone's) task. Enforced in the bus, not
        by orchestrator convention, because 'who is trusted to accept' is a trust boundary."""
        r = self._role_of(project, from_agent)
        if r != "approver":
            raise CollabError(
                f"'{from_agent}' (role: {r or 'not a participant'}) may not post an "
                "'approval': only an approver (a trusted reviewer) can accept work. "
                "Join the trusted reviewer as `--role approver`.")

    def _insert_message(self, project, from_agent, to_agent, mtype, body,
                        thread_id=None, round_=None, parent=None,
                        refs=None, idempotency_key=None, role=None):
        """Insert a message + its inbox rows. MUST be called inside write_tx().
        Returns (message_id, was_duplicate)."""
        if mtype == "approval":
            self._assert_can_approve(project, from_agent)
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
            for rcpt in self._recipients(project, from_agent, to_agent, mtype):
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
        # Activity IS presence: posting means the sender is actively attached, so its
        # last_heartbeat tracks real liveness (without this, an agent that posts all day
        # but never calls `heartbeat` reads as "offline").
        self.heartbeat(project, from_agent)
        with self.write_tx():
            mid, dup = self._insert_message(
                project, from_agent, to_agent, mtype, body, **kw)
        res = {"message_id": mid, "duplicate": dup}
        if to_agent == "broadcast" and mtype in ACTIONABLE and not dup:
            if not self._recipients(project, from_agent, "broadcast", mtype):
                who = "workers" if mtype == "task" else "reviewers"
                res["warning"] = (
                    f"no {who} have joined yet — this broadcast has zero recipients "
                    f"right now. {who.capitalize()} who join later are backfilled "
                    "automatically.")
        elif to_agent not in ("broadcast", from_agent) and not dup:
            # Directed message footguns: (1) a log-only type creates no inbox row, so the
            # recipient is never prompted; (2) an actionable message to an agent that is
            # not actively attached will sit undelivered until it returns.
            if mtype not in ACTIONABLE:
                res["warning"] = (
                    f"'{mtype}' is a log-only type: it does NOT create an inbox row, so "
                    f"{to_agent} will not be prompted to act on it. Use review_request / "
                    f"question / response (with --parent) for anything that needs a reply.")
            else:
                pres, age = presence(self._heartbeat_of(project, to_agent))
                if pres in ("idle", "offline"):
                    res["warning"] = (
                        f"{to_agent} looks {pres} (last seen ~{int((age or 0) // 60)} min "
                        f"ago) — it will not see this until it re-attaches or runs "
                        f"`collab watch`. The message is queued durably and delivers when "
                        f"it returns.")
                    res["recipient_presence"] = pres
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
        """List pending inbox rows for agent (peek, no claim). Checking in counts as
        activity, so this refreshes the caller's presence heartbeat."""
        self.heartbeat(project, agent)
        return self.conn.execute(
            "SELECT i.*, m.seq, m.type, m.from_agent, m.round, m.parent_message_id "
            "FROM inbox i JOIN messages m USING(message_id) "
            "WHERE i.recipient=? AND m.project=? AND i.status='pending' "
            "ORDER BY m.seq",
            (agent, project),
        ).fetchall()

    def inbox_view(self, project, agent):
        """A human/agent-friendly inbox: pending actionable messages addressed to me,
        each with sender, type, parent thread, and a one-line summary. Unlike `poll`
        (a raw peek), this is shaped for a per-turn 'what needs my attention?' read."""
        self.get_project(project)
        self.heartbeat(project, agent)  # checking inbox = active presence
        rows = self.conn.execute(
            "SELECT m.seq, m.message_id, m.type, m.from_agent, m.round, "
            "m.parent_message_id, m.body, m.created_at "
            "FROM inbox i JOIN messages m USING(message_id) "
            "WHERE i.recipient=? AND m.project=? AND i.status='pending' ORDER BY m.seq",
            (agent, project),
        ).fetchall()
        items = []
        for r in rows:
            body = (r["body"] or "").replace("\n", " ")
            items.append({
                "seq": r["seq"], "message_id": r["message_id"], "type": r["type"],
                "from": r["from_agent"], "round": r["round"],
                "parent": r["parent_message_id"],
                "summary": body[:140] + ("..." if len(body) > 140 else ""),
            })
        return {"agent": agent, "pending": len(items), "items": items}

    def inbox_drain(self, project, agent, limit=10000):
        """Mark every pending inbox row done WITHOUT replying — claim+ack each in turn.
        Use to clear a backlog of messages you've already handled out-of-band so the
        'pending' count reflects reality. (A plain `ack` needs a claim_token; this does
        the claim for you.)"""
        self.get_project(project)
        self.heartbeat(project, agent)
        drained = []
        for _ in range(limit):
            got = self._claim_once(project, agent)
            if not got:
                break
            self.ack(project, agent, got["message_id"], got["claim_token"])
            drained.append(got["message_id"])
        return {"drained": len(drained), "message_ids": drained}

    def _restore_task_pool(self, project):
        """Re-open a task to the whole worker pool once it falls back to unclaimed.

        A claimed task preempts its sibling rows (work-stealing). If the winning worker
        dies and the claim is swept/reclaimed back to pending, those siblings must return
        to pending too — otherwise only the original (dead) worker could re-claim it and
        the task would strand. Restore siblings ONLY for a task with no active 'claimed'
        and no 'done' row (i.e. abandoned, not in-progress and not already submitted).
        MUST be called inside a write_tx()."""
        self.conn.execute(
            "UPDATE inbox SET status='pending', claimed_by=NULL, claim_token=NULL, "
            "leased_until=NULL WHERE status='preempted' "
            "AND message_id IN (SELECT message_id FROM messages "
            "                   WHERE project=? AND type='task') "
            "AND message_id NOT IN (SELECT message_id FROM inbox "
            "                       WHERE status IN ('claimed','done'))",
            (project,),
        )

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
            self._restore_task_pool(project)
            return cur.rowcount

    def in_flight(self, project, agent=None):
        """List claimed (in-flight) inbox rows for the project — work a watcher has
        picked up but not yet completed. An orphaned row (lease already expired) means
        the owning watcher almost certainly died mid-run: it is invisible to poll/inbox
        (which only show 'pending') and will not move until the next claim triggers a
        sweep. Surfacing it here is how a human sees an abandoned review instead of a
        misleading empty 'pending'."""
        now = now_iso()
        rows = self.conn.execute(
            "SELECT i.recipient, i.message_id, m.type, i.deliveries, i.leased_until, "
            "m.thread_id FROM inbox i JOIN messages m USING(message_id) "
            "WHERE m.project=? AND i.status='claimed'"
            + (" AND i.recipient=?" if agent else "")
            + " ORDER BY m.seq",
            (project, agent) if agent else (project,),
        ).fetchall()
        return [
            {"recipient": r["recipient"], "message_id": r["message_id"],
             "type": r["type"], "deliveries": r["deliveries"],
             "leased_until": r["leased_until"], "thread_id": r["thread_id"],
             # a NULL lease can't expire; only a set-and-past lease is orphaned
             "orphaned": bool(r["leased_until"] and r["leased_until"] < now)}
            for r in rows
        ]

    def reclaim(self, project, message_id=None, agent=None, force=False):
        """Human-facing recovery for a dead watcher's stranded claims: return claimed
        inbox rows to pending so a fresh watcher can pick them up immediately, without
        waiting out the remaining lease.

        Default (no --force) reclaims only EXPIRED leases — same safety as the inline
        sweeper, but reportable and scopeable. --force reclaims even a still-live lease
        (use when you KNOW the watcher is dead and don't want to wait). Scope with
        --message (one row) or --agent (one recipient's rows).

        Safe against a prior owner that turns out to be alive: reclaim mints no token,
        it just nulls the row's claim_token, so the old worker's later complete()/ack()
        is fenced out by _verify_lease (token mismatch) and skipped, exactly as with a
        normal sweep."""
        self.get_project(project)
        where = ["status='claimed'",
                 "message_id IN (SELECT message_id FROM messages WHERE project=?)"]
        params = [project]
        if message_id:
            where.append("message_id=?")
            params.append(message_id)
        if agent:
            where.append("claimed_by=?")
            params.append(agent)
        if not force:
            where.append("leased_until IS NOT NULL AND leased_until < ?")
            params.append(now_iso())
        with self.write_tx():
            targets = [
                r["message_id"] for r in self.conn.execute(
                    "SELECT message_id FROM inbox WHERE " + " AND ".join(where),
                    params,
                ).fetchall()
            ]
            if targets:
                ph = ",".join("?" * len(targets))
                self.conn.execute(
                    "UPDATE inbox SET status='pending', claimed_by=NULL, "
                    f"claim_token=NULL, leased_until=NULL WHERE message_id IN ({ph})",
                    targets,
                )
                # a reclaimed task must reopen to the whole worker pool, not just its
                # (dead) original claimer — restore the preempted sibling rows
                self._restore_task_pool(project)
        return {"reclaimed": len(targets), "message_ids": targets, "forced": force}

    def claim(self, project, agent, lease_min=DEFAULT_LEASE_MIN, wait=0.0,
              poll_interval=2.0):
        """Claim the next pending inbox row for agent. If wait>0, block up to `wait`
        seconds (polling every poll_interval) until something is claimable, then return
        it; return None on timeout. wait=0 is the original non-blocking behavior."""
        self.heartbeat(project, agent)  # claiming = active presence
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
            # Work-stealing: a `task` fans out one row per worker, but only ONE worker
            # should do it. The first claimer preempts the still-pending sibling rows so
            # no one else picks it up. If this claim is later swept/reclaimed back to
            # pending (worker died), the siblings are RESTORED (see _restore_task_pool) so
            # any interchangeable worker can re-steal it — not just the original claimer.
            self.conn.execute(
                "UPDATE inbox SET status='preempted', claim_token=NULL, leased_until=NULL "
                "WHERE message_id=? AND recipient!=? AND status='pending' "
                "AND message_id IN (SELECT message_id FROM messages WHERE type='task')",
                (cand["message_id"], agent),
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

    def _approval_status(self, project):
        """Map each approver participant -> whether they have posted an approval."""
        approvers = [r["agent_id"]
                     for r in self.participants(project, role="approver")]
        if not approvers:
            return {}
        rows = self.conn.execute(
            "SELECT DISTINCT from_agent FROM messages "
            "WHERE project=? AND type='approval'", (project,),
        ).fetchall()
        approved = {r["from_agent"] for r in rows}
        return {a: (a in approved) for a in approvers}

    def _task_rollup(self, project):
        """Per-task state for an orchestrated plan. A task moves todo -> claimed (a worker
        holds it) -> submitted (worker posted its result) -> accepted (trusted reviewer(s)
        approved it, per the project's accept_policy). Only an approver can post an
        `approval` (see _assert_can_approve). This is the roll-up decide() converges on.

        accept_policy decides what 'accepted' requires:
          any        -> >=1 approver approved the task's thread
          all        -> every approver approved it
          final:<id> -> approver <id> approved it
        """
        p = self.get_project(project)
        policy = (p["accept_policy"] if "accept_policy" in p.keys() else None) or "any"
        approvers = {r["agent_id"] for r in self.participants(project, role="approver")}
        tasks = self.conn.execute(
            "SELECT message_id, thread_id, body, seq FROM messages "
            "WHERE project=? AND type='task' ORDER BY seq", (project,),
        ).fetchall()
        out = []
        for t in tasks:
            mid = t["message_id"]
            doer = self.conn.execute(
                "SELECT claimed_by, status FROM inbox WHERE message_id=? "
                "AND status IN ('claimed','done') LIMIT 1", (mid,),
            ).fetchone()
            approved_by = sorted(
                r["from_agent"] for r in self.conn.execute(
                    "SELECT DISTINCT from_agent FROM messages WHERE project=? "
                    "AND type='approval' AND thread_id=?", (project, t["thread_id"]),
                ).fetchall())
            if policy == "all":
                accepted = bool(approvers) and approvers.issubset(set(approved_by))
            elif policy.startswith("final:"):
                accepted = policy[6:].strip() in approved_by
            else:  # any
                accepted = len(approved_by) > 0
            if accepted:
                state = "accepted"
            elif doer and doer["status"] == "done":
                state = "submitted"
            elif doer and doer["status"] == "claimed":
                state = "claimed"
            else:
                state = "todo"
            if not accepted:
                accepted_by = None
            elif policy.startswith("final:"):
                accepted_by = policy[6:].strip()          # the designated final reviewer
            else:
                accepted_by = ",".join(approved_by)       # any/all: who signed off
            title = (t["body"] or "").strip().splitlines()[0][:60] if t["body"] else ""
            out.append({"task": mid, "seq": t["seq"], "title": title, "state": state,
                        "worker": doer["claimed_by"] if doer else None,
                        "approved_by": approved_by, "accepted_by": accepted_by})
        return out

    def decide(self, project, from_agent, body, thread_id=None, parent=None,
               idempotency_key=None, force=False):
        """Initiator/orchestrator posts the binding decision and converges the project.

        Pass --thread/--parent to attach the decision to the thread it closes so
        the log threads cleanly; the project-level state also flips to converged.

        Convergence gate, overridable with force=True (recorded in output):
        - Orchestrated plan (has `task`s): every task must be `accepted` per the project's
          accept_policy (any | all | final:<id>). The policy — not a blanket all-approvers
          rule — is the authority for who must sign off, so the approver gate is skipped.
        - Plain review project (no tasks): if it has approver participants, every one must
          have posted an `approval` first.
        """
        tasks = self._task_rollup(project)
        approvals = self._approval_status(project)
        missing = sorted(a for a, ok in approvals.items() if not ok)
        if tasks:
            unaccepted = [t for t in tasks if t["state"] != "accepted"]
            if unaccepted and not force:
                policy = self.get_project(project)["accept_policy"] or "any"
                detail = ", ".join(t["task"][:8] + "=" + t["state"] for t in unaccepted)
                raise CollabError(
                    f"decide blocked: {len(unaccepted)} of {len(tasks)} task(s) not yet "
                    f"accepted under policy '{policy}' ({detail}). A task is accepted when "
                    "the required approver(s) post an `approval` in its thread. Pass "
                    "--force to converge anyway.")
        else:
            unaccepted = []
            if missing and not force:
                raise CollabError(
                    "decide blocked: approver(s) have not signed off yet: "
                    f"{', '.join(missing)}. Each approver must post an 'approval' "
                    "message (`post --type approval`, or `complete --type approval` "
                    "when draining their inbox). Pass --force to converge anyway.")
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
        out = {"message_id": mid, "duplicate": dup, "state": "converged",
               "closed_deliveries": cur.rowcount}
        if approvals:
            out["approvals"] = approvals
            if missing:
                out["forced_over_missing_approvals"] = missing
        if tasks:
            out["tasks_total"] = len(tasks)
            if unaccepted:
                out["forced_over_unaccepted_tasks"] = [t["task"] for t in unaccepted]
        return out

    def log(self, project, since_seq=0, actionable_only=False, to_agent=None,
            from_agent=None):
        q = "SELECT * FROM messages WHERE project=? AND seq>?"
        params = [project, since_seq]
        if actionable_only:
            q += " AND type IN (%s)" % ",".join("?" * len(ACTIONABLE))
            params += list(ACTIONABLE)
        if to_agent:
            q += " AND to_agent=?"
            params.append(to_agent)
        if from_agent:
            q += " AND from_agent=?"
            params.append(from_agent)
        q += " ORDER BY seq"
        return self.conn.execute(q, params).fetchall()

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
                dict(zip(
                    ("agent", "role", "last_heartbeat", "presence", "last_seen_age_s"),
                    (r["agent_id"], r["role"], r["last_heartbeat"],
                     *presence(r["last_heartbeat"])),
                )) for r in parts
            ],
            "pending": {r["recipient"]: r["n"] for r in pending},
            # claimed-but-not-completed rows. An 'orphaned' entry (lease expired) is a
            # review a watcher picked up then abandoned — it is NOT in 'pending', so
            # without this it would be invisible (an empty 'pending' hiding real work).
            "in_flight": self.in_flight(project),
            # per-task roll-up for orchestrated plans (empty when the project has no tasks)
            "tasks": self._task_rollup(project),
            "accept_policy": (p["accept_policy"] if "accept_policy" in p.keys()
                              else "any") or "any",
            "stalled": [
                {"recipient": r["recipient"], "message_id": r["message_id"],
                 "type": r["type"], "deliveries": r["deliveries"]} for r in stalled
            ],
            "open_threads": [r["thread_id"] for r in open_threads],
            # {} when the project has no approvers; otherwise agent -> approved?
            "approvals": self._approval_status(project),
        }

    def next_action(self, project, agent):
        """The deterministic 'what should I do next?' signal for a self-paced loop.

        `status` dumps raw thread IDs — a loop tick can't act on that, so a human ends
        up interpreting it and re-kicking the loop by hand ('had to run /loop continue to
        fish all the steps'). This collapses the whole board into ONE recommended action
        for `agent`, so a loop can advance a multi-step plan hands-off:

          reclaim  - a review claimed for you was abandoned (dead watcher); recover it
          drain    - you have inbox messages to claim + handle
          decide   - every reviewer has answered your latest review_request; converge
                     (or rebut) — the ball is in your court
          wait     - you're waiting on reviewer(s); nothing for you to do yet
          done     - project converged; advance to the next plan step
          broadcast- you're the initiator but no review_request has gone out yet

        `open_threads` in status is NOT used here: `decide` converges a whole project at
        once, so open-thread count is noisy by design and a poor 'am I blocked' signal.
        The real question — is the ball in my court or a reviewer's — is answered from the
        inbox and per-reviewer response coverage of the latest review round."""
        p = self.get_project(project)
        parts = self.participants(project)
        mine = next((r for r in parts if r["agent_id"] == agent), None)
        role = mine["role"] if mine else None
        reviewers = [r["agent_id"] for r in parts
                     if r["role"] in FANOUT_ROLES and r["agent_id"] != agent]
        out = {
            "project": project, "agent": agent, "your_role": role,
            "state": p["state"], "reviewers": reviewers,
            "pending_for_you": len(self.poll(project, agent)) if agent else 0,
            "orphaned_for_you": [f["message_id"] for f in self.in_flight(project, agent)
                                 if f["orphaned"]],
            "latest_review_request": None, "responded": [], "awaiting": [],
        }

        def result(action, why):
            out["action"] = action
            out["why"] = why
            return out

        if p["state"] == "converged":
            return result("done", "Project converged — advance to the next plan step "
                                  "(start/broadcast the next step's review).")
        if out["orphaned_for_you"]:
            ids = ", ".join(m[:8] for m in out["orphaned_for_you"])
            return result("reclaim", f"{len(out['orphaned_for_you'])} item(s) claimed "
                          f"for you were abandoned (watcher likely died): {ids}. "
                          f"Run reclaim --agent {agent} --force, then continue.")

        # Workers pull from the shared task queue: pending work == a task to do.
        if role in WORKER_ROLES:
            if out["pending_for_you"]:
                return result("do-task", f"{out['pending_for_you']} task(s) available — "
                              "claim one and do it (first claim wins; siblings preempt).")
            return result("wait", "No tasks queued for you right now.")

        # The orchestrator owns the plan: drive tasks to acceptance, then converge.
        if role == "orchestrator":
            tasks = self._task_rollup(project)
            by_state = {}
            for t in tasks:
                by_state[t["state"]] = by_state.get(t["state"], 0) + 1
            out["tasks"] = {"total": len(tasks), "by_state": by_state}
            if not tasks:
                return result("broadcast", "No tasks posted yet — post the plan's "
                              "task(s) to put the workers to work.")
            orphan_tasks = [f["message_id"] for f in self.in_flight(project)
                            if f["orphaned"] and f["type"] == "task"]
            if orphan_tasks:
                return result("reclaim", f"{len(orphan_tasks)} task(s) abandoned by a "
                              "dead worker — `reclaim --force` reopens them to the pool.")
            if not any(t["state"] != "accepted" for t in tasks):
                return result("decide", f"All {len(tasks)} task(s) accepted by trusted "
                              "reviewers — `decide` to converge the plan.")
            summary = ", ".join(f"{n} {s}" for s, n in sorted(by_state.items()))
            return result("wait", f"Plan in progress ({summary}) — workers and reviewers "
                          "still finishing. Wait, then re-check.")

        if out["pending_for_you"]:
            return result("drain", f"{out['pending_for_you']} message(s) in your inbox — "
                          "claim each, handle it, and complete/decide.")

        # Ball-in-court analysis on the initiator's latest review_request round.
        rr = self.conn.execute(
            "SELECT message_id, seq, round FROM messages "
            "WHERE project=? AND from_agent=? AND type='review_request' "
            "ORDER BY seq DESC LIMIT 1",
            (project, agent),
        ).fetchone()
        if rr is None:
            if role in FANOUT_ROLES:
                return result("wait", "No review request addressed to you yet — wait.")
            return result("broadcast", "You're the initiator but no review_request has "
                                       "gone out yet — post/broadcast one to start a round.")
        out["latest_review_request"] = {
            "message_id": rr["message_id"], "seq": rr["seq"], "round": rr["round"]}
        REPLY = ("response", "proposal", "rebuttal", "approval")
        ph = ",".join("?" * len(REPLY))
        for rv in reviewers:
            replied = self.conn.execute(
                f"SELECT 1 FROM messages WHERE project=? AND from_agent=? "
                f"AND type IN ({ph}) AND seq>? LIMIT 1",
                (project, rv, *REPLY, rr["seq"]),
            ).fetchone()
            if replied:
                out["responded"].append(rv)
            else:
                out["awaiting"].append({
                    "agent": rv, "presence": presence(self._heartbeat_of(project, rv))[0]})
        if reviewers and not out["awaiting"]:
            return result("decide", "Every reviewer has answered your latest "
                          "review_request — rebut open points or `decide` to converge "
                          "and advance the plan.")
        waiting = ", ".join(f"{a['agent']}({a['presence']})" for a in out["awaiting"])
        offline = [a["agent"] for a in out["awaiting"] if a["presence"] != "online"]
        why = f"Waiting on reviewer(s): {waiting or 'none registered'}."
        if offline:
            why += (f" {', '.join(offline)} look offline — a watcher may have died; "
                    "consider (re)launching it, or reclaim if they hold a claim.")
        return result("wait", why)

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
                     "Claude, codex-1 for Codex, copilot-1 for Copilot, cursor-1 for Cursor, "
                     "antigravity-1 for Antigravity) or pass --agent.")
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
        # In-flight rows are claimed but not completed and thus NOT counted in
        # pending_for_you; an orphaned one is a review a dead watcher abandoned.
        out["in_flight_for_you"] = self.in_flight(project, agent) if agent else []
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
        orphaned = [f for f in out["in_flight_for_you"] if f["orphaned"]]
        if orphaned:
            ids = ", ".join(f["message_id"][:8] for f in orphaned)
            h.append(f"{len(orphaned)} review(s) claimed for you but abandoned "
                     f"(lease expired, watcher likely died mid-run): {ids}. These are "
                     "NOT in your pending count. Recover them now with "
                     f"`reclaim --project {project} --agent {agent} --force` (or wait "
                     "for the lease to expire and a new claim to sweep them).")
        if out["pending_for_you"]:
            h.append(f"You have {out['pending_for_you']} item(s) to handle: claim each, "
                     "read the referenced artifact, and respond/complete.")
        elif mine and mine["role"] in FANOUT_ROLES and not orphaned:
            h.append("Nothing pending for you right now.")
        approvals = self._approval_status(project)
        out["approvals"] = approvals
        missing = sorted(a for a, ok in approvals.items() if not ok)
        if missing and out["state"] != "converged":
            h.append("decide is gated: approver(s) have not signed off yet: "
                     f"{', '.join(missing)}. Each must post an 'approval' message "
                     "(post --type approval, or complete --type approval).")
            if agent in missing:
                h.append("You are one of the missing approvers — post your approval "
                         "when you're satisfied, or a response with your objections.")
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
    instructions = (
        f"You are {agent}, an AI reviewer collaborating with other agents over a "
        "shared bus. Read the message and the referenced artifact, then write ONLY "
        "your review to stdout as plain text. Lead with your strongest substantive "
        "objection; if you genuinely agree, say specifically why and name the one "
        "thing you would still change. You are reviewing the GOAL, not just the "
        "artifact as written: if you think the whole approach is wrong, say so "
        "plainly and propose the alternative — challenging the premise is in scope, "
        "not only refining the details. No preamble, no sign-off.")
    me = next((r for r in store.participants(project)
               if r["agent_id"] == agent), None)
    if me and me["role"] == "approver":
        instructions += (
            " ADDITIONALLY: you are an APPROVER on this project — the initiator "
            "cannot converge until you formally sign off. If (and only if) you are "
            "satisfied and formally approve, make the FIRST line of your output "
            "exactly 'APPROVED', then your reasoning. If you have any remaining "
            "objection, do NOT write APPROVED — state the objection instead; you "
            "will be asked again on a later round.")
    return json.dumps({
        "instructions": instructions,
        "message": {k: claimed.get(k) for k in (
            "type", "from_agent", "round", "body", "thread_id", "parent_message_id")},
        "artifact": artifact,
    }, indent=2)


def _signals_approval(text):
    """True iff the first non-empty line of the agent's output is an APPROVED
    marker. Only honored for participants whose role is 'approver'."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return bool(re.match(r"APPROVED\b", line, re.IGNORECASE))
    return False


PAYLOAD_PLACEHOLDER = "{}"


def _bind_payload(exec_argv, payload):
    """Decide how the payload reaches the agent. Default: on STDIN (codex exec, and
    any CLI that reads a piped prompt). But CLIs like GitHub Copilot (`copilot -p
    <text>`) want the prompt as an ARGUMENT — so if any token contains the `{}`
    placeholder, substitute the payload there and send NO stdin. Returns
    (argv, stdin_text_or_None)."""
    if any(PAYLOAD_PLACEHOLDER in a for a in exec_argv):
        argv = [a.replace(PAYLOAD_PLACEHOLDER, payload) for a in exec_argv]
        return argv, None
    return list(exec_argv), payload


def _run_agent_with_heartbeat(store, project, agent, claimed, exec_argv,
                              payload, lease_min, agent_timeout=None):
    """Invoke the agent (argv list), extending the lease in a background thread so a
    long review is not falsely redelivered. The payload reaches the agent on stdin by
    default, or as an argument when exec_argv contains the `{}` placeholder (see
    _bind_payload). Kills the agent if it exceeds agent_timeout. Returns
    (returncode, stdout, stderr)."""
    import subprocess
    import threading

    cm, tok = claimed["claim_message_id"], claimed["claim_token"]
    argv, stdin_text = _bind_payload(exec_argv, payload)
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
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(input=stdin_text, timeout=agent_timeout)
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
        try:
            store.join(project, agent)  # auto-join as reviewer (idempotent)
        except CollabError as e:
            # almost always the same-id-as-initiator collision; surface and stop
            print(f"[watch] cannot start: {e}", file=log_fh, flush=True)
            return 0
    my_role = next((r["role"] for r in store.participants(project)
                    if r["agent_id"] == agent), "reviewer")
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

        # An approver's watcher can sign off hands-off: when the agent's output
        # leads with the APPROVED marker, post it as an `approval` (which is what
        # unblocks decide) instead of a plain response. Reviewers' output is never
        # promoted — the marker only means something from an approver.
        mtype = reply_type
        if my_role == "approver" and _signals_approval(review):
            mtype = "approval"
        try:
            store.complete(project, agent, cm, tok, mtype, review,
                           round_=rnd, parent=cm,
                           idempotency_key=f"{agent}:{mtype}:{cm}:r{rnd}")
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
        verb = "APPROVED" if mtype == "approval" else "responded to"
        print(f"[watch] {agent} {verb} {cm[:8]}", file=log_fh, flush=True)
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
    s.add_argument("--role", default="initiator", choices=ROLES,
                   help="starter's role — use 'orchestrator' to own a multi-worker plan")
    s.add_argument("--accept-policy", default="any",
                   help="task acceptance: any | all | final:<agent-id> (orchestrated plans)")
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
    s.add_argument("--force", action="store_true",
                   help="converge even if approvers have not signed off")
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
    s.add_argument("--since", type=int, default=0,
                   help="only messages with seq > this (replaces hand-rolled filtering)")
    s.add_argument("--actionable", action="store_true",
                   help="only review_request/question/response/rebuttal/proposal")
    s.add_argument("--to", dest="to_agent", help="only messages addressed to this agent")
    s.add_argument("--from", dest="from_agent", help="only messages from this agent")
    s.add_argument("--follow", action="store_true", help="tail new messages live")
    s.add_argument("--interval", type=float, default=2.0,
                   help="poll seconds when --follow (default 2)")

    s = sub.add_parser("inbox",
                       help="show or drain YOUR pending inbox (actionable messages "
                            "addressed to you)")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")
    s.add_argument("--drain", action="store_true",
                   help="claim+ack all pending (mark read) without replying — clears an "
                        "already-handled backlog so 'pending' reflects reality")

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
        "policy", help="show or set a plan's task acceptance policy (any|all|final:<id>)")
    s.add_argument("--project", required=True)
    s.add_argument("--set", dest="set_policy", default=None,
                   help="new policy: any | all | final:<agent-id>")

    s = sub.add_parser(
        "next",
        help="one recommended action for a self-paced loop "
             "(reclaim|drain|decide|wait|done|broadcast)")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=os.environ.get("COLLAB_AGENT"),
                   help="agent id; defaults to $COLLAB_AGENT")

    s = sub.add_parser(
        "reclaim",
        help="recover a dead watcher's stranded claims: return claimed rows to "
             "pending so a fresh watcher can pick them up")
    s.add_argument("--project", required=True)
    s.add_argument("--agent", default=None,
                   help="only reclaim rows claimed by this recipient")
    s.add_argument("--message", default=None,
                   help="only reclaim this specific claimed message_id")
    s.add_argument("--force", action="store_true",
                   help="reclaim even leases that have NOT expired yet (use when you "
                        "know the watcher is dead and won't wait out the lease)")

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
                  "watch", "review", "inbox", "next"}
    NEED_FROM = {"post", "complete", "decide"}
    if args.cmd in NEED_AGENT and not getattr(args, "agent", None):
        print(json.dumps({"error": "no agent identity: pass --agent or set "
                          "COLLAB_AGENT (e.g. claude-1 for Claude, codex-1 for Codex, "
                          "copilot-1 for Copilot, cursor-1 for Cursor, antigravity-1 for Antigravity)"}),
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
                              role=args.role, max_rounds=args.max_rounds,
                              accept_policy=args.accept_policy))
        elif cmd == "join":
            _emit(store.join(args.project, args.agent, args.role))
        elif cmd == "post":
            _emit(store.post(args.project, args.from_agent, args.to_agent, args.mtype,
                             _read_body(args), round_=args.round_, parent=args.parent,
                             thread_id=args.thread_id, refs=_refs(args),
                             idempotency_key=args.idem, role=args.role))
        elif cmd == "poll":
            _emit(store.poll(args.project, args.agent))
        elif cmd == "inbox":
            if args.drain:
                _emit(store.inbox_drain(args.project, args.agent))
            else:
                _emit(store.inbox_view(args.project, args.agent))
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
                               idempotency_key=args.idem, force=args.force))
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
                        for r in store.log(args.project, since,
                                           actionable_only=args.actionable,
                                           to_agent=args.to_agent,
                                           from_agent=args.from_agent):
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
                _emit(store.log(args.project, args.since,
                                actionable_only=args.actionable,
                                to_agent=args.to_agent, from_agent=args.from_agent))
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
        elif cmd == "policy":
            if args.set_policy is not None:
                _emit(store.set_accept_policy(args.project, args.set_policy))
            else:
                pr = store.get_project(args.project)
                _emit({"project": args.project,
                       "accept_policy": (pr["accept_policy"]
                                         if "accept_policy" in pr.keys() else "any") or "any"})
        elif cmd == "next":
            _emit(store.next_action(args.project, args.agent))
        elif cmd == "reclaim":
            _emit(store.reclaim(args.project, message_id=args.message,
                                agent=args.agent, force=args.force))
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

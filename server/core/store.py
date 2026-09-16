"""Transactional candidate store for the single-authority runtime.

No driver is called here. An action is journaled before external dispatch, and
an unresolved dispatch is never made executable again by reopening the DB.
The current prototype Run has not yet been cut over to this store.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from .contracts import ActionEnvelope, Approval, Checkpoint, CompletionVerdict, Evidence, Event, Identifier, ObservationRef, ResourceLease, ResourceScope, Sha256, Task

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "INCONCLUSIVE", "CANCELLED"})
TRANSITIONS = {
    "DRAFT": {"QUEUED"}, "QUEUED": {"PLANNING", "BLOCKED"},
    "PLANNING": {"READY", "WAITING_USER", "BLOCKED", "REPLAN", "PAUSED"},
    "READY": {"RUNNING", "WAITING_APPROVAL", "REOBSERVE", "PAUSED"},
    "RUNNING": {"VERIFYING", "WAITING_APPROVAL", "WAITING_USER", "REOBSERVE", "REPLAN", "RECONCILING", "BLOCKED", "PAUSED"},
    "WAITING_APPROVAL": {"REOBSERVE", "PAUSED"}, "WAITING_USER": {"REOBSERVE", "PAUSED"},
    "PAUSED": {"REOBSERVE"}, "REOBSERVE": {"PLANNING", "READY", "RUNNING", "BLOCKED", "PAUSED"},
    "REPLAN": {"PLANNING", "WAITING_USER", "BLOCKED", "PAUSED"},
    "RECONCILING": {"REOBSERVE", "VERIFYING", "INCONCLUSIVE", "BLOCKED", "PAUSED"},
    "VERIFYING": {"REOBSERVE", "REPLAN", "RECONCILING", "WAITING_USER", "INCONCLUSIVE", "PAUSED"},
    "BLOCKED": {"REOBSERVE", "WAITING_USER"}, "CANCELLING": {"CANCELLED"},
}


class StoreError(RuntimeError):
    pass


class Conflict(StoreError):
    pass


class AdmissionDenied(StoreError):
    pass


class OutcomeUnknown(StoreError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def _json(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _resource_identity(scope: ResourceScope) -> str:
    """Input lock identity ignores narrower authorization scopes/aliases.

    The parent executor registry must supply authentic session/device IDs.
    This normalization does not prove an arbitrary caller owns that executor.
    """
    identity = {"kind": scope.kind, "device": scope.device_id or "local"}
    if scope.kind in {"browser", "desktop", "terminal"}:
        identity["session"] = scope.session_id
    elif scope.kind == "filesystem":
        identity["path"] = scope.path
    elif scope.kind == "model":
        identity["endpoint"] = scope.website_origin
    return _json(identity)


class TaskStore:
    def __init__(self, path: Path, *, clock=utcnow):
        self.path, self._clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fresh = not self.path.exists()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        if fresh:
            self.path.chmod(0o600)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 2, 3, 4):
            self._db.close()
            raise StoreError("Unsupported TaskStore schema version")
        self._db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS policies(id TEXT PRIMARY KEY, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(task_id TEXT NOT NULL REFERENCES tasks(id), sequence INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(task_id,sequence));
            CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY, fence INTEGER NOT NULL, identity TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, resource_id TEXT NOT NULL REFERENCES resources(id), owner TEXT NOT NULL REFERENCES tasks(id), fence INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS lease_resource ON leases(resource_id);
            CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), resource_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS observation_resource ON observations(task_id,resource_id,revision);
            CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), used INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), idempotency_key TEXT NOT NULL, payload_sha256 TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL, outcome TEXT, UNIQUE(task_id,idempotency_key));
            CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), task_revision INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS model_view_retention(task_id TEXT PRIMARY KEY REFERENCES tasks(id), mode TEXT NOT NULL, max_bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS model_tool_results(task_id TEXT NOT NULL REFERENCES tasks(id), tool_name TEXT NOT NULL, digest TEXT NOT NULL, content BLOB NOT NULL, stored_bytes INTEGER NOT NULL, PRIMARY KEY(task_id,tool_name,digest));
            CREATE TABLE IF NOT EXISTS model_request_views(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), task_revision INTEGER NOT NULL, audit TEXT NOT NULL, source BLOB NOT NULL, view BLOB NOT NULL, manifest BLOB NOT NULL, stored_bytes INTEGER NOT NULL);
            PRAGMA user_version=4;
            COMMIT;
        """)

    def clock(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise StoreError("TaskStore clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            self._db.close()

    def pin_policy(self, policy_id, digest):
        """Keep policy identity stable across parent restarts.

        Candidate v2 records with an existing action but no policy pin need
        explicit migration/reconciliation, never an inferred permission grant.
        """
        from pydantic import TypeAdapter
        policy_id = TypeAdapter(Identifier).validate_python(policy_id)
        digest = TypeAdapter(Sha256).validate_python(digest)
        with self._tx() as db:
            previous = db.execute("SELECT digest FROM policies WHERE id=?", (policy_id,)).fetchone()
            if previous is not None:
                if previous["digest"] != digest:
                    raise Conflict("Persisted policy ID has different immutable content")
                return
            for row in db.execute("SELECT data FROM actions"):
                action = ActionEnvelope.model_validate_json(row["data"])
                if action.policy_ref == policy_id:
                    raise AdmissionDenied("Existing dispatches have no policy pin; explicit migration is required")
            db.execute("INSERT INTO policies VALUES(?,?)", (policy_id, digest))

    def assert_policy(self, policy_id, digest):
        with self._tx() as db:
            row = db.execute("SELECT digest FROM policies WHERE id=?", (policy_id,)).fetchone()
            if row is None or row["digest"] != digest:
                raise AdmissionDenied("Policy content does not match its persistent authority")

    def _task(self, db, task_id, expected_revision=None):
        row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise StoreError("Task does not exist")
        task = Task.model_validate_json(row["data"])
        if expected_revision is not None and task.revision != expected_revision:
            raise Conflict("Task revision changed")
        return task

    def _event(self, db, task, kind, payload):
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE task_id=?", (task.id,)).fetchone()[0]
        event = Event(id=uuid.uuid4().hex, sequence=sequence, timestamp=self.clock(), task_id=task.id,
                      task_revision=task.revision, type=kind, redacted_payload=payload)
        db.execute("INSERT INTO events VALUES(?,?,?)", (task.id, sequence, _json(event)))
        return event

    def _save_task(self, db, task, **changes):
        value = task.model_dump(mode="json")
        value.update(changes, updated_at=self.clock().isoformat())
        updated = Task.model_validate(value)
        db.execute("UPDATE tasks SET revision=?,status=?,data=? WHERE id=?", (updated.revision, updated.status, _json(updated), updated.id))
        return updated

    def create_task(self, task: Task):
        task = Task.model_validate_json(_json(task))
        if task.revision != 0 or task.status != "DRAFT" or task.completion is not None:
            raise AdmissionDenied("New tasks must begin as an unverified DRAFT at revision zero")
        with self._tx() as db:
            try:
                db.execute("INSERT INTO tasks VALUES(?,?,?,?)", (task.id, task.revision, task.status, _json(task)))
            except sqlite3.IntegrityError:
                raise Conflict("Task already exists") from None
            self._event(db, task, "task.created", {})
        return task

    def get_task(self, task_id):
        with self._tx() as db:
            return self._task(db, task_id)

    def list_tasks(self, limit=40):
        with self._tx() as db:
            return [Task.model_validate_json(row["data"]) for row in db.execute("SELECT data FROM tasks ORDER BY rowid DESC LIMIT ?", (min(max(limit, 1), 200),))]

    def events(self, task_id):
        with self._tx() as db:
            self._task(db, task_id)
            return [Event.model_validate_json(row["data"]) for row in db.execute("SELECT data FROM events WHERE task_id=? ORDER BY sequence", (task_id,))]

    def transition(self, task_id, expected_revision, status):
        with self._tx() as db:
            task = self._task(db, task_id, expected_revision)
            allowed = TRANSITIONS.get(task.status, set()) | ({"FAILED"} if task.status not in TERMINAL | {"CANCELLING"} else set())
            if status == "SUCCEEDED" or status not in allowed:
                raise AdmissionDenied("Task state transition is not allowed")
            if task.status == "PAUSED" and status == "REOBSERVE":
                self._assert_resumable(db, task)
            updated = self._save_task(db, task, status=status, revision=task.revision+1)
            if status in TERMINAL | {"PAUSED"} or task.status == "PAUSED":
                db.execute("UPDATE leases SET revoked=1 WHERE owner=?", (task.id,))
                db.execute("UPDATE approvals SET revoked=1 WHERE task_id=?", (task.id,))
                db.execute("UPDATE actions SET state='outcome_unknown' WHERE task_id=? AND state='dispatched'", (task.id,))
            self._event(db, updated, "task.transitioned", {"from": task.status, "to": status})
            return updated

    def acquire_lease(self, task_id: str, scope: ResourceScope, ttl_seconds=30):
        scope = ResourceScope.model_validate_json(_json(scope))
        if not 0 < ttl_seconds <= 300:
            raise ValueError("Lease duration must be within 300 seconds")
        with self._tx() as db:
            task = self._task(db, task_id)
            if task.status in TERMINAL | {"CANCELLING"}:
                raise AdmissionDenied("Task cannot acquire a resource")
            now = self.clock()
            identity = _resource_identity(scope)
            existing = db.execute("SELECT id,identity FROM resources WHERE id=? OR identity=?", (scope.resource_id, identity)).fetchone()
            if existing and (existing["id"] != scope.resource_id or existing["identity"] != identity):
                raise Conflict("Resource ID is already bound to a different identity, or this executor has another canonical ID")
            occupied = db.execute("SELECT id FROM leases WHERE resource_id=? AND revoked=0 AND expires_at>?", (scope.resource_id, now.isoformat())).fetchone()
            if occupied:
                raise Conflict("Resource already has a live lease")
            db.execute("INSERT INTO resources VALUES(?,0,?) ON CONFLICT(id) DO NOTHING", (scope.resource_id, identity))
            db.execute("UPDATE resources SET fence=fence+1 WHERE id=?", (scope.resource_id,))
            fence = db.execute("SELECT fence FROM resources WHERE id=?", (scope.resource_id,)).fetchone()[0]
            lease = ResourceLease(id=uuid.uuid4().hex, resource_id=scope.resource_id, resource_scope=scope,
                owner=task_id, fencing_token=fence, issued_at=now, expires_at=now+timedelta(seconds=ttl_seconds), revoked=False)
            db.execute("INSERT INTO leases VALUES(?,?,?,?,0,?,?)", (lease.id, scope.resource_id, task_id, fence, lease.expires_at.isoformat(), _json(lease)))
            self._event(db, task, "lease.acquired", {"lease_id": lease.id, "resource_id": scope.resource_id, "fencing_token": fence})
            return lease

    def release_lease(self, lease_id, fencing_token):
        with self._tx() as db:
            row = db.execute("SELECT * FROM leases WHERE id=?", (lease_id,)).fetchone()
            if row is None or row["fence"] != fencing_token:
                raise AdmissionDenied("Lease identity or fencing token changed")
            db.execute("UPDATE leases SET revoked=1 WHERE id=?", (lease_id,))
            self._event(db, self._task(db, row["owner"]), "lease.released", {"lease_id": lease_id})

    def _check_lease(self, db, task, scope, lease_id, fencing_token):
        row = db.execute("SELECT l.*,r.fence AS current_fence FROM leases l JOIN resources r ON r.id=l.resource_id WHERE l.id=?", (lease_id,)).fetchone()
        if (row is None or row["revoked"] or row["owner"] != task.id
                or row["fence"] != fencing_token or row["current_fence"] != fencing_token):
            raise AdmissionDenied("Resource lease was revoked or superseded")
        lease = ResourceLease.model_validate_json(row["data"])
        if not lease.issued_at <= self.clock() < lease.expires_at or lease.resource_scope != scope:
            raise AdmissionDenied("Resource lease expired or has a different scope")
        return lease

    def validate_lease(self, task_id, expected_revision, scope, lease_id, fencing_token):
        """Parent read boundary; never grants a new lease or revives a stopped one."""
        scope = ResourceScope.model_validate_json(_json(scope))
        with self._tx() as db:
            task = self._task(db, task_id, expected_revision)
            if task.status in TERMINAL | {"CANCELLING"}:
                raise AdmissionDenied("Task no longer owns an active executor")
            return self._check_lease(db, task, scope, lease_id, fencing_token)

    def latest_observation_revision(self, task_id, resource_id):
        with self._tx() as db:
            self._task(db, task_id)
            return db.execute("SELECT COALESCE(MAX(revision),0) FROM observations WHERE task_id=? AND resource_id=?", (task_id, resource_id)).fetchone()[0]

    def assert_dispatch_authority(self, envelope: ActionEnvelope):
        """Check a journaled operation before the driver's next input command.

        This cannot revoke an IPC command already accepted by an external app.
        Approval is consumed at admission, so it is not consumed again here.
        """
        envelope = ActionEnvelope.model_validate_json(_json(envelope))
        with self._tx() as db:
            task = self._task(db, envelope.task_id, envelope.task_revision)
            if task.status != "RUNNING" or task.policy_ref != envelope.policy_ref:
                raise AdmissionDenied("Task no longer permits input")
            if db.execute("SELECT 1 FROM policies WHERE id=?", (task.policy_ref,)).fetchone() is None:
                raise AdmissionDenied("Task policy has no persistent authority pin")
            row = db.execute("SELECT state,payload_sha256 FROM actions WHERE id=? AND task_id=?", (envelope.action_id, task.id)).fetchone()
            if row is None or row["state"] != "dispatched" or row["payload_sha256"] != envelope.payload_sha256:
                raise AdmissionDenied("Input is not covered by the current journaled dispatch")
            self._check_lease(db, task, envelope.resource_scope, envelope.lease_id, envelope.fencing_token)
            row = db.execute("SELECT data FROM observations WHERE id=?", (envelope.observation_id,)).fetchone()
            observation = ObservationRef.model_validate_json(row["data"]) if row else None
            latest = db.execute("SELECT MAX(revision) FROM observations WHERE task_id=? AND resource_id=?", (task.id, envelope.resource_scope.resource_id)).fetchone()[0]
            if (observation is None or observation.task_id != task.id or observation.task_revision != task.revision
                    or observation.revision != envelope.observation_revision or observation.revision != latest
                    or observation.resource_scope != envelope.resource_scope
                    or not observation.observed_at <= self.clock() < observation.expires_at):
                raise AdmissionDenied("The dispatch observation is no longer current")
            if task.budget.max_seconds is not None and (self.clock()-task.created_at).total_seconds() >= task.budget.max_seconds:
                raise AdmissionDenied("Task wall-clock budget is exhausted")

    def mark_outcome_unknown(self, action_id, *, reason_code="executor_interrupted"):
        """Do not mislabel a partially applied operation as safely retryable."""
        with self._tx() as db:
            row = db.execute("SELECT task_id,state FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise StoreError("Action does not exist")
            if row["state"] == "outcome_unknown":
                return
            if row["state"] != "dispatched":
                raise Conflict("Action already has a known outcome")
            db.execute("UPDATE actions SET state='outcome_unknown' WHERE id=?", (action_id,))
            self._event(db, self._task(db, row["task_id"]), "action.outcome_unknown", {"action_id": action_id, "reason_code": reason_code})

    def record_observation(self, observation: ObservationRef):
        observation = ObservationRef.model_validate_json(_json(observation))
        with self._tx() as db:
            task = self._task(db, observation.task_id, observation.task_revision)
            if task.status in TERMINAL | {"CANCELLING"}:
                raise AdmissionDenied("Task no longer accepts observations")
            latest = db.execute("SELECT MAX(revision) FROM observations WHERE task_id=? AND resource_id=?", (task.id, observation.resource_scope.resource_id)).fetchone()[0]
            if latest is not None and observation.revision <= latest:
                raise Conflict("Observation revisions must increase for this resource")
            db.execute("INSERT INTO observations VALUES(?,?,?,?,?)", (observation.id, task.id, observation.resource_scope.resource_id, observation.revision, _json(observation)))
            self._event(db, task, "observation.recorded", {"observation_id": observation.id, "revision": observation.revision})

    def approve(self, approval: Approval):
        approval = Approval.model_validate_json(_json(approval))
        with self._tx() as db:
            task = self._task(db, approval.task_id, approval.task_revision)
            if task.status in TERMINAL | {"CANCELLING"} or approval.revoked or approval.expires_at <= self.clock():
                raise AdmissionDenied("Approval is not currently valid")
            db.execute("INSERT INTO approvals(id,task_id,data) VALUES(?,?,?)", (approval.id, task.id, _json(approval)))
            self._event(db, task, "approval.recorded", {"approval_id": approval.id, "action_id": approval.action_id, "payload_sha256": approval.payload_sha256})

    def admit_action(self, envelope: ActionEnvelope, *, require_approval=True):
        envelope = ActionEnvelope.model_validate_json(_json(envelope))
        envelope.verify_payload_hash()
        with self._tx() as db:
            task = self._task(db, envelope.task_id, envelope.task_revision)
            previous = db.execute("SELECT * FROM actions WHERE id=? OR (task_id=? AND idempotency_key=?)", (envelope.action_id, task.id, envelope.idempotency_key)).fetchone()
            if previous:
                if previous["id"] != envelope.action_id or previous["payload_sha256"] != envelope.payload_sha256:
                    raise Conflict("Action identity was reused with different execution content")
                if previous["state"] in {"dispatched", "outcome_unknown"}:
                    raise OutcomeUnknown("Previous dispatch needs independent reconciliation; do not resend input")
                return {"dispatch": False, "state": previous["state"], "outcome": json.loads(previous["outcome"]) if previous["outcome"] else None}
            if task.status != "RUNNING" or task.policy_ref != envelope.policy_ref:
                raise AdmissionDenied("Task is not running under the action's policy revision")
            if db.execute("SELECT 1 FROM policies WHERE id=?", (task.policy_ref,)).fetchone() is None:
                raise AdmissionDenied("Task policy has no persistent authority pin")
            used_steps = db.execute("SELECT COUNT(*) FROM actions WHERE task_id=?", (task.id,)).fetchone()[0]
            if used_steps >= task.budget.max_steps:
                raise AdmissionDenied("Task action budget is exhausted")
            if task.budget.max_seconds is not None and (self.clock() - task.created_at).total_seconds() >= task.budget.max_seconds:
                raise AdmissionDenied("Task wall-clock budget is exhausted")
            if envelope.action.type in {"done", "ask_user"}:
                raise AdmissionDenied("Completion and user questions use dedicated parent gateways, not driver actions")
            for pending in db.execute("SELECT data FROM actions WHERE state IN ('dispatched','outcome_unknown')"):
                previous_action = ActionEnvelope.model_validate_json(pending["data"])
                if previous_action.resource_scope.resource_id == envelope.resource_scope.resource_id:
                    raise OutcomeUnknown("This resource still has an unresolved dispatch; reconcile it before another action")
            now = self.clock()
            row = db.execute("SELECT l.*,r.fence AS current_fence FROM leases l JOIN resources r ON r.id=l.resource_id WHERE l.id=?", (envelope.lease_id,)).fetchone()
            if row is None or row["revoked"] or row["owner"] != task.id or row["fence"] != envelope.fencing_token or row["current_fence"] != envelope.fencing_token:
                raise AdmissionDenied("Resource lease was revoked or superseded")
            lease = ResourceLease.model_validate_json(row["data"])
            if not lease.issued_at <= now < lease.expires_at or _json(lease.resource_scope) != _json(envelope.resource_scope):
                raise AdmissionDenied("Resource lease expired or has a different scope")
            row = db.execute("SELECT data FROM observations WHERE id=?", (envelope.observation_id,)).fetchone()
            if row is None:
                raise AdmissionDenied("Action has no recorded observation")
            observation = ObservationRef.model_validate_json(row["data"])
            latest = db.execute("SELECT MAX(revision) FROM observations WHERE task_id=? AND resource_id=?", (task.id, envelope.resource_scope.resource_id)).fetchone()[0]
            if (observation.task_id != task.id or observation.task_revision != task.revision
                    or observation.revision != envelope.observation_revision or observation.revision != latest
                    or not observation.observed_at <= now < observation.expires_at or _json(observation.resource_scope) != _json(envelope.resource_scope)):
                raise AdmissionDenied("Observation is stale or belongs to another task/resource")
            if require_approval:
                row = db.execute("SELECT * FROM approvals WHERE id=?", (envelope.approval_id,)).fetchone()
                if row is None or row["revoked"] or row["used"]:
                    raise AdmissionDenied("Exact action approval is absent, consumed or revoked")
                approval = Approval.model_validate_json(row["data"])
                if not approval.matches(envelope, now):
                    raise AdmissionDenied("Approval no longer matches the exact action")
                db.execute("UPDATE approvals SET used=1 WHERE id=?", (approval.id,))
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,NULL)", (envelope.action_id, task.id, envelope.idempotency_key,
                envelope.payload_sha256, "dispatched", _json(envelope)))
            self._event(db, task, "action.dispatched", {"action_id": envelope.action_id, "payload_sha256": envelope.payload_sha256})
            return {"dispatch": True, "state": "dispatched"}

    def existing_action_result(self, envelope: ActionEnvelope):
        """Read an exact previous result without re-admitting or replaying it."""
        envelope = ActionEnvelope.model_validate_json(_json(envelope))
        with self._tx() as db:
            row = db.execute("SELECT * FROM actions WHERE id=? OR (task_id=? AND idempotency_key=?)", (envelope.action_id, envelope.task_id, envelope.idempotency_key)).fetchone()
            if row is None:
                return None
            if (row["id"] != envelope.action_id or row["task_id"] != envelope.task_id
                    or row["payload_sha256"] != envelope.payload_sha256):
                raise Conflict("Action identity was reused with different execution content")
            if row["state"] in {"dispatched", "outcome_unknown"}:
                raise OutcomeUnknown("Previous dispatch needs independent reconciliation; do not resend input")
            return {"dispatch": False, "state": row["state"], "outcome": json.loads(row["outcome"]) if row["outcome"] else None}

    def record_outcome(self, action_id, outcome: dict, *, succeeded: bool):
        with self._tx() as db:
            row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None or row["state"] not in {"dispatched", "outcome_unknown"}:
                raise Conflict("Action is absent or already has an outcome")
            state = "succeeded" if succeeded else "failed"
            db.execute("UPDATE actions SET state=?,outcome=? WHERE id=?", (state, _json(outcome), action_id))
            self._event(db, self._task(db, row["task_id"]), "action.result", {"action_id": action_id, "state": state})

    def request_stop(self, task_id, expected_revision):
        with self._tx() as db:
            task = self._task(db, task_id, expected_revision)
            if task.status in TERMINAL | {"CANCELLING"}:
                return task
            updated = self._save_task(db, task, status="CANCELLING", revision=task.revision+1)
            db.execute("UPDATE leases SET revoked=1 WHERE owner=?", (task.id,))
            db.execute("UPDATE approvals SET revoked=1 WHERE task_id=?", (task.id,))
            db.execute("UPDATE actions SET state='outcome_unknown' WHERE task_id=? AND state='dispatched'", (task.id,))
            self._event(db, updated, "task.stop_requested", {"leases_revoked": True, "unknown_effects_require_reconciliation": True})
            return updated

    @staticmethod
    def _assert_resumable(db, task):
        if db.execute("SELECT 1 FROM actions WHERE task_id=? AND state IN ('dispatched','outcome_unknown') LIMIT 1",
                      (task.id,)).fetchone():
            raise OutcomeUnknown("Paused task has an unresolved effect; reconcile before resuming")

    def request_pause(self, task_id, expected_revision):
        """Revoke input authority in the same commit as the nonterminal pause.

        Driver cancellation/draining belongs to Gateway after this returns.
        Already dispatched input may have taken effect, so it cannot be replayed.
        A concurrent Stop/terminal verdict has priority and is never undone.
        """
        with self._tx() as db:
            task = self._task(db, task_id)
            if task.status in TERMINAL | {"CANCELLING"}:
                return task
            task = self._task(db, task_id, expected_revision)
            if task.status == "PAUSED":
                return task
            if "PAUSED" not in TRANSITIONS.get(task.status, set()):
                raise AdmissionDenied("Task cannot pause from its current state")
            updated = self._save_task(db, task, status="PAUSED", revision=task.revision+1)
            db.execute("UPDATE leases SET revoked=1 WHERE owner=?", (task.id,))
            db.execute("UPDATE approvals SET revoked=1 WHERE task_id=?", (task.id,))
            db.execute("UPDATE actions SET state='outcome_unknown' WHERE task_id=? AND state='dispatched'", (task.id,))
            self._event(db, updated, "task.pause_requested", {"leases_revoked": True,
                "approvals_revoked": True, "unknown_effects_require_reconciliation": True})
            return updated

    def request_resume(self, task_id, expected_revision):
        """Enter REOBSERVE only; no lease, permission or pending action revives."""
        with self._tx() as db:
            task = self._task(db, task_id, expected_revision)
            if task.status != "PAUSED":
                raise AdmissionDenied("Only a paused task can resume")
            self._assert_resumable(db, task)
            updated = self._save_task(db, task, status="REOBSERVE", revision=task.revision+1)
            db.execute("UPDATE leases SET revoked=1 WHERE owner=?", (task.id,))
            db.execute("UPDATE approvals SET revoked=1 WHERE task_id=?", (task.id,))
            self._event(db, updated, "task.resume_requested", {"fresh_observation_required": True,
                "fresh_authority_required": True})
            return updated

    def record_evidence(self, evidence: Evidence):
        evidence = Evidence.model_validate_json(_json(evidence))
        with self._tx() as db:
            task = self._task(db, evidence.task_id, evidence.task_revision)
            row = db.execute("SELECT data FROM observations WHERE id=?", (evidence.observation_id,)).fetchone()
            if row is None:
                raise AdmissionDenied("Evidence must cite an actual recorded observation")
            observation = ObservationRef.model_validate_json(row["data"])
            if (observation.task_id != task.id or observation.task_revision != task.revision
                    or observation.revision != evidence.observation_revision or evidence.observed_at < observation.observed_at
                    or evidence.observed_at > self.clock()):
                raise AdmissionDenied("Evidence observation identity or timestamp is inconsistent")
            db.execute("INSERT INTO evidence VALUES(?,?,?)", (evidence.id, task.id, _json(evidence)))
            self._event(db, task, "evidence.recorded", {"evidence_id": evidence.id, "result": evidence.result})

    def commit_verdict(self, task_id, expected_revision, verdict: CompletionVerdict):
        verdict = CompletionVerdict.model_validate_json(_json(verdict))
        with self._tx() as db:
            task = self._task(db, task_id, expected_revision)
            if task.status != "VERIFYING" or verdict.task_revision != task.revision:
                raise AdmissionDenied("Completion requires the current VERIFYING task revision")
            criteria = {criterion.id for criterion in task.success_criteria}
            if not set(verdict.criterion_ids) <= criteria or verdict.decided_at > self.clock():
                raise AdmissionDenied("Verdict cites unknown criteria or a future decision time")
            checked_evidence = []
            for evidence_id in verdict.evidence_ids:
                row = db.execute("SELECT data FROM evidence WHERE id=?", (evidence_id,)).fetchone()
                if row is None:
                    raise AdmissionDenied("Completion cites missing evidence")
                evidence = Evidence.model_validate_json(row["data"])
                if (evidence.task_id != task.id or evidence.task_revision != task.revision
                        or evidence.verifier_ref != verdict.verifier_ref or evidence.source != "external_verifier"
                        or evidence.observed_at > verdict.decided_at):
                    raise AdmissionDenied("Completion evidence is stale, model-only, future-dated or from another verifier")
                for criterion in task.success_criteria:
                    if criterion.id in evidence.criterion_ids and criterion.verifier_ref not in (None, evidence.verifier_ref):
                        raise AdmissionDenied("Evidence did not come from the criterion's required verifier")
                checked_evidence.append(evidence)
            if verdict.verdict == "failed" and not any(item.result == "fail" for item in checked_evidence):
                raise AdmissionDenied("A failed verdict requires an independently observed failure")
            if verdict.verdict == "succeeded":
                unresolved = db.execute("SELECT 1 FROM actions WHERE task_id=? AND state IN ('dispatched','outcome_unknown') LIMIT 1", (task.id,)).fetchone()
                if unresolved:
                    raise OutcomeUnknown("Task still has an unresolved external effect; completion cannot be certified")
                if set(verdict.criterion_ids) != criteria:
                    raise AdmissionDenied("Not all user-defined success criteria were verified")
                covered = set()
                for evidence in checked_evidence:
                    if evidence.result != "pass":
                        raise AdmissionDenied("Completion evidence is stale, failed, model-only or from another verifier")
                    observation_row = db.execute("SELECT data FROM observations WHERE id=?", (evidence.observation_id,)).fetchone()
                    observation = ObservationRef.model_validate_json(observation_row["data"])
                    latest = db.execute("SELECT MAX(revision) FROM observations WHERE task_id=? AND resource_id=?",
                        (task.id, observation.resource_scope.resource_id)).fetchone()[0]
                    if observation.revision != latest or observation.expires_at <= self.clock():
                        raise AdmissionDenied("Completion evidence no longer describes a current observation")
                    covered.update(evidence.criterion_ids)
                if covered != criteria:
                    raise AdmissionDenied("Successful independent evidence does not cover every criterion")
            status = {"succeeded": "SUCCEEDED", "failed": "FAILED", "inconclusive": "INCONCLUSIVE"}[verdict.verdict]
            # Keep the verified control revision; this terminal commit cannot
            # admit more actions and does not pretend old evidence is newer.
            updated = self._save_task(db, task, status=status, completion=verdict.model_dump(mode="json"))
            self._event(db, updated, "task.verdict", {"verdict": verdict.verdict, "evidence_ids": list(verdict.evidence_ids)})
            db.execute("UPDATE leases SET revoked=1 WHERE owner=?", (task.id,))
            db.execute("UPDATE approvals SET revoked=1 WHERE task_id=?", (task.id,))
            return updated

    def pending_effects(self, task_id):
        with self._tx() as db:
            self._task(db, task_id)
            return [{"action_id": row["id"], "payload_sha256": row["payload_sha256"],
                     "idempotency_key": row["idempotency_key"], "state": "outcome_unknown"}
                    for row in db.execute("SELECT * FROM actions WHERE task_id=? AND state IN ('dispatched','outcome_unknown')", (task_id,))]

    def checkpoint(self, checkpoint: Checkpoint):
        checkpoint = Checkpoint.model_validate_json(_json(checkpoint))
        with self._tx() as db:
            task = self._task(db, checkpoint.task_id, checkpoint.task_revision)
            if checkpoint.plan_version != task.plan_version:
                raise Conflict("Checkpoint refers to a different plan version")
            policy = db.execute("SELECT digest FROM policies WHERE id=?", (task.policy_ref,)).fetchone()
            if policy is None or policy["digest"] != checkpoint.policy_digest:
                raise AdmissionDenied("Checkpoint policy digest does not match persistent authority")
            consumed = db.execute("SELECT COUNT(*) FROM actions WHERE task_id=?", (task.id,)).fetchone()[0]
            if checkpoint.budget_remaining.max_steps > max(0, task.budget.max_steps-consumed):
                raise AdmissionDenied("Checkpoint cannot increase the remaining action budget")
            for field in ("max_seconds", "max_tokens", "max_cost_microunits"):
                original = getattr(task.budget, field)
                remaining = getattr(checkpoint.budget_remaining, field)
                if field == "max_seconds" and original is not None:
                    original = max(0, original-(self.clock()-task.created_at).total_seconds())
                if original is not None and (remaining is None or remaining > original):
                    raise AdmissionDenied("Checkpoint cannot loosen the task's original budget")
            if checkpoint.budget_remaining.currency != task.budget.currency:
                raise AdmissionDenied("Checkpoint cannot change cost units")
            rows = db.execute("SELECT * FROM actions WHERE task_id=? AND state IN ('dispatched','outcome_unknown')", (task.id,)).fetchall()
            expected = {(row["id"], row["payload_sha256"], row["idempotency_key"]) for row in rows}
            actual = {(item.action_id, item.payload_sha256, item.idempotency_key) for item in checkpoint.pending_effects}
            if expected != actual:
                raise AdmissionDenied("Checkpoint omitted or invented an unresolved external effect")
            for evidence_id in checkpoint.verified_evidence_ids:
                row = db.execute("SELECT data FROM evidence WHERE id=? AND task_id=?", (evidence_id, task.id)).fetchone()
                evidence = Evidence.model_validate_json(row["data"]) if row else None
                if (evidence is None or evidence.result != "pass" or evidence.source != "external_verifier"
                        or evidence.task_revision != task.revision):
                    raise AdmissionDenied("Checkpoint cites missing or failed evidence")
            db.execute("INSERT INTO checkpoints VALUES(?,?,?,?)", (checkpoint.id, task.id, task.revision, _json(checkpoint)))
            self._event(db, task, "checkpoint.saved", {"checkpoint_id": checkpoint.id, "pending_effect_count": len(expected)})

    def latest_checkpoint(self, task_id):
        with self._tx() as db:
            self._task(db, task_id)
            row = db.execute("SELECT data FROM checkpoints WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
            return Checkpoint.model_validate_json(row["data"]) if row else None

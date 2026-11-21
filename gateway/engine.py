"""Policy, approvals and durable execution state machine."""
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import Callable

from pydantic import ValidationError

from .adapters import SQL_ADAPTERS, Rejected, Uncertain
from .config import APPROVAL_SECONDS, Principal, same_digest
from .contracts import Closed, INTENT, OPERATION_BY_TYPE, READ_OPERATIONS, RECEIPTS, canonical, digest, fingerprint, strict_json
from .store import ACTIVE_STATES, append_event, select_principal, transition

LOG = logging.getLogger(__name__)
KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
MAX_ACTIVE = 128
MAX_ACTIVE_PER_PRINCIPAL = 32


class GatewayError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def get_row(db, execution_id):
    return db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()


def check_actor(db, principal: Principal):
    if select_principal(db, principal.name) != principal:
        raise GatewayError(401, "authentication_required")


def validated_intent(row):
    intent = INTENT.validate_python(strict_json(row["intent_json"]))
    if (intent.operation != row["operation"] or intent.schema_version != row["schema_version"]
            or fingerprint(intent.model_dump(exclude_none=True)) != row["request_hash"]):
        raise ValueError("Stored intent integrity check failed")
    return intent


def execution_view(row, principal):
    keys = ("id", "operation", "status", "reason", "created_at", "updated_at", "approval_expires_at")
    view = {key: row[key] for key in keys}
    receipt = strict_json(row["receipt_json"]) if row["receipt_json"] else None
    if receipt is not None and principal.role == "agent":
        if row["operation"] == "read-file":
            receipt.pop("content", None)
        if row["operation"] == "lookup-record" and receipt["collection"] == "tickets":
            receipt["record"].pop("detail", None)
    view["receipt"] = receipt
    if principal.role == "reviewer":
        view.update(
            requester=row["requester"], request_hash=row["request_hash"], policy_revision=row["policy_revision"],
            adapter=row["adapter_id"], adapter_version=row["adapter_version"], registry_revision=row["registry_revision"],
        )
        if row["status"] == "awaiting_approval":
            view["requested_intent"] = validated_intent(row).model_dump(exclude_none=True)
    return view


class Gateway:
    def __init__(self, store, policy, registry, workspace, *, clock=time.time,
                 fault: Callable | None = None, observe: Callable | None = None):
        self.store = store
        self.policy = policy
        self.policy_revision = policy.revision
        self.registry = registry
        self.workspace = workspace
        self.clock = clock
        self.fault = fault
        self.observe = observe
        self.cleanup_retry_at = {}

    def checkpoint(self, name, row):
        if self.fault is not None:
            self.fault(name, row)

    def measured(self, name, started):
        if self.observe is not None:
            self.observe(name, (time.perf_counter_ns() - started) / 1_000_000)

    def submit(self, principal, request_key, proposed):
        if not KEY.fullmatch(request_key):
            raise GatewayError(422, "invalid_idempotency_key")
        if principal.role == "reviewer":
            raise GatewayError(403, "submission_forbidden")
        started = time.perf_counter_ns()
        try:
            data = proposed
            if isinstance(proposed, Closed):
                data = {**vars(proposed), **(proposed.model_extra or {})}
                if OPERATION_BY_TYPE.get(type(proposed)) != data.get("operation"):
                    raise ValueError("Intent type and operation disagree")
            intent = INTENT.validate_python(data)
            arguments = intent.model_dump(exclude_none=True)
            request_hash = fingerprint(arguments)
        except (ValidationError, UnicodeError, ValueError) as exc:
            raise GatewayError(422, "invalid_intent") from exc
        self.measured("intent_validation", started)

        with self.store.connect(write=True) as db:
            check_actor(db, principal)
            existing = db.execute("SELECT * FROM executions WHERE requester=? AND scope=? AND request_key=?",
                                  (principal.name, principal.scope, request_key)).fetchone()
            if existing is not None:
                if not same_digest(existing["request_hash"], request_hash):
                    raise GatewayError(409, "idempotency_conflict")
                return execution_view(existing, principal), False

            now = self.clock()
            self.expire_approvals(db, now)
            started = time.perf_counter_ns()
            allowed = self.policy.authorize(principal, intent)
            self.measured("policy_decision", started)
            requires_approval = intent.operation not in READ_OPERATIONS
            state = "denied"
            expiry = None
            if allowed:
                self.check_capacity(db, principal.name)
                state = "awaiting_approval" if requires_approval else "ready"
                expiry = now + APPROVAL_SECONDS if requires_approval else None

            execution_id = uuid.uuid4().hex
            reason = None if allowed else "policy_denied"
            db.execute("""INSERT INTO executions (
                id,requester,scope,request_key,request_hash,operation,schema_version,policy_revision,
                status,reason,intent_json,approval_expires_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (execution_id, principal.name, principal.scope, request_key, request_hash, intent.operation,
                 intent.schema_version, self.policy_revision, state, reason,
                 canonical(arguments) if allowed else None, expiry, now, now))
            row = get_row(db, execution_id)
            append_event(db, row, reason or ("approval_required" if requires_approval else "authorized"), principal.name, now=now)
        return execution_view(row, principal), True

    @staticmethod
    def check_capacity(db, requester):
        active, owned = db.execute("""SELECT COUNT(*),COUNT(CASE WHEN requester=? THEN 1 END)
            FROM executions WHERE status IN (?,?,?)""", (requester, *ACTIVE_STATES)).fetchone()
        if active >= MAX_ACTIVE or owned >= MAX_ACTIVE_PER_PRINCIPAL:
            raise GatewayError(429, "execution_queue_full")

    def expire_approvals(self, db, now):
        rows = db.execute("""SELECT * FROM executions WHERE status='awaiting_approval'
            AND (approval_expires_at<=? OR policy_revision<>?)""", (now, self.policy_revision)).fetchall()
        for row in rows:
            reason = "policy_changed" if row["policy_revision"] != self.policy_revision else "approval_expired"
            transition(db, row, "expired", reason, now=now, reason=reason)

    @staticmethod
    def visible(row, principal):
        return row is not None and row["scope"] == principal.scope and (
            principal.role == "reviewer" or row["requester"] == principal.name
        )

    def decision(self, principal, execution_id, decision):
        if principal.role != "reviewer":
            raise GatewayError(403, "review_forbidden")
        with self.store.connect(write=True) as db:
            check_actor(db, principal)
            now = self.clock()
            self.expire_approvals(db, now)
            row = get_row(db, execution_id)
            if not self.visible(row, principal):
                error = GatewayError(404, "execution_not_found")
            elif row["requester"] == principal.name:
                error = GatewayError(403, "self_approval_forbidden")
            elif row["status"] != "awaiting_approval":
                error = GatewayError(409, row["reason"] if row["status"] == "expired" else "approval_not_pending")
            elif not same_digest(row["request_hash"], decision.request_hash) or not same_digest(
                row["policy_revision"], decision.policy_revision
            ):
                error = GatewayError(409, "approval_snapshot_changed")
            else:
                error = None
                if decision.decision == "deny":
                    row = transition(db, row, "denied", "reviewer_denied", now=now,
                                     actor=principal.name, reason="reviewer_denied")
                else:
                    reason = self.recheck(db, row, now, approval_in_progress=True)
                    if reason is None:
                        row = transition(db, row, "ready", "approved", now=now,
                                         actor=principal.name, approved_by=principal.name)
                    else:
                        row = transition(db, row, "denied", reason, now=now, reason=reason)
        if error is not None:
            raise error
        return execution_view(row, principal)

    def recheck(self, db, row, now, *, approval_in_progress=False):
        if row["policy_revision"] != self.policy_revision:
            return "policy_changed"
        try:
            intent = validated_intent(row)
        except (ValueError, TypeError, RecursionError):
            return "invalid_stored_intent"
        requester = select_principal(db, row["requester"])
        if requester is None or requester.scope != row["scope"] or not self.policy.authorize(requester, intent):
            return "policy_denied"
        if intent.operation not in READ_OPERATIONS and not approval_in_progress:
            reviewer = select_principal(db, row["approved_by"])
            if (reviewer is None or reviewer.role != "reviewer" or reviewer.name == requester.name
                    or reviewer.scope != requester.scope):
                return "approval_invalid"
            if row["approval_expires_at"] <= now:
                return "approval_expired"
        return None

    def list_executions(self, principal, *, limit=50, before=None, approval_queue=False):
        if approval_queue and principal.role != "reviewer":
            raise GatewayError(403, "review_forbidden")
        with self.store.connect() as db:
            check_actor(db, principal)
            clauses = ["scope=?"]
            parameters = [principal.scope]
            if principal.role != "reviewer":
                clauses.append("requester=?")
                parameters.append(principal.name)
            if approval_queue:
                clauses.extend(("status='awaiting_approval'", "approval_expires_at>?", "policy_revision=?"))
                parameters.extend((self.clock(), self.policy_revision))
            if before:
                anchor = get_row(db, before)
                if not self.visible(anchor, principal):
                    raise GatewayError(404, "execution_not_found")
                clauses.append("(created_at,id)<(?,?)")
                parameters.extend((anchor["created_at"], anchor["id"]))
            rows = db.execute(f"SELECT * FROM executions WHERE {' AND '.join(clauses)} ORDER BY created_at DESC,id DESC LIMIT ?",
                              (*parameters, limit)).fetchall()
        return [execution_view(row, principal) for row in rows]

    def get_execution(self, principal, execution_id, *, events=False):
        with self.store.connect() as db:
            db.execute("BEGIN")
            check_actor(db, principal)
            row = get_row(db, execution_id)
            if not self.visible(row, principal):
                raise GatewayError(404, "execution_not_found")
            if events:
                columns = "seq,state,at,detail"
                if principal.role == "reviewer":
                    columns += ",actor,policy_revision,adapter_id,attempt"
                return [dict(event) for event in db.execute(
                    f"SELECT {columns} FROM events WHERE execution_id=? ORDER BY seq", (execution_id,)
                )]
        return execution_view(row, principal)

    def inbox(self, principal):
        with self.store.connect() as db:
            check_actor(db, principal)
            return [dict(row) for row in db.execute("""SELECT id,sender,body,created_at FROM inbox
                WHERE scope=? AND recipient=? ORDER BY created_at DESC,id DESC LIMIT 100""",
                (principal.scope, principal.name))]

    def claim_next(self):
        with self.store.connect(write=True) as db:
            now = self.clock()
            self.expire_approvals(db, now)
            row = db.execute("SELECT * FROM executions WHERE status='ready' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            reason = self.recheck(db, row, now)
            if reason is not None:
                transition(db, row, "denied", reason, now=now, reason=reason)
                return row, None
            started = time.perf_counter_ns()
            selected = self.registry.resolve(row["operation"])
            self.measured("registry_lookup", started)
            if selected is None:
                transition(db, row, "failed", "no_enabled_adapter", now=now, reason="no_enabled_adapter")
                return row, None
            self.checkpoint("before_claim_commit", row)
            claimed = transition(db, row, "executing", "adapter_claimed", now=now, attempt=row["attempt"] + 1,
                                 adapter_id=selected.adapter_id, adapter_version=selected.version,
                                 registry_revision=self.registry.revision)
        return claimed, selected

    def finalize(self, row, state, reason, *, receipt=None, cleanup_path=None):
        with self.store.connect(write=True) as db:
            current = get_row(db, row["id"])
            if current["status"] != "executing" or current["attempt"] != row["attempt"]:
                raise RuntimeError("Execution attempt changed unexpectedly")
            transition(db, current, state, reason or "effect_verified", now=self.clock(),
                       reason=reason, receipt=receipt, cleanup_path=cleanup_path)

    def cleanup_files(self, execution_id=None):
        with self.store.connect() as db:
            if execution_id is None:
                rows = db.execute("SELECT id,cleanup_path FROM executions WHERE cleanup_path IS NOT NULL LIMIT 128").fetchall()
            else:
                rows = db.execute("SELECT id,cleanup_path FROM executions WHERE id=? AND cleanup_path IS NOT NULL",
                                  (execution_id,)).fetchall()
        for row in rows:
            if time.monotonic() < self.cleanup_retry_at.get(row["id"], 0):
                continue
            try:
                self.workspace.discard(row["id"], row["cleanup_path"])
            except (Rejected, OSError):
                self.cleanup_retry_at[row["id"]] = time.monotonic() + 30
                LOG.warning("Workspace staging cleanup deferred for %s", row["id"])
                continue
            with self.store.connect(write=True) as db:
                db.execute("UPDATE executions SET cleanup_path=NULL WHERE id=?", (row["id"],))
            self.cleanup_retry_at.pop(row["id"], None)

    def check_receipt(self, row, intent, candidate):
        result = RECEIPTS[intent.operation].model_validate(candidate).model_dump()
        operation = intent.operation
        if operation == "send-message":
            matches = result["recipient"] == intent.recipient and result["message_id"] == "msg-" + row["id"]
        elif operation == "lookup-record":
            key = "record_id" if intent.collection == "services" else "ticket_id"
            matches = result["collection"] == intent.collection and result["record"].get(key) == intent.record_id
        elif operation == "create-ticket":
            matches = result == {"ticket_id": "tkt-" + row["id"], "revision": 1, "status": "open"}
        elif operation == "update-ticket":
            matches = result["ticket_id"] == intent.ticket_id and result["revision"] == intent.expected_revision + 1
            matches = matches and (intent.status is None or result["status"] == intent.status)
        else:
            data = (intent.content if operation == "write-file" else result["content"]).encode("utf-8")
            matches = result["path"] == intent.path and result["bytes"] == len(data) and result["sha256"] == digest(data)
        if not matches:
            raise Rejected("invalid_effect_receipt")
        return result

    def execute_sql(self, row, selected, intent):
        with self.store.connect(write=True) as db:
            current = get_row(db, row["id"])
            started = time.perf_counter_ns()
            receipt = SQL_ADAPTERS[selected.implementation](db, current, intent, self.clock())
            receipt = self.check_receipt(row, intent, receipt)
            self.measured("adapter", started)
            started = time.perf_counter_ns()
            self.checkpoint("before_sql_commit", row)
            transition(db, current, "succeeded", "effect_verified", now=self.clock(), receipt=receipt)
        self.measured("persistence_commit", started)
        self.checkpoint("after_sql_commit", row)

    def execute_file(self, row, intent):
        started = time.perf_counter_ns()
        if intent.operation == "read-file":
            receipt = self.check_receipt(row, intent, self.workspace.read(intent))
            self.measured("adapter", started)
            commit_started = time.perf_counter_ns()
            self.finalize(row, "succeeded", None, receipt=receipt)
            self.measured("persistence_commit", commit_started)
            return

        try:
            stage = self.workspace.stage(row["id"], intent)
        except OSError as exc:
            raise Rejected("file_staging_failed") from exc
        stage["path"] = intent.path
        stage_ms = time.perf_counter_ns() - started
        checkpoint_started = time.perf_counter_ns()
        with self.store.connect(write=True) as db:
            updated = db.execute("UPDATE executions SET stage_json=? WHERE id=? AND status='executing' AND attempt=?",
                                 (canonical(stage), row["id"], row["attempt"])).rowcount
            if updated != 1:
                raise RuntimeError("Execution state changed during file staging")
        self.checkpoint("after_file_stage", row)
        self.measured("persistence_checkpoint", checkpoint_started)
        publish_started = time.perf_counter_ns()
        receipt = self.workspace.publish(row["id"], intent.path, stage)
        self.checkpoint("after_file_publish", row)
        try:
            receipt = self.check_receipt(row, intent, receipt)
        except (ValidationError, Rejected) as exc:
            raise Uncertain("file_receipt_invalid") from exc
        self.measured("adapter", publish_started - stage_ms)
        commit_started = time.perf_counter_ns()
        self.finalize(row, "succeeded", None, receipt=receipt, cleanup_path=intent.path)
        self.measured("persistence_commit", commit_started)
        self.checkpoint("after_file_commit", row)

    def run_once(self):
        self.cleanup_files()
        claim = self.claim_next()
        if claim is None:
            return False
        row, selected = claim
        if selected is None:
            return True
        self.checkpoint("after_claim_commit", row)
        intent = validated_intent(row)
        cleanup_path = intent.path if intent.operation == "write-file" else None
        try:
            if selected.implementation in SQL_ADAPTERS:
                self.execute_sql(row, selected, intent)
            else:
                self.execute_file(row, intent)
        except Rejected as exc:
            self.finalize(row, "failed", exc.code, cleanup_path=cleanup_path)
        except Uncertain:
            self.finalize(row, "outcome_unknown", "file_publication_unconfirmed")
        except sqlite3.Error:
            raise  # A commit may have reached disk. Recovery reads durable state before deciding.
        except (ValidationError, OSError):
            # SQL rollback and read-only failures have no business effect. A write failure can be ambiguous.
            LOG.error("Adapter failure for %s", row["id"])
            state = "outcome_unknown" if cleanup_path is not None else "failed"
            self.finalize(row, state, "adapter_io_or_receipt_failure")
        self.cleanup_files(row["id"])
        return True

    def recover(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM executions WHERE status='executing' ORDER BY created_at,id").fetchall()
        for row in rows:
            if row["operation"] != "write-file":
                with self.store.connect(write=True) as db:
                    if row["attempt"] >= 3:
                        transition(db, row, "failed", "recovery_attempt_limit", now=self.clock(), reason="recovery_attempt_limit")
                    else:
                        transition(db, row, "ready", "interrupted_before_atomic_completion", now=self.clock())
                continue

            try:
                intent = validated_intent(row)
            except (ValueError, TypeError, RecursionError):
                self.finalize(row, "outcome_unknown", "invalid_stored_intent")
                continue
            if not row["stage_json"]:
                self.finalize(row, "failed", "interrupted_before_publication", cleanup_path=intent.path)
                continue
            try:
                receipt = self.workspace.reconcile(row["id"], intent.path, strict_json(row["stage_json"]))
                if receipt is not None:
                    receipt = self.check_receipt(row, intent, receipt)
            except (Rejected, OSError, ValueError, TypeError, KeyError, RecursionError):
                receipt = None
            if receipt is None:
                self.finalize(row, "outcome_unknown", "file_publication_unconfirmed")
            else:
                self.finalize(row, "succeeded", None, receipt=receipt, cleanup_path=intent.path)
        self.cleanup_files()

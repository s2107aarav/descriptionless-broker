"""SQLite schema and transaction primitives for one local runtime."""
import fcntl
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager

from .config import Principal, SCOPE, ensure_private_directory, ensure_private_file
from .contracts import canonical, digest

SCHEMA_VERSION = 1
ACTIVE_STATES = ("awaiting_approval", "ready", "executing")
TERMINAL_STATES = frozenset({"denied", "expired", "failed", "succeeded", "outcome_unknown"})
TRANSITIONS = {
    "awaiting_approval": {"ready", "denied", "expired"},
    "ready": {"executing", "denied", "failed"},
    "executing": {"ready", "succeeded", "failed", "outcome_unknown"},
}

SCHEMA = (
    """CREATE TABLE principals (
        name TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL CHECK(role IN ('requester','agent','reviewer')),
        scope TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1))
    )""",
    """CREATE TABLE executions (
        id TEXT PRIMARY KEY, requester TEXT NOT NULL REFERENCES principals(name),
        scope TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        operation TEXT NOT NULL, schema_version TEXT NOT NULL, policy_revision TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('ready','awaiting_approval','executing','succeeded','failed','outcome_unknown','denied','expired')),
        reason TEXT, intent_json TEXT, receipt_json TEXT, approval_expires_at REAL,
        approved_by TEXT REFERENCES principals(name), adapter_id TEXT, adapter_version TEXT,
        registry_revision TEXT, attempt INTEGER NOT NULL DEFAULT 0, stage_json TEXT, cleanup_path TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(requester, scope, request_key)
    )""",
    """CREATE INDEX execution_queue ON executions (status, created_at, id)""",
    """CREATE INDEX execution_owner ON executions (requester, scope, created_at, id)""",
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions(id),
        state TEXT NOT NULL, at REAL NOT NULL, detail TEXT NOT NULL, actor TEXT,
        policy_revision TEXT NOT NULL, adapter_id TEXT, attempt INTEGER NOT NULL
    )""",
    """CREATE INDEX event_execution ON events (execution_id, seq)""",
    """CREATE TABLE services (
        scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
        PRIMARY KEY(scope,id)
    )""",
    """CREATE TABLE tickets (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','investigating','resolved')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        origin_execution TEXT NOT NULL UNIQUE REFERENCES executions(id)
    )""",
    """CREATE TABLE inbox (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, recipient TEXT NOT NULL REFERENCES principals(name),
        sender TEXT NOT NULL REFERENCES principals(name), body TEXT NOT NULL, created_at REAL NOT NULL,
        origin_execution TEXT NOT NULL UNIQUE REFERENCES executions(id)
    )""",
    """CREATE INDEX inbox_owner ON inbox (recipient,scope,created_at,id)""",
)


@contextmanager
def runtime_lock(root):
    ensure_private_directory(root)
    fd = os.open(root / ".gateway.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        ensure_private_file(root / ".gateway.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another gateway process holds the runtime lock") from exc
        yield
    finally:
        os.close(fd)


class Store:
    def __init__(self, root):
        ensure_private_directory(root)
        self.path = root / "gateway.sqlite3"

    def migrate(self, *, create=False):
        if create:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        ensure_private_file(self.path)
        with self.connect() as db:
            if db.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise RuntimeError("SQLite WAL mode unavailable")
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("PRAGMA user_version").fetchone()[0]
            if current == 0:
                for ddl in SCHEMA:
                    db.execute(ddl)
                db.execute("PRAGMA user_version = 1")
            elif current != SCHEMA_VERSION:
                raise RuntimeError("Unsupported gateway database schema")
        with self.connect() as db:
            if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Gateway database integrity check failed")

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=4)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA synchronous = FULL")
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def provision_demo(self):
        output = {}
        with self.connect(write=True) as db:
            for name in ("requester", "agent", "reviewer"):
                token = secrets.token_urlsafe(32)
                result = db.execute("""INSERT INTO principals (name,token_hash,role,scope,active)
                    VALUES (?,?,?,?,1) ON CONFLICT(name) DO NOTHING""",
                    (name, digest(token.encode("ascii")), name, SCOPE))
                if result.rowcount:
                    output[name] = token
            db.execute("""INSERT INTO services VALUES (?,?,?,?) ON CONFLICT(scope,id) DO NOTHING""",
                       (SCOPE, "gateway", "Intent execution gateway", "Governed local workspace, ticketing and inbox."))
        return output

    def rotate_token(self, name):
        token = secrets.token_urlsafe(32)
        with self.connect(write=True) as db:
            count = db.execute("UPDATE principals SET token_hash=?, active=1 WHERE name=?",
                               (digest(token.encode("ascii")), name)).rowcount
            if not count:
                raise ValueError("Unknown principal")
        return token

    def revoke(self, name):
        with self.connect(write=True) as db:
            if not db.execute("UPDATE principals SET active=0 WHERE name=?", (name,)).rowcount:
                raise ValueError("Unknown principal")

    def authenticate(self, token):
        with self.connect() as db:
            row = db.execute("SELECT name,role,scope FROM principals WHERE token_hash=? AND active=1",
                             (digest(token.encode("ascii")),)).fetchone()
        return Principal(**row) if row else None


def append_event(db, row, detail, actor, *, now=None):
    db.execute("""INSERT INTO events (execution_id,state,at,detail,actor,policy_revision,adapter_id,attempt)
        VALUES (?,?,?,?,?,?,?,?)""",
        (row["id"], row["status"], time.time() if now is None else now, detail, actor, row["policy_revision"],
         row["adapter_id"], row["attempt"]))


def transition(db, row, state, detail, *, now, actor=None, reason=None, receipt=None, **updates):
    if state not in TRANSITIONS[row["status"]]:
        raise RuntimeError("Illegal execution transition")
    allowed = {"approved_by", "adapter_id", "adapter_version", "registry_revision", "attempt", "stage_json", "cleanup_path"}
    if updates.keys() - allowed:
        raise RuntimeError("Invalid transition field")
    updates.update(status=state, reason=reason, updated_at=now)
    if state in TERMINAL_STATES:
        updates["receipt_json"] = canonical(receipt) if receipt is not None else None
        updates["intent_json"] = None
        if state != "outcome_unknown":
            updates["stage_json"] = None
    setters = ", ".join(f"{key}=?" for key in updates)
    changed = db.execute(f"UPDATE executions SET {setters} WHERE id=? AND status=?",
                         (*updates.values(), row["id"], row["status"])).rowcount
    if changed != 1:
        raise RuntimeError("Execution state changed unexpectedly")
    current = db.execute("SELECT * FROM executions WHERE id=?", (row["id"],)).fetchone()
    append_event(db, current, detail, actor, now=now)
    return current


def select_principal(db, name):
    row = db.execute("SELECT name,role,scope FROM principals WHERE name=? AND active=1", (name,)).fetchone()
    return Principal(**row) if row else None


def purge_receipts(store, cutoff):
    with store.connect(write=True) as db:
        # The operation rows and idempotency keys are retained.
        count = db.execute("""UPDATE executions SET receipt_json=NULL, intent_json=NULL
            WHERE updated_at<? AND status IN ('succeeded','failed','denied','expired','outcome_unknown')
            AND (intent_json IS NOT NULL OR receipt_json IS NOT NULL)""", (cutoff,)).rowcount
    return count


def verify_audit(store):
    with store.connect() as db:
        db.execute("BEGIN")
        rows = db.execute("SELECT id,status,attempt FROM executions ORDER BY id").fetchall()
        events = db.execute("SELECT execution_id,state,attempt FROM events ORDER BY seq").fetchall()
        for table, operation in (("tickets", "create-ticket"), ("inbox", "send-message")):
            mismatch = db.execute(f"""SELECT 1 FROM executions AS e
                LEFT JOIN {table} AS d ON d.origin_execution=e.id
                WHERE e.operation=? AND ((e.status='succeeded' AND d.id IS NULL)
                    OR (e.status<>'succeeded' AND d.id IS NOT NULL) OR d.scope<>e.scope) LIMIT 1""", (operation,)).fetchone()
            if mismatch:
                raise RuntimeError("A transactional effect and its audit record disagree")
            incorrect_origin = db.execute(f"""SELECT 1 FROM {table} AS d JOIN executions AS e ON e.id=d.origin_execution
                WHERE e.operation<>? LIMIT 1""", (operation,)).fetchone()
            if incorrect_origin:
                raise RuntimeError("A transactional effect has the wrong originating operation")
    by_id = {row["id"]: [] for row in rows}
    for event in events:
        if event["execution_id"] not in by_id:
            raise RuntimeError("Orphan audit event")
        by_id[event["execution_id"]].append(event)
    for row in rows:
        trail = by_id[row["id"]]
        if not trail or trail[0]["state"] not in {"denied", "ready", "awaiting_approval"} or trail[0]["attempt"] != 0:
            raise RuntimeError("Missing or invalid initial audit event")
        for previous, current in zip(trail, trail[1:]):
            if current["state"] not in TRANSITIONS.get(previous["state"], set()):
                raise RuntimeError("Invalid audited transition")
            if current["attempt"] != previous["attempt"] + (current["state"] == "executing"):
                raise RuntimeError("Invalid audited attempt number")
        if (trail[-1]["state"], trail[-1]["attempt"]) != (row["status"], row["attempt"]):
            raise RuntimeError("Execution and audit trail disagree")
    return {"executions": len(rows), "events": len(events)}

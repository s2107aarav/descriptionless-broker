"""SQLite execution claims and audit receipts; no import-time filesystem writes.

A crash after invocation but before receipt commit has UNKNOWN outcome. The same
principal/key is never automatically re-executed. This is not exactly-once delivery
at an external service; such a service must also support durable idempotency.
"""
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DATA_DIR = str(Path(os.getenv("BROKER_DATA_DIR", Path(__file__).resolve().parents[1] / ".runtime" / "v2")))
REGISTRY_DB_PATH = str(Path(DATA_DIR) / "registry.db")
LEDGER_DB_PATH = str(Path(DATA_DIR) / "ledger.db")


def canonical_json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_json_hash(data):
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def init_db():
    """Explicit compatibility hook for registry initialization only."""
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(REGISTRY_DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS tools (
            tool_id TEXT PRIMARY KEY, effect TEXT NOT NULL, handler_name TEXT NOT NULL,
            enabled INTEGER NOT NULL, description TEXT NOT NULL, priority INTEGER NOT NULL)""")


class AuditStore:
    def __init__(self, path=LEDGER_DB_PATH, capture_prompts=False):
        self.path = str(path)
        self.capture_prompts = capture_prompts
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS ledger (
                principal TEXT NOT NULL, intent_id TEXT NOT NULL, canonical_hash TEXT NOT NULL,
                effect TEXT NOT NULL, chosen_tool_id TEXT NOT NULL, latency_ms REAL NOT NULL DEFAULT 0,
                prompt_hash TEXT NOT NULL, timestamp REAL NOT NULL, result_json TEXT,
                request_json TEXT NOT NULL, status TEXT NOT NULL, request_hash TEXT, prompt_turn_id TEXT,
                PRIMARY KEY(principal, intent_id))""")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(ledger)")}
            if "request_hash" not in columns:
                conn.execute("ALTER TABLE ledger ADD COLUMN request_hash TEXT")
            if "prompt_turn_id" not in columns:
                conn.execute("ALTER TABLE ledger ADD COLUMN prompt_turn_id TEXT")
            conn.execute("""CREATE TABLE IF NOT EXISTS prompts (
                turn_id TEXT PRIMARY KEY, principal TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
                model TEXT NOT NULL, timestamp REAL NOT NULL, prompt_text TEXT)""")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _find_claim(conn, principal, intent_id):
        return conn.execute("SELECT * FROM ledger WHERE principal=? AND intent_id=?",
                            (principal, intent_id)).fetchone()

    @staticmethod
    def _existing_claim(row, request_hash, fingerprint):
        if row is None:
            return None
        if row["request_hash"] is not None and request_hash is not None:
            matches = row["request_hash"] == request_hash
        elif fingerprint is not None:
            matches = row["canonical_hash"] == fingerprint
        else:
            return None  # Legacy entries can only be compared after planning.
        return (row["status"], row) if matches else ("conflict", None)

    def lookup_claim(self, principal, intent_id, *, request_hash=None, fingerprint=None):
        with self.connect() as conn:
            row = self._find_claim(conn, principal, intent_id)
        return self._existing_claim(row, request_hash, fingerprint)

    def claim(self, principal, intent, tool_id, prompt_hash, *, request_hash=None, prompt_turn_id=None):
        fingerprint = canonical_json_hash(intent)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._find_claim(conn, principal, intent["intent_id"])
            existing = self._existing_claim(row, request_hash, fingerprint)
            if existing is not None:
                return existing
            conn.execute("""INSERT INTO ledger
                (principal,intent_id,canonical_hash,effect,chosen_tool_id,prompt_hash,timestamp,request_json,status,
                 request_hash,prompt_turn_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (principal, intent["intent_id"], fingerprint, intent["effect"], tool_id, prompt_hash,
                 time.time(), canonical_json(intent), "pending", request_hash, prompt_turn_id))
        return "new", None

    def finish(self, principal, intent_id, result, latency_ms, status="completed"):
        with self.connect() as conn:
            changed = conn.execute("""UPDATE ledger SET status=?, result_json=?, latency_ms=?
                WHERE principal=? AND intent_id=? AND status='pending'""",
                (status, canonical_json(result), latency_ms, principal, intent_id)).rowcount
            if changed != 1:
                raise RuntimeError("Execution claim is not pending")

    def record_prompt(self, principal, turn_id, messages, model):
        prompt = canonical_json([{"role": m.type, "content": m.content} for m in messages])
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("INSERT INTO prompts VALUES (?,?,?,?,?,?)",
                         (turn_id, principal, digest, model, time.time(), prompt if self.capture_prompts else None))
        return digest

    def ledger(self, principal=None, limit=200):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM ledger " + ("WHERE principal=? " if principal else "") +
                                "ORDER BY timestamp DESC LIMIT ?", ((principal,) if principal else ()) + (min(limit, 500),)).fetchall()
        return [dict(row) for row in rows]

    def prompts(self, principal, limit=200):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM prompts WHERE principal=? ORDER BY timestamp DESC LIMIT ?",
                                (principal, min(limit, 500))).fetchall()
        return [dict(row) for row in rows]

    def prompt_for_intent(self, principal, intent_id):
        with self.connect() as conn:
            claim = self._find_claim(conn, principal, intent_id)
            if claim is None:
                return None
            if claim["prompt_turn_id"] is not None:
                row = conn.execute("SELECT * FROM prompts WHERE principal=? AND turn_id=? AND prompt_sha256=?",
                                   (principal, claim["prompt_turn_id"], claim["prompt_hash"])).fetchone()
            else:
                # Old entries recorded only the digest. An ambiguous match cannot identify the turn.
                matches = conn.execute("""SELECT * FROM prompts WHERE principal=? AND prompt_sha256=?
                    AND timestamp<=? LIMIT 2""", (principal, claim["prompt_hash"], claim["timestamp"])).fetchall()
                row = matches[0] if len(matches) == 1 else None
        return dict(row) if row is not None else None

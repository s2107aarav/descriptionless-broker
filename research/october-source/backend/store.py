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
            conn.execute("""CREATE TABLE IF NOT EXISTS ledger (
                principal TEXT NOT NULL, intent_id TEXT NOT NULL, canonical_hash TEXT NOT NULL,
                effect TEXT NOT NULL, chosen_tool_id TEXT NOT NULL, latency_ms REAL NOT NULL DEFAULT 0,
                prompt_hash TEXT NOT NULL, timestamp REAL NOT NULL, result_json TEXT,
                request_json TEXT NOT NULL, status TEXT NOT NULL,
                PRIMARY KEY(principal, intent_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS prompts (
                turn_id TEXT PRIMARY KEY, principal TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
                model TEXT NOT NULL, timestamp REAL NOT NULL, prompt_text TEXT)""")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def claim(self, principal, intent, tool_id, prompt_hash):
        fingerprint = canonical_json_hash(intent)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM ledger WHERE principal=? AND intent_id=?",
                               (principal, intent["intent_id"])).fetchone()
            if row:
                if row["canonical_hash"] != fingerprint:
                    return "conflict", None
                return row["status"], json.loads(row["result_json"]) if row["result_json"] else None
            conn.execute("""INSERT INTO ledger
                (principal,intent_id,canonical_hash,effect,chosen_tool_id,prompt_hash,timestamp,request_json,status)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (principal, intent["intent_id"], fingerprint, intent["effect"], tool_id, prompt_hash,
                 time.time(), canonical_json(intent), "pending"))
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

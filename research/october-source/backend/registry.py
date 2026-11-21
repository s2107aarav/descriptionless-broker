import sqlite3
import uuid
from pydantic import BaseModel
from typing import List, Callable, Dict, Any
from .store import REGISTRY_DB_PATH, init_db
from . import tools_mock

class ToolDefinition(BaseModel):
    tool_id: str
    effect: str
    handler_name: str
    enabled: bool
    description: str
    priority: int

class Registry:
    def __init__(self, in_memory: bool = False, handlers=None):
        self.handlers = handlers
        if in_memory:
            self.db_path = f"file:memdb_{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._conn = sqlite3.connect(self.db_path, uri=True) # Keep alive
            with sqlite3.connect(self.db_path, uri=True) as conn:
                conn.execute("""
                    CREATE TABLE tools (
                        tool_id TEXT PRIMARY KEY, effect TEXT NOT NULL,
                        handler_name TEXT NOT NULL, enabled INTEGER NOT NULL,
                        description TEXT NOT NULL, priority INTEGER NOT NULL
                    )
                """)
        else:
            self.db_path = REGISTRY_DB_PATH
            init_db()

    def load_fixtures(self, tools_data: List[Dict[str, Any]]):
        with sqlite3.connect(self.db_path, uri=self.db_path.startswith("file:")) as conn:
            conn.execute("DELETE FROM tools")
            for t in tools_data:
                conn.execute(
                    "INSERT INTO tools (tool_id, effect, handler_name, enabled, description, priority) VALUES (?, ?, ?, ?, ?, ?)",
                    (t.get("tool_id") or str(uuid.uuid4()), t["effect"], t["handler_name"], int(t.get("enabled", True)), t["description"], t.get("priority", 0))
                )
            conn.commit()

    def get_enabled_tools_for_effect(self, effect: str) -> List[ToolDefinition]:
        with sqlite3.connect(self.db_path, uri=self.db_path.startswith("file:")) as conn:
            conn.row_factory = sqlite3.Row
            # "ORDER BY priority ASC, tool_id ASC"
            rows = conn.execute(
                "SELECT * FROM tools WHERE effect = ? AND enabled = 1 ORDER BY priority ASC, tool_id ASC",
                (effect,)
            ).fetchall()
            return [ToolDefinition(**dict(row)) for row in rows]
    
    def get_handler(self, handler_name: str) -> Callable:
        if self.handlers is not None:
            return self.handlers[handler_name]
        allowed = {"smtp_send_a", "smtp_send_b", "fs_read_a", "fs_read_b", "fs_write_a", "fs_write_b",
                   "db_lookup_a", "db_lookup_b", "jira_ticket_a", "jira_ticket_b"}
        base = handler_name.removesuffix("_preferred_fast_secure")
        if base not in allowed:
            raise KeyError("Unregistered handler")
        return getattr(tools_mock, base)

    def all_tools(self):
        with sqlite3.connect(self.db_path, uri=self.db_path.startswith("file:")) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM tools ORDER BY priority, tool_id")]

    def close(self):
        if hasattr(self, "_conn"):
            self._conn.close()

class LazyRegistry:
    """Initialize only on explicit use; importing the backend changes no data."""
    def __init__(self):
        self._registry = None
        import threading
        self._lock = threading.RLock()

    def __getattr__(self, name):
        with self._lock:
            if self._registry is None:
                self._registry = Registry()
                seed_default_registry(self._registry)
            return getattr(self._registry, name)


default_registry = LazyRegistry()

def seed_default_registry(registry):
    with sqlite3.connect(registry.db_path, uri=registry.db_path.startswith('file:')) as conn:
        count = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
    if count == 0:
        fixtures = [
            {"effect": "message-delivered", "handler_name": "smtp_send_a", "description": "Standard email sender", "priority": 10},
            {"effect": "message-delivered", "handler_name": "smtp_send_b", "description": "Alternative email sender", "priority": 20},
            {"effect": "file-content-returned", "handler_name": "fs_read_a", "description": "Standard file reader", "priority": 10},
            {"effect": "file-content-returned", "handler_name": "fs_read_b", "description": "Alternative file reader", "priority": 20},
            {"effect": "file-written", "handler_name": "fs_write_a", "description": "Standard file writer", "priority": 10},
            {"effect": "file-written", "handler_name": "fs_write_b", "description": "Alternative file writer", "priority": 20},
            {"effect": "record-returned", "handler_name": "db_lookup_a", "description": "Standard DB reader", "priority": 10},
            {"effect": "record-returned", "handler_name": "db_lookup_b", "description": "Alternative DB reader", "priority": 20},
            {"effect": "ticket-created", "handler_name": "jira_ticket_a", "description": "Standard Jira creator", "priority": 10},
            {"effect": "ticket-created", "handler_name": "jira_ticket_b", "description": "Alternative Jira creator", "priority": 20},
        ]
        registry.load_fixtures(fixtures)

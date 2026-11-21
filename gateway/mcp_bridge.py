"""Approved adapter mappings for bounded, loopback MCP ticket services."""
import re
import time
from contextlib import contextmanager
from typing import Annotated, Literal

import httpx
from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

from .adapters import Rejected, Uncertain
from .config import AdapterSpec, ensure_private_file, parse_config, same_digest
from .contracts import Closed, Detail, Digest, TicketReceipt, Title, canonical, fingerprint, strict_json
from .engine import GatewayError, check_actor

PROTOCOL = "2025-11-25"
CREATE = "create_incident_ticket"
READ = "get_incident_ticket"
TITLE_MAPPING = {"execution_id": "Gateway execution ID", "title": "Intent title", "detail": "Intent detail"}
ServiceId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")]
ExecutionKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class TicketArguments(Closed):
    execution_id: ExecutionKey
    title: Title
    detail: Detail


class TicketReadArguments(Closed):
    execution_id: ExecutionKey


class ServiceTicket(TicketReceipt):
    title: Title
    detail: Detail


class AllowedServer(Closed):
    id: ServiceId
    port: Annotated[int, Field(ge=1024, le=65535)]

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def binding(self):
        return fingerprint({"id": self.id, "endpoint": self.endpoint, "protocol": PROTOCOL})


class MappingRequest(Closed):
    server_id: ServiceId
    tool: Literal["create_incident_ticket"]
    schema_sha256: Digest
    priority: Annotated[int, Field(ge=0, le=1000)] = 5
    enabled: bool = True


class McpUnavailable(Exception):
    """Transport, protocol or advertised contract could not be verified."""


def ticket_tools():
    return [
        {"name": CREATE, "description": "Create an incident ticket in the local ticket service.",
         "inputSchema": TicketArguments.model_json_schema(), "outputSchema": TicketReceipt.model_json_schema()},
        {"name": READ, "description": "Read the ticket recorded for a gateway execution.",
         "inputSchema": TicketReadArguments.model_json_schema(), "outputSchema": ServiceTicket.model_json_schema()},
    ]


def shape(schema):
    """Drop descriptive annotations only from schemas, not from named properties."""
    if not isinstance(schema, dict):
        raise McpUnavailable()
    result = {key: value for key, value in schema.items() if key not in {"description", "title", "$comment", "examples"}}
    for key in ("properties", "$defs"):
        if key in result:
            if not isinstance(result[key], dict):
                raise McpUnavailable()
            result[key] = {name: shape(node) for name, node in result[key].items()}
    for key in ("anyOf", "oneOf", "allOf"):
        if key in result:
            if not isinstance(result[key], list):
                raise McpUnavailable()
            result[key] = [shape(item) for item in result[key]]
    if "required" in result:
        if not isinstance(result["required"], list) or not all(isinstance(item, str) for item in result["required"]):
            raise McpUnavailable()
        result["required"] = sorted(result["required"])
    return result


def interface(tools):
    return {
        item["name"]: {"input": shape(item["inputSchema"]), "output": shape(item["outputSchema"])}
        for item in tools if item["name"] in {CREATE, READ} and "outputSchema" in item
    }


class HttpMcp:
    def __init__(self, http, endpoint):
        self.http = http
        self.endpoint = endpoint
        self.next_id = 0
        self.info = None

    def request(self, method, params=None, *, notification=False):
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self.next_id
        if params is not None:
            payload["params"] = params
        try:
            deadline = time.monotonic() + 5
            with self.http.stream("POST", self.endpoint, json=payload) as response:
                if notification and response.status_code == 202:
                    return None
                if response.status_code != 200 or "Mcp-Session-Id" in response.headers:
                    raise McpUnavailable()
                if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    raise McpUnavailable()
                data = bytearray()
                for chunk in response.iter_raw():
                    if time.monotonic() > deadline or len(data) + len(chunk) > 65536:
                        raise McpUnavailable()
                    data.extend(chunk)
                result = strict_json(data)
            if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                    or type(result.get("id")) is not int or result["id"] != self.next_id):
                raise McpUnavailable()
            if not isinstance(result.get("result"), dict) or "error" in result:
                raise McpUnavailable()
            return result["result"]
        except (httpx.HTTPError, ValueError, TypeError, RecursionError) as exc:
            raise McpUnavailable() from exc

    def initialize(self):
        reply = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                           "clientInfo": {"name": "descriptionless-broker", "version": "1"}})
        capability = reply.get("capabilities")
        info = reply.get("serverInfo")
        if (reply.get("protocolVersion") != PROTOCOL or not isinstance(capability, dict)
                or not isinstance(capability.get("tools"), dict) or not isinstance(info, dict)
                or any(not isinstance(info.get(key), str) or len(info[key]) > 96 for key in ("name", "version"))):
            raise McpUnavailable()
        self.info = {key: info[key] for key in ("name", "version")}
        self.request("notifications/initialized", notification=True)

    def tools(self):
        result = self.request("tools/list", {})
        rows = result.get("tools")
        if (not isinstance(rows, list) or len(rows) > 24 or result.get("nextCursor")
                or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                       or len(row["name"]) > 96 or not isinstance(row.get("inputSchema"), dict) for row in rows)
                or len({row["name"] for row in rows}) != len(rows)):
            raise McpUnavailable()
        return rows

    def call(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        error = result.get("isError")
        if (error is not None and error is not False) or not isinstance(result.get("structuredContent"), dict):
            raise McpUnavailable()
        return result["structuredContent"]


class McpGovernance:
    def __init__(self, store, root, servers):
        self.store = store
        self.root = root
        self.servers = {server.id: server for server in servers}
        if len(self.servers) != len(servers):
            raise ValueError("Duplicate MCP server ID")

    @classmethod
    def load(cls, store, root):
        config = root / "mcp-servers.json"
        if config.exists() or config.is_symlink():
            ensure_private_file(config)
        model = TypeAdapter(Annotated[list[AllowedServer], Field(max_length=6)])
        return cls(store, root, parse_config(config, model.validate_python, list))

    @contextmanager
    def connect(self, server):
        path = self.root / f"mcp-{server.id}.token"
        try:
            ensure_private_file(path)
            with path.open("r", encoding="ascii") as file:
                token = file.read(128).strip()
            if not TOKEN.fullmatch(token):
                raise McpUnavailable()
        except (OSError, RuntimeError, ValueError) as exc:
            raise McpUnavailable() from exc
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
            "Accept-Encoding": "identity",
        }
        with httpx.Client(headers=headers, timeout=httpx.Timeout(3, connect=2),
                          follow_redirects=False, trust_env=False) as http:
            client = HttpMcp(http, server.endpoint)
            client.initialize()
            yield client

    @staticmethod
    def heads(db):
        return db.execute("""SELECT m.* FROM mcp_mapping_revisions AS m JOIN (
            SELECT adapter_id, MAX(revision) AS revision FROM mcp_mapping_revisions GROUP BY adapter_id
            ) AS latest USING (adapter_id, revision) ORDER BY m.adapter_id""").fetchall()

    def entries(self, db):
        return [
            AdapterSpec(adapter_id=row["adapter_id"], operation="create-ticket", implementation="mcp-ticket",
                        priority=row["priority"], mapping_revision=row["revision"])
            for row in self.heads(db) if row["enabled"] and row["server_id"] in self.servers
            and row["server_binding"] == self.servers[row["server_id"]].binding
        ]

    def overview(self, principal):
        self.check_reviewer(principal)
        with self.store.connect() as db:
            check_actor(db, principal)
            return {
                "servers": [{"id": server.id, "endpoint": server.endpoint} for server in self.servers.values()],
                "mappings": [self.public_row(row) for row in self.heads(db)],
            }

    @staticmethod
    def public_row(row):
        return {key: bool(row[key]) if key == "enabled" else row[key] for key in (
            "adapter_id", "server_id", "tool", "schema_sha256", "revision", "priority", "enabled", "actor", "recorded_at",
        )}

    @staticmethod
    def check_reviewer(principal):
        if principal.role != "reviewer":
            raise GatewayError(403, "registry_review_required")

    def discovery(self, client):
        rows = client.tools()
        contract = interface(rows)
        compatible = contract == interface(ticket_tools())
        revision = fingerprint({"server": client.info, "interface": contract})
        credential = client.http.headers["Authorization"].split(" ", 1)[1]
        return [
            {"name": row["name"].replace(credential, "[redacted]"),
             "description": str(row.get("description", "")).replace(credential, "[redacted]")[:512],
             "input_schema": strict_json(canonical(row["inputSchema"]).replace(credential, "[redacted]")),
             "schema_sha256": revision if row["name"] == CREATE else fingerprint(row["inputSchema"]),
             "compatible": compatible and row["name"] == CREATE}
            for row in rows
        ]

    def inspect(self, principal, server_id):
        self.check_reviewer(principal)
        with self.store.connect() as db:
            check_actor(db, principal)
        server = self.servers.get(server_id)
        if server is None:
            raise GatewayError(404, "mcp_server_not_allowlisted")
        try:
            with self.connect(server) as client:
                return {"tools": self.discovery(client), "argument_mapping": TITLE_MAPPING}
        except (McpUnavailable, ValueError, TypeError, RecursionError) as exc:
            raise GatewayError(502, "mcp_discovery_failed") from exc

    def register(self, principal, request: MappingRequest):
        self.check_reviewer(principal)
        server = self.servers.get(request.server_id)
        if server is None:
            raise GatewayError(404, "mcp_server_not_allowlisted")
        adapter_id = "mcp-" + server.id
        if request.enabled:
            active = self.inspect(principal, request.server_id)
            tool = next((item for item in active["tools"] if item["name"] == request.tool), None)
            if tool is None or not tool["compatible"]:
                raise GatewayError(422, "mcp_incompatible_tool")
            if not same_digest(tool["schema_sha256"], request.schema_sha256):
                raise GatewayError(409, "mcp_schema_changed")
        with self.store.connect(write=True) as db:
            check_actor(db, principal)
            prior = db.execute("""SELECT * FROM mcp_mapping_revisions
                WHERE adapter_id=? ORDER BY revision DESC LIMIT 1""", (adapter_id,)).fetchone()
            if not request.enabled and (prior is None or prior["schema_sha256"] != request.schema_sha256):
                raise GatewayError(409, "mcp_unknown_mapping")
            if prior is not None and self.matches(prior, server, request):
                return self.public_row(prior)
            count = db.execute("SELECT COUNT(*) FROM mcp_mapping_revisions").fetchone()[0]
            if count >= 512:
                raise GatewayError(429, "mcp_mapping_history_full")
            cursor = db.execute("""INSERT INTO mcp_mapping_revisions
                (adapter_id,server_id,server_binding,tool,schema_sha256,priority,enabled,actor,recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (adapter_id, server.id, server.binding, request.tool,
                    request.schema_sha256, request.priority, int(request.enabled), principal.name, time.time()))
            row = db.execute("SELECT * FROM mcp_mapping_revisions WHERE revision=?", (cursor.lastrowid,)).fetchone()
        return self.public_row(row)

    @staticmethod
    def matches(prior, server, request):
        return (prior["server_binding"], prior["schema_sha256"], prior["priority"], bool(prior["enabled"])) == (
            server.binding, request.schema_sha256, request.priority, request.enabled)

    def execute(self, row, intent):
        with self.store.connect() as db:
            current = next((item for item in self.heads(db) if item["revision"] == row["mapping_revision"]), None)
        server = self.servers.get(current["server_id"]) if current else None
        if current is None or not current["enabled"] or server is None or server.binding != current["server_binding"]:
            raise Rejected("mcp_mapping_revoked")
        dispatched = False
        try:
            with self.connect(server) as client:
                tool = next((item for item in self.discovery(client) if item["name"] == current["tool"]), None)
                if not tool or not tool["compatible"] or not same_digest(tool["schema_sha256"], current["schema_sha256"]):
                    raise Rejected("mcp_schema_changed")
                arguments = TicketArguments(execution_id=row["id"], title=intent.title, detail=intent.detail)
                dispatched = True
                result = TicketReceipt.model_validate(client.call(CREATE, arguments.model_dump()))
                evidence = ServiceTicket.model_validate(client.call(READ, {"execution_id": row["id"]}))
                if evidence.model_dump(exclude={"title", "detail"}) != result.model_dump() or (
                    evidence.title, evidence.detail
                ) != (intent.title, intent.detail):
                    raise Uncertain("mcp_readback_mismatch")
                return result.model_dump()
        except (McpUnavailable, ValidationError, ValueError, TypeError, RecursionError) as exc:
            if dispatched:
                raise Uncertain("mcp_outcome_unconfirmed") from exc
            raise Rejected("mcp_unavailable_before_dispatch") from exc

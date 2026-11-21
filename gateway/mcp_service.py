"""Standalone loopback MCP ticket service with its own private SQLite runtime."""
import argparse
import os
import secrets
import sqlite3
from asyncio import timeout
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ensure_private_directory, ensure_private_file, same_digest
from .contracts import TicketReceipt, strict_json
from .mcp_bridge import CREATE, PROTOCOL, READ, TOKEN, ServiceTicket, TicketArguments, TicketReadArguments, ticket_tools


def initialize(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    ensure_private_directory(root)
    db_path = root / "tickets.sqlite3"
    token_file = root / "token"
    if not db_path.exists():
        fd = os.open(db_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    ensure_private_file(db_path)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS tickets (
            execution_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, detail TEXT NOT NULL
        )""")
    if not token_file.exists():
        fd = os.open(token_file, os.O_CREAT | os.O_WRONLY | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(secrets.token_urlsafe(32) + "\n")
    ensure_private_file(token_file)


def rpc_error(identifier, code, message):
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def handle(root, method, params):
    if method == "initialize":
        return {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                "serverInfo": {"name": "descriptionless-local-tickets", "version": "1"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": ticket_tools()}
    if method != "tools/call":
        raise LookupError()
    name = params.get("name")
    if name == CREATE:
        arguments = TicketArguments.model_validate(params.get("arguments"))
    elif name == READ:
        arguments = TicketReadArguments.model_validate(params.get("arguments"))
    else:
        raise LookupError()
    with closing(sqlite3.connect((root / "tickets.sqlite3").as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
        db.row_factory = sqlite3.Row
        if name == CREATE:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM tickets WHERE execution_id=?", (arguments.execution_id,)).fetchone()
        if name == CREATE and row is None:
            if db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] >= 10_000:
                return {"content": [{"type": "text", "text": "Ticket capacity reached"}], "isError": True}
            db.execute("INSERT INTO tickets VALUES (?,?,?,?)",
                       (arguments.execution_id, "tkt-" + arguments.execution_id, arguments.title, arguments.detail))
            row = db.execute("SELECT * FROM tickets WHERE execution_id=?", (arguments.execution_id,)).fetchone()
        if row is None or (name == CREATE and (row["title"], row["detail"]) != (arguments.title, arguments.detail)):
            return {"content": [{"type": "text", "text": "Ticket unavailable or argument conflict"}], "isError": True}
        fields = {"ticket_id": row["ticket_id"], "status": "open", "revision": 1}
        ticket = TicketReceipt(**fields) if name == CREATE else ServiceTicket(**fields, title=row["title"], detail=row["detail"])
    return {"content": [{"type": "text", "text": ticket.model_dump_json()}],
            "structuredContent": ticket.model_dump(), "isError": False}


def create_service(root):
    from starlette.concurrency import run_in_threadpool

    root = root.absolute()
    ensure_private_directory(root)
    ensure_private_file(root / "tickets.sqlite3")
    ensure_private_file(root / "token")
    with (root / "token").open(encoding="ascii") as file:
        token = file.read(128).strip()
    if not TOKEN.fullmatch(token):
        raise ValueError("Invalid MCP service token")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"], www_redirect=False)

    @app.post("/mcp")
    async def dispatch(request: Request):
        if request.client is None or request.client.host != "127.0.0.1" or request.headers.get("origin") is not None:
            return Response(status_code=403)
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or not TOKEN.fullmatch(authorization[7:]) or not same_digest(authorization[7:], token):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return Response(status_code=415)
        try:
            data = bytearray()
            async with timeout(5):
                async for chunk in request.stream():
                    if len(data) + len(chunk) > 16384:
                        return Response(status_code=413)
                    data.extend(chunk)
            body = strict_json(data)
        except (ValueError, RecursionError, TimeoutError):
            return JSONResponse(rpc_error(None, -32700, "Invalid JSON"), status_code=400)
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
            return JSONResponse(rpc_error(None, -32600, "Invalid request"), status_code=400)
        identifier, method = body.get("id"), body["method"]
        if method != "initialize" and request.headers.get("MCP-Protocol-Version", PROTOCOL) != PROTOCOL:
            return JSONResponse(rpc_error(identifier, -32600, "Unsupported protocol"), status_code=400)
        if "id" not in body:
            return Response(status_code=202 if method in {"notifications/initialized", "notifications/cancelled"} else 400)
        if type(identifier) not in (int, str) or not isinstance(body.get("params", {}), dict):
            return JSONResponse(rpc_error(None, -32600, "Invalid request"), status_code=400)
        try:
            result = await run_in_threadpool(handle, root, method, body.get("params", {}))
            return {"jsonrpc": "2.0", "id": identifier, "result": result}
        except LookupError:
            return rpc_error(identifier, -32601, "Unknown method or tool")
        except ValidationError:
            return rpc_error(identifier, -32602, "Invalid parameters")
        except sqlite3.Error:
            return rpc_error(identifier, -32603, "Ticket storage unavailable")

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "serve"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = args.root.absolute()
    if args.command == "init":
        initialize(root)
        print(f"Initialized local ticket service at {root}")
    else:
        uvicorn.run(create_service(root), host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False,
                    limit_concurrency=32)


if __name__ == "__main__":
    main()

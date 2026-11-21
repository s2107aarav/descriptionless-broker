"""Authenticated local demonstration API. All effects are simulated."""
import json
import os
import secrets
from pathlib import Path
from typing import Annotated
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from .agent import run_agent
from .registry import default_registry
from .store import AuditStore, DATA_DIR
from .policy import ExecutionPolicy


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    user_text: str = Field(min_length=1, max_length=12000)


class BodyLimit:
    """Bound raw HTTP input even without a Content-Length header."""
    def __init__(self, app, limit=65536):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        data = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            data.extend(message.get("body", b""))
            if len(data) > self.limit:
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"Request too large"}'})
                return
            if not message.get("more_body", False):
                break
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(data), "more_body": False}
            return await receive()
        await self.app(scope, bounded_receive, send)


def create_app(*, registry=None, store=None, policy=None, provider=None, token=None, eval_path=None):
    app = FastAPI(title="Descriptionless Broker — local simulation", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
                       allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"])
    active_registry = registry if registry is not None else default_registry
    active_policy = policy if policy is not None else ExecutionPolicy()
    active_store = store

    def get_store():
        nonlocal active_store
        if active_store is None:
            active_store = AuditStore(capture_prompts=os.getenv("BROKER_CAPTURE_PROMPTS") == "1")
        return active_store

    def authenticate(authorization: Annotated[str | None, Header()] = None):
        expected = token if token is not None else os.getenv("BROKER_API_TOKEN")
        if not expected or len(expected) < 24:
            raise HTTPException(503, "Configure a private BROKER_API_TOKEN of at least 24 characters")
        if not authorization or not secrets.compare_digest(authorization, "Bearer " + expected):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})
        return "local-owner"

    @app.post("/run")
    def api_run(req: RunRequest,
                idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")],
                principal=Depends(authenticate)):
        res = run_agent(req.user_text, registry=active_registry, store=get_store(), policy=active_policy,
                        provider=provider, principal=principal, idempotency_key=idempotency_key)
        return {k: res.get(k) for k in ("final_result", "error", "turn_count", "history")}

    @app.get("/ledger")
    def api_ledger(principal=Depends(authenticate)):
        return get_store().ledger(principal)

    @app.get("/prompts")
    def api_prompts(principal=Depends(authenticate)):
        return get_store().prompts(principal)

    @app.get("/eval-summary")
    def api_eval_summary(principal=Depends(authenticate)):
        path = Path(eval_path or Path(DATA_DIR) / "evaluation.json")
        if not path.exists():
            return {"status": "not_run", "security_evaluation": "not_run", "reason": "No current versioned evaluation artifact"}
        report = json.loads(path.read_text())
        if report.get("schema_version") != 2:
            raise HTTPException(409, "Unsupported historical evaluation artifact")
        return {k: v for k, v in report.items() if k != "rows"}

    return app


app = create_app()

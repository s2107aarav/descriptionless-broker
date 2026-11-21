"""Authenticated, loopback-only operational API."""
import logging
import re
import sqlite3
import threading
from asyncio import timeout
from contextlib import asynccontextmanager
from functools import partial
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings, load_policy, load_registry
from .contracts import Closed, Digest, INTENT, strict_json
from .engine import Gateway, GatewayError
from .labs import ComparisonRequest, LabReports, RecoveryRequest, provenance, run_comparison, run_recovery
from .mcp_bridge import MappingRequest, McpGovernance
from .planner import InvalidPlannerOutput, OllamaProvider, Planner, PlannerUnavailable
from .store import Store, runtime_lock
from .workflows import WorkflowPlan, Workflows, propose_workflow
from .workspace import Workspace

LOG = logging.getLogger(__name__)
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
EXECUTION_ID = r"^[0-9a-f]{32}$"
ExecutionId = Annotated[str, PathParam(pattern=EXECUTION_ID)]
BODY_LIMIT = 512 * 1024


class ProposalRequest(Closed):
    text: str = Field(min_length=1, max_length=16000)


class Decision(Closed):
    decision: Literal["approve", "deny"]
    request_hash: Digest
    policy_revision: Digest


class LocalResponses:
    """No caching for private API replies and a bound on the bytes consumed."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"cache-control", b"no-store")]
            await send(message)

        client = scope.get("client")
        try:
            loopback = client is not None and ip_address(client[0]).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            response = JSONResponse({"detail": "loopback_required"}, status_code=403)
            return await response(scope, receive, private_send)

        if scope["method"] != "POST":
            return await self.app(scope, receive, private_send)
        buffer = bytearray()
        try:
            async with timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(buffer) + len(chunk) > BODY_LIMIT:
                        response = JSONResponse({"detail": "body_too_large"}, status_code=413)
                        return await response(scope, receive, private_send)
                    buffer.extend(chunk)
                    if not message.get("more_body"):
                        break
        except TimeoutError:
            response = JSONResponse({"detail": "body_receive_timeout"}, status_code=408)
            return await response(scope, receive, private_send)
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(buffer), "more_body": False}

        await self.app(scope, replay, private_send)


async def parse_body(request: Request, *, validator):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "expected_application_json")
    try:
        return validator(strict_json(await request.body()))
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(422, "invalid_json_or_schema") from exc


parse_intent = partial(parse_body, validator=INTENT.validate_python)
parse_proposal = partial(parse_body, validator=ProposalRequest.model_validate)
parse_decision = partial(parse_body, validator=Decision.model_validate)
parse_workflow = partial(parse_body, validator=WorkflowPlan.model_validate)
parse_mapping = partial(parse_body, validator=MappingRequest.model_validate)
parse_comparison = partial(parse_body, validator=ComparisonRequest.model_validate)
parse_recovery = partial(parse_body, validator=RecoveryRequest.model_validate)


class Worker:
    def __init__(self, gateway, workflows=None):
        self.gateway = gateway
        self.workflows = workflows
        self.stopping = threading.Event()
        self.wakeup = threading.Event()
        self.healthy = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="intent-worker")

    def loop(self):
        needs_recovery = False
        while not self.stopping.is_set():
            try:
                if needs_recovery:
                    self.gateway.recover()
                    needs_recovery = False
                if self.workflows is not None:
                    self.workflows.tick()
                worked = self.gateway.run_once()
                self.healthy.set()
                if worked:
                    continue
            except Exception as exc:
                self.healthy.clear()
                needs_recovery = True
                LOG.error("Local worker interrupted by %s", type(exc).__name__)
            self.wakeup.wait(0.25)
            self.wakeup.clear()

    def __enter__(self):
        self.healthy.set()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopping.set()
        self.wakeup.set()
        self.thread.join()


def historical_summary():
    root = Path(__file__).resolve().parents[1] / "evidence" / "hardening-2026-10-13"
    reports = {}
    metrics = ("clean_utility_percent", "baseline_clean_utility_percent", "argument_invariance_percent",
               "prompt_metadata_hits", "baseline_attack_success_percent")
    for key, filename in (("offline", "offline-evaluation.json"), ("local_model", "local-model-traced.json")):
        try:
            with (root / filename).open("rb") as source:
                raw = source.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("Oversized historical report")
            report = strict_json(raw)
            if report["schema_version"] != 2 or report["status"] not in {"passed", "invalid"}:
                raise ValueError("Unrecognized historical report")
            summary = {field: report["metrics"][field] for field in metrics}
            if any(value is not None and type(value) not in {float, int} for value in summary.values()):
                raise ValueError("Invalid historical metrics")
            reason = report.get("reason")
            reports[key] = {"status": report["status"], "metrics": summary,
                            "reason": reason if isinstance(reason, str) and len(reason) <= 1000 else None}
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            reports[key] = {"status": "unavailable", "metrics": {}, "reason": "Historical report unavailable"}
    return reports


def create_app(settings=None, *, planner=None):
    settings = settings if settings is not None else Settings.from_env()
    slots = threading.BoundedSemaphore(2)
    lab_slot = threading.BoundedSemaphore(1)
    discovery_slot = threading.BoundedSemaphore(2)

    @asynccontextmanager
    async def lifespan(app):
        with runtime_lock(settings.root):
            store = Store(settings.root)
            store.migrate()
            with Workspace(settings.root) as workspace:
                mcp = McpGovernance.load(store, settings.root)
                gateway = Gateway(store, load_policy(settings.root), load_registry(settings.root), workspace, mcp=mcp)
                gateway.recover()
                app.state.gateway = gateway
                app.state.mcp = mcp
                app.state.workflows = Workflows(gateway)
                app.state.labs = LabReports(store)
                app.state.research = historical_summary()
                with Worker(gateway, app.state.workflows) as worker:
                    app.state.worker = worker
                    yield

    app = FastAPI(title="Descriptionless Broker Gateway", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"], www_redirect=False)
    app.add_middleware(LocalResponses)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.frontend_origins), allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type", "Idempotency-Key"], max_age=600)
    enabled_planner = planner if planner is not None else (
        Planner(OllamaProvider(settings.ollama_url, settings.model)) if settings.model else None
    )

    @app.exception_handler(GatewayError)
    def gateway_error(_request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse({"detail": exc.code}, status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    def validation_error(_request, _exc):
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    @app.exception_handler(sqlite3.Error)
    def storage_error(_request, exc):
        LOG.error("Gateway storage unavailable (%s)", type(exc).__name__)
        return JSONResponse({"detail": "storage_unavailable"}, status_code=503)

    def authenticated(request: Request, authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer ") or not TOKEN.fullmatch(authorization[7:]):
            raise GatewayError(401, "authentication_required")
        principal = request.app.state.gateway.store.authenticate(authorization[7:])
        if principal is None:
            raise GatewayError(401, "authentication_required")
        return principal

    @app.get("/healthz")
    def health():
        return {"status": "alive"}

    @app.get("/readyz")
    def readiness(request: Request):
        worker = request.app.state.worker
        if not worker.thread.is_alive() or not worker.healthy.is_set():
            raise HTTPException(503, "worker_unavailable")
        with request.app.state.gateway.store.connect() as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ready"}

    @app.get("/api/v1/session")
    def session(principal=Depends(authenticated)):
        return {"principal": principal.name, "role": principal.role, "scope": principal.scope}

    @app.post("/api/v1/proposals")
    def propose(principal=Depends(authenticated), body=Depends(parse_proposal)):
        if principal.role == "reviewer":
            raise HTTPException(403, "proposal_forbidden")
        if not body.text.strip():
            raise HTTPException(422, "empty_request")
        if enabled_planner is None:
            raise HTTPException(503, "planner_not_configured")
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "planner_at_capacity")
        try:
            intent = enabled_planner.propose(body.text)
        except PlannerUnavailable as exc:
            raise HTTPException(503, "planner_unavailable") from exc
        except InvalidPlannerOutput as exc:
            raise HTTPException(502, "planner_output_invalid") from exc
        finally:
            slots.release()
        return {"status": "proposed" if intent is not None else "clarification_required",
                "intent": intent.model_dump(exclude_none=True) if intent is not None else None}

    @app.post("/api/v1/executions")
    def submit(request: Request, key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
               principal=Depends(authenticated), intent=Depends(parse_intent)):
        execution, new = request.app.state.gateway.submit(principal, key, intent)
        request.app.state.worker.wakeup.set()
        return JSONResponse(execution, status_code=201 if new else 200)

    @app.get("/api/v1/executions")
    def executions(request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50,
                   before: Annotated[str | None, Query(pattern=EXECUTION_ID)] = None,
                   principal=Depends(authenticated)):
        return request.app.state.gateway.list_executions(principal, limit=limit, before=before)

    @app.get("/api/v1/executions/{execution_id}")
    def execution(request: Request, execution_id: ExecutionId, principal=Depends(authenticated)):
        return request.app.state.gateway.get_execution(principal, execution_id)

    @app.get("/api/v1/executions/{execution_id}/events")
    def events(request: Request, execution_id: ExecutionId, principal=Depends(authenticated)):
        return request.app.state.gateway.get_execution(principal, execution_id, events=True)

    @app.get("/api/v1/approvals")
    def approvals(request: Request, before: Annotated[str | None, Query(pattern=EXECUTION_ID)] = None,
                  principal=Depends(authenticated)):
        return request.app.state.gateway.list_executions(principal, limit=100, before=before, approval_queue=True)

    @app.post("/api/v1/approvals/{execution_id}/decision")
    def decision(request: Request, execution_id: ExecutionId, principal=Depends(authenticated),
                 review=Depends(parse_decision)):
        execution = request.app.state.gateway.decision(principal, execution_id, review)
        request.app.state.worker.wakeup.set()
        return execution

    @app.get("/api/v1/inbox")
    def inbox(request: Request, principal=Depends(authenticated)):
        return request.app.state.gateway.inbox(principal)

    @app.get("/api/v1/research")
    def research(request: Request, _principal=Depends(authenticated)):
        return request.app.state.research

    @app.post("/api/v1/workflows/compile")
    def workflow_compile(request: Request, principal=Depends(authenticated), plan=Depends(parse_workflow)):
        _, order = request.app.state.workflows.compile(principal, plan)
        return {"order": order}

    @app.post("/api/v1/workflows/proposals")
    def workflow_proposal(request: Request, principal=Depends(authenticated), body=Depends(parse_proposal)):
        if principal.role == "reviewer":
            raise HTTPException(403, "proposal_forbidden")
        if not body.text.strip():
            raise HTTPException(422, "empty_request")
        provider = getattr(enabled_planner, "provider", None)
        if provider is None:
            raise HTTPException(503, "planner_not_configured")
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "planner_at_capacity")
        try:
            plan = propose_workflow(provider, body.text)
        except PlannerUnavailable as exc:
            raise HTTPException(503, "planner_unavailable") from exc
        except InvalidPlannerOutput as exc:
            raise HTTPException(502, "planner_output_invalid") from exc
        finally:
            slots.release()
        if plan is None:
            return {"status": "clarification_required", "plan": None, "order": []}
        _, order = request.app.state.workflows.compile(principal, plan)
        return {"status": "proposed", "plan": plan.model_dump(), "order": order}

    @app.post("/api/v1/workflows")
    def workflow_submit(request: Request, key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
                        principal=Depends(authenticated), plan=Depends(parse_workflow)):
        workflow, new = request.app.state.workflows.submit(principal, key, plan)
        request.app.state.worker.wakeup.set()
        return JSONResponse(workflow, status_code=201 if new else 200)

    @app.get("/api/v1/workflows")
    def workflow_list(request: Request, principal=Depends(authenticated)):
        return request.app.state.workflows.get(principal)

    @app.get("/api/v1/workflows/{workflow_id}")
    def workflow_detail(request: Request, workflow_id: ExecutionId, principal=Depends(authenticated)):
        return request.app.state.workflows.get(principal, workflow_id)

    @app.get("/api/v1/mcp")
    def mcp_overview(request: Request, principal=Depends(authenticated)):
        return request.app.state.mcp.overview(principal)

    @app.get("/api/v1/mcp/servers/{server_id}/tools")
    def mcp_discover(request: Request, server_id: Annotated[str, PathParam(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")],
                     principal=Depends(authenticated)):
        if not discovery_slot.acquire(blocking=False):
            raise HTTPException(429, "mcp_discovery_at_capacity")
        try:
            return request.app.state.mcp.inspect(principal, server_id)
        finally:
            discovery_slot.release()

    @app.post("/api/v1/mcp/mappings")
    def mcp_register(request: Request, principal=Depends(authenticated), mapping=Depends(parse_mapping)):
        if not discovery_slot.acquire(blocking=False):
            raise HTTPException(429, "mcp_discovery_at_capacity")
        try:
            result = request.app.state.mcp.register(principal, mapping)
            request.app.state.worker.wakeup.set()
            return result
        finally:
            discovery_slot.release()

    @app.get("/api/v1/labs")
    def lab_list(request: Request, principal=Depends(authenticated)):
        return request.app.state.labs.get(principal)

    def run_lab(request, principal, kind, arguments, run):
        if not lab_slot.acquire(blocking=False):
            raise HTTPException(429, "lab_at_capacity")
        try:
            try:
                report = run()
            except GatewayError as exc:
                if exc.status < 500:
                    raise
                report = {**arguments.model_dump(), "status": "invalid", "reason": exc.code,
                          "provenance": provenance(kind)}
            return request.app.state.labs.record(principal, kind, report)
        finally:
            lab_slot.release()

    @app.post("/api/v1/labs/recovery")
    def recovery_lab(request: Request, principal=Depends(authenticated), scenario=Depends(parse_recovery)):
        return run_lab(request, principal, "recovery", scenario, lambda: run_recovery(scenario))

    @app.post("/api/v1/labs/comparison")
    def comparison_lab(request: Request, principal=Depends(authenticated), scenario=Depends(parse_comparison)):
        return run_lab(request, principal, "comparison", scenario, lambda: run_comparison(scenario, settings))

    @app.get("/api/v1/labs/{run_id}")
    def lab_detail(request: Request, run_id: ExecutionId, principal=Depends(authenticated)):
        return request.app.state.labs.get(principal, run_id)

    return app


app = create_app()

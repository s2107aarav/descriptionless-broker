"""The added features exercise the ordinary gateway and disposable real effects."""
import json
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from gateway import mcp_service
from gateway.api import create_app
from gateway.config import AdapterSpec, Registry, Settings, default_adapters
from gateway.engine import GatewayError
from gateway.labs import (
    ComparisonRequest, RecoveryRequest, REQUESTER, REVIEWER, approve, disposable, run_comparison, run_recovery,
)
from gateway.mcp_bridge import AllowedServer, HttpMcp, MappingRequest, McpGovernance, McpUnavailable, PROTOCOL
from gateway.store import SCHEMA, SCHEMA_VERSION, Store, purge_receipts, verify_audit
from gateway.workflows import InvalidWorkflow, Workflows, compile_plan

AGENT_TOKEN_ROLE = "agent"


def incident():
    return {"schema_version": "workflow-v1", "nodes": [
        {"id": "notify", "operation": "send-message", "depends_on": ["ticket", "report"],
         "arguments": {"recipient": "reviewer", "body": {"join": ["Review ", {"step": "ticket", "field": "ticket_id"}]}}},
        {"id": "ticket", "operation": "create-ticket", "arguments": {"title": "Latency", "detail": "Check queue"}},
        {"id": "report", "operation": "write-file", "depends_on": ["ticket"], "arguments": {
            "path": "reports/incident.txt", "content": {"ref": {"step": "ticket", "field": "ticket_id"}},
        }},
    ]}


def test_migrate_populated_legacy_runtime(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    db_path = root / "gateway.sqlite3"
    db_path.touch(mode=0o600)
    with sqlite3.connect(db_path) as db:
        for definition in SCHEMA:
            db.execute(definition)
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO services VALUES ('demo','legacy','Before','Retain this record')")
        db.execute("INSERT INTO principals VALUES ('requester','digest','requester','demo',1)")
        db.execute("""INSERT INTO executions (id,requester,scope,request_key,request_hash,operation,schema_version,
            policy_revision,status,created_at,updated_at) VALUES ('stale','requester','demo','legacy-key','hash',
            'create-ticket','v2','legacy-policy','awaiting_approval',1,1)""")
    store = Store(root)
    store.migrate()
    store.migrate()
    with store.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("SELECT name FROM services WHERE id='legacy'").fetchone()[0] == "Before"
        assert db.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0
        assert db.execute("SELECT planned_registry_revision FROM executions WHERE id='stale'").fetchone()[0] == "legacy-unpinned"


def test_workflow_reference_validation_and_topological_oracle():
    edges = [(a, b) for b in range(4) for a in range(b)]
    for mask in range(1 << len(edges)):
        parents = {f"n{i}": [] for i in range(4)}
        for index, (parent, child) in enumerate(edges):
            if mask & (1 << index):
                parents[f"n{child}"].append(f"n{parent}")
        plan = {"schema_version": "workflow-v1", "nodes": [
            {"id": name, "operation": "lookup-record", "depends_on": dependencies,
             "arguments": {"collection": "services", "record_id": "gateway"}}
            for name, dependencies in reversed(list(parents.items()))
        ]}
        _, order, _ = compile_plan(plan)
        assert set(order) == parents.keys()
        assert all(order.index(parent) < order.index(child) for child, parts in parents.items() for parent in parts)
    plan = incident()
    plan["nodes"][1]["depends_on"] = ["notify"]
    with pytest.raises(InvalidWorkflow, match="workflow_cycle"):
        compile_plan(plan)
    plan = incident()
    plan["nodes"][2]["arguments"]["content"]["ref"]["field"] = "revision"
    with pytest.raises(InvalidWorkflow, match="workflow_reference_type_mismatch"):
        compile_plan(plan)
    plan["nodes"][2]["arguments"]["content"]["ref"]["step"] = "notify"
    with pytest.raises(InvalidWorkflow, match="workflow_reference_requires_dependency"):
        compile_plan(plan)


def test_workflow_resolves_exact_approval_and_survives_recreation():
    with disposable() as (root, gateway):
        graph = Workflows(gateway)
        with ThreadPoolExecutor(max_workers=5) as workers:
            submissions = list(workers.map(lambda _: graph.submit(REQUESTER, "same-workflow", incident()), range(10)))
        assert sum(new for _, new in submissions) == 1
        identifier = submissions[0][0]["id"]
        assert {row["id"] for row, _ in submissions} == {identifier}
        graph.tick()
        assert not gateway.run_once()
        [ticket] = gateway.list_executions(REVIEWER, approval_queue=True)
        approve(gateway, ticket["id"])
        gateway.run_once()
        assert purge_receipts(gateway.store, time.time() + 1) == 0  # The dependent lookup has not captured its source.
        graph = Workflows(gateway)
        graph.tick()
        [report] = gateway.list_executions(REVIEWER, approval_queue=True)
        ticket_id = "tkt-" + ticket["id"]
        assert report["requested_intent"]["content"] == ticket_id
        approve(gateway, report["id"])
        gateway.run_once()
        graph.tick()
        [message] = gateway.list_executions(REVIEWER, approval_queue=True)
        assert message["requested_intent"]["body"] == "Review " + ticket_id
        approve(gateway, message["id"])
        gateway.run_once()
        graph.tick()
        assert graph.get(REQUESTER, identifier)["status"] == "succeeded"
        assert (root / "files/reports/incident.txt").read_text() == ticket_id
        assert len(gateway.inbox(REVIEWER)) == 1
        assert graph.submit(REQUESTER, "same-workflow", incident())[1] is False
        assert verify_audit(gateway.store)["executions"] == 3
        with gateway.store.connect() as db:
            assert all("arguments" not in json.loads(row[0]) for row in db.execute(
                "SELECT definition_json FROM workflow_nodes"
            ))
        with pytest.raises(GatewayError, match="reserved_idempotency_key"):
            gateway.submit(REQUESTER, "wf_spoofed", ticket["requested_intent"])


def test_workflow_partial_and_revision_are_real():
    with disposable() as (root, gateway):
        graph = Workflows(gateway)
        (root / "files/reports/incident.txt").write_text("already there")
        workflow, _ = graph.submit(REQUESTER, "partial-flow", incident())
        for _ in range(2):
            graph.tick()
            [step] = gateway.list_executions(REVIEWER, approval_queue=True)
            approve(gateway, step["id"])
            gateway.run_once()
        graph.tick()
        finished = graph.get(REQUESTER, workflow["id"])
        assert finished["status"] == "partial"
        assert next(node for node in finished["nodes"] if node["id"] == "notify")["status"] == "skipped"
        assert not gateway.inbox(REVIEWER)
        assert (root / "files/reports/incident.txt").read_text() == "already there"

        workflow, _ = graph.submit(REQUESTER, "stale-flow", incident())
        graph.tick()
        [step] = gateway.list_executions(REVIEWER, approval_queue=True)
        gateway.registry = Registry([*default_adapters(), AdapterSpec(adapter_id="earlier", operation="create-ticket",
                                                                      implementation="create-ticket", priority=0)])
        gateway.run_once()
        graph.tick()
        assert gateway.get_execution(REVIEWER, step["id"])["reason"] == "registry_changed"
        assert graph.get(REQUESTER, workflow["id"])["status"] == "failed"


def test_workflow_marks_permanent_execution_capacity_as_failure(monkeypatch):
    with disposable() as (_, gateway):
        graph = Workflows(gateway)
        workflow, _ = graph.submit(REQUESTER, "bounded-flow", incident())
        monkeypatch.setattr("gateway.engine.MAX_EXECUTIONS", 0)
        graph.tick()
        result = graph.get(REQUESTER, workflow["id"])
        assert result["status"] == "failed"
        assert next(node for node in result["nodes"] if node["id"] == "ticket")["reason"] == "execution_history_full"


@pytest.fixture
def mcp_runtime(tmp_path):
    service_dir = tmp_path / "ticket-service"
    mcp_service.initialize(service_dir)
    token = (service_dir / "token").read_text().strip()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(mcp_service.create_service(service_dir), log_level="critical",
                                              proxy_headers=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield service_dir, token, AllowedServer(id="tickets", port=port)
        finally:
            server.should_exit = True
            thread.join(6)


def configure_mcp(root, token, server, gateway):
    path = root / "mcp-tickets.token"
    path.write_text(token)
    path.chmod(0o600)
    client = McpGovernance(gateway.store, root, [server])
    gateway.mcp = client
    [create, _] = client.inspect(REVIEWER, server.id)["tools"]
    assert create["compatible"]
    args = MappingRequest(server_id=server.id, tool=create["name"], schema_sha256=create["schema_sha256"], priority=0)
    first = client.register(REVIEWER, args)
    assert client.register(REVIEWER, args)["revision"] == first["revision"]
    return client, first, args


def test_mcp_wire_discovery_and_governed_effect(mcp_runtime):
    service_dir, token, server = mcp_runtime
    # Independent protocol client, separate from the connector under test.
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PROTOCOL}
    with httpx.Client(timeout=3, trust_env=False) as client:
        assert client.post(server.endpoint, headers={**headers, "Origin": "https://other.example"},
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 403
        initialized = client.post(server.endpoint, headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                             "clientInfo": {"name": "independent-test", "version": "1"}}})
        assert initialized.json()["result"]["protocolVersion"] == PROTOCOL
        assert client.post(server.endpoint, headers=headers,
                           json={"jsonrpc":"2.0","method":"notifications/initialized"}).status_code == 202
        listed = client.post(server.endpoint, headers=headers, json={"jsonrpc":"2.0","id":2,"method":"tools/list"})
        assert len(listed.json()["result"]["tools"]) == 2
    with disposable() as (root, gateway):
        governance, mapping, args = configure_mcp(root, token, server, gateway)
        with pytest.raises(GatewayError, match="registry_review_required"):
            governance.overview(REQUESTER)
        with pytest.raises(GatewayError, match="mcp_server_not_allowlisted"):
            governance.inspect(REVIEWER, "unknown")
        with pytest.raises(GatewayError, match="mcp_schema_changed"):
            governance.register(REVIEWER, args.model_copy(update={"schema_sha256": "0" * 64}))
        assert governance.overview(REVIEWER)["mappings"][0]["revision"] == mapping["revision"]
        row, _ = gateway.submit(REQUESTER, "mcp-real-effect", incident()["nodes"][1]["arguments"] |
                                {"schema_version": "v2", "operation": "create-ticket"})
        approve(gateway, row["id"])
        assert gateway.run_once()
        receipt = gateway.get_execution(REVIEWER, row["id"])
        assert receipt["status"] == "succeeded" and receipt["adapter"] == "mcp-tickets"
        assert receipt["mapping_revision"] == mapping["revision"]
        assert verify_audit(gateway.store)["executions"] == 1
        with sqlite3.connect(service_dir / "tickets.sqlite3") as db:
            assert db.execute("SELECT title, detail FROM tickets").fetchall() == [("Latency", "Check queue")]
        assert "adapter" not in gateway.get_execution(REQUESTER, row["id"])
        with gateway.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_unknown_mcp_ack_stops_dependents_and_no_replay(mcp_runtime, monkeypatch):
    service_dir, token, server = mcp_runtime
    original = HttpMcp.call

    def drop_ack(self, name, arguments):
        result = original(self, name, arguments)
        if name == mcp_service.CREATE:
            raise McpUnavailable()
        return result

    monkeypatch.setattr(HttpMcp, "call", drop_ack)
    with disposable() as (root, gateway):
        configure_mcp(root, token, server, gateway)
        workflows = Workflows(gateway)
        workflow, _ = workflows.submit(REQUESTER, "mcp-unknown-workflow", incident())
        workflows.tick()
        [step] = gateway.list_executions(REVIEWER, approval_queue=True)
        approve(gateway, step["id"])
        gateway.run_once()
        gateway.recover()
        workflows.tick()
        assert workflows.get(REQUESTER, workflow["id"])["status"] == "outcome_unknown"
        assert gateway.get_execution(REQUESTER, step["id"])["reason"] == "mcp_outcome_unconfirmed"
        assert not gateway.run_once()
        assert not (root / "files/reports/incident.txt").exists()
        with sqlite3.connect(service_dir / "tickets.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 1
        with gateway.store.connect() as db:
            assert db.execute("SELECT count(*) FROM tickets").fetchone()[0] == 0
            assert db.execute("SELECT attempt FROM executions WHERE id=?", (step["id"],)).fetchone()[0] == 1


def test_schema_drift_is_rejected_before_external_dispatch(mcp_runtime, monkeypatch):
    service_dir, token, server = mcp_runtime
    with disposable() as (root, gateway):
        configure_mcp(root, token, server, gateway)
        record, _ = gateway.submit(REQUESTER, "schema-drift", {"schema_version": "v2", "operation": "create-ticket",
                                                              "title": "Concern", "detail": "Check mapping"})
        approve(gateway, record["id"])
        original = mcp_service.ticket_tools

        def changed():
            tools = original()
            tools[0]["inputSchema"]["properties"]["title"]["maxLength"] = 1
            return tools

        monkeypatch.setattr(mcp_service, "ticket_tools", changed)
        gateway.run_once()
        assert gateway.get_execution(REQUESTER, record["id"])["reason"] == "mcp_schema_changed"
        with sqlite3.connect(service_dir / "tickets.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_discovery_does_not_reflect_service_credential(mcp_runtime, monkeypatch):
    _, token, server = mcp_runtime
    original = mcp_service.ticket_tools

    def descriptive_only():
        rows = original()
        rows[0]["description"] = f"Description {token}"
        rows[0]["inputSchema"]["properties"]["title"]["description"] = token
        return rows

    monkeypatch.setattr(mcp_service, "ticket_tools", descriptive_only)
    with disposable() as (root, gateway):
        governance, _, _ = configure_mcp(root, token, server, gateway)
        result = governance.inspect(REVIEWER, server.id)
        assert token not in json.dumps(result)
        assert result["tools"][0]["compatible"]


@pytest.mark.parametrize("operation,boundary,evidence", [
    ("send-message", "after_claim_commit", "intact"),
    ("send-message", "before_sql_commit", "intact"),
    ("send-message", "after_sql_commit", "intact"),
    ("write-file", "after_claim_commit", "intact"),
    ("write-file", "after_file_stage", "intact"),
    ("write-file", "after_file_publish", "intact"),
    ("write-file", "after_file_commit", "intact"),
    ("write-file", "after_file_publish", "remove-staging"),
])
def test_recovery_oracle(operation, boundary, evidence):
    report = run_recovery(RecoveryRequest(operation=operation, boundary=boundary, evidence=evidence))
    assert report["independent_check_passed"]
    assert report["child_exit_code"] == 71
    if evidence == "remove-staging":
        assert report["after"]["target"] is not None
        assert report["classification"] == "outcome_unknown"


@pytest.mark.parametrize("task", ["ticket", "message"])
def test_counterfactual_uses_isolated_real_state(task):
    report = run_comparison(ComparisonRequest(task=task), Settings(Path("/unused")))
    assert report["metrics"]["correct_effect_cells"] == 4
    assert report["metrics"]["equivalent_initial_world"]
    assert report["metrics"]["protected_context_equal"]
    assert report["metrics"]["conventional_selection_changed"]  # Fixture catalog order, not model security evidence.
    assert report["metrics"]["final_state_equal"]
    assert all(cell["attempted"] for cell in report["cells"])
    assert run_comparison(ComparisonRequest(task=task, mode="live"), Settings(Path("/unused")))["status"] == "not_run"


def test_invalid_live_decisions_are_recorded_without_effect(monkeypatch):
    monkeypatch.setattr("gateway.labs.OllamaProvider.invoke", lambda *_: "[]")
    run = run_comparison(ComparisonRequest(task="ticket", mode="live"), Settings(Path("/unused"), model="fixture"))
    assert len(run["cells"]) == 4
    assert all(cell["status"] == "invalid_decision" and not cell["attempted"] for cell in run["cells"])
    assert all(not cell["observed"]["tickets"] for cell in run["cells"])


def test_lab_api_scope_and_workflow_real_paths(tmp_path):
    root = tmp_path / "operator"
    root.mkdir(mode=0o700)
    store = Store(root)
    store.migrate(create=True)
    credentials = store.provision_demo()
    from gateway.workspace import Workspace
    Workspace(root).initialize()
    requester = {"Authorization": "Bearer " + credentials["requester"]}
    other = {"Authorization": "Bearer " + credentials[AGENT_TOKEN_ROLE]}
    with TestClient(create_app(Settings(root)), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        workflow = client.post("/api/v1/workflows", headers={**requester, "Idempotency-Key": "workflow-submit"}, json=incident())
        assert workflow.status_code == 201
        assert client.get("/api/v1/workflows/" + workflow.json()["id"], headers=other).status_code == 404
        lab = client.post("/api/v1/labs/recovery", headers=requester,
                          json={"operation":"send-message","boundary":"after_sql_commit"})
        assert lab.status_code == 200 and lab.json()["report"]["independent_check_passed"]
        uri = "/api/v1/labs/" + lab.json()["id"]
        assert client.get(uri, headers=other).status_code == 404
        assert client.get(uri, headers=requester).json() == lab.json()
        assert client.get("/api/v1/mcp", headers=other).status_code == 403
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM lab_runs").fetchone()[0] == 1


def test_workflow_proposal_uses_only_trusted_inputs_and_creates_no_execution(tmp_path):
    from gateway.planner import Planner
    from gateway.workspace import Workspace

    prompts = []

    class DraftProvider:
        def invoke(self, messages):
            prompts.append(messages)
            return json.dumps(incident())

    root = tmp_path / "operator"
    root.mkdir(mode=0o700)
    store = Store(root)
    store.migrate(create=True)
    tokens = store.provision_demo()
    Workspace(root).initialize()
    (root / "registry.json").write_text(json.dumps([{"adapter_id": "local-ticket", "operation": "create-ticket",
                                                  "implementation": "create-ticket", "description": "INTERNAL_LABEL"}]))
    with TestClient(create_app(Settings(root), planner=Planner(DraftProvider())), base_url="http://127.0.0.1",
                    client=("127.0.0.1", 50000)) as client:
        headers = {"Authorization": "Bearer " + tokens["requester"]}
        result = client.post("/api/v1/workflows/proposals", headers=headers, json={"text": "Record an incident and notify the reviewer"})
        assert result.status_code == 200
        assert result.json()["order"] == ["ticket", "report", "notify"]
        assert "INTERNAL_LABEL" not in json.dumps(prompts)
        assert client.get("/api/v1/workflows", headers=headers).json() == []
        assert client.get("/api/v1/executions", headers=headers).json() == []

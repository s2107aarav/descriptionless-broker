"""Product-mode tests use actual SQLite transactions and workspace files."""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from gateway.api import create_app
from gateway.config import Principal, Settings, Policy, Registry, AdapterSpec, default_adapters
from gateway.contracts import INTENT
from gateway.engine import Gateway, GatewayError
from gateway.planner import Planner
from gateway.store import Store, runtime_lock, verify_audit
from gateway.workspace import Workspace

REQUESTER = Principal("requester", "requester")
REVIEWER = Principal("reviewer", "reviewer")
AGENT = Principal("agent", "agent")


def action(operation, **fields):
    return {"schema_version": "v2", "operation": operation, **fields}


def decision(row, value="approve", **changes):
    return SimpleNamespace(**({"decision": value, "request_hash": row["request_hash"],
                              "policy_revision": row["policy_revision"]} | changes))


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    store = Store(root)
    store.migrate(create=True)
    tokens = store.provision_demo()
    workspace = Workspace(root)
    workspace.initialize()
    with workspace:
        gateway = Gateway(store, Policy(), Registry(default_adapters()), workspace)
        yield SimpleNamespace(root=root, store=store, tokens=tokens, gateway=gateway, workspace=workspace)


def submit_approved(gateway, key, intent):
    row, _ = gateway.submit(REQUESTER, key, intent)
    snapshot = gateway.get_execution(REVIEWER, row["id"])
    gateway.decision(REVIEWER, row["id"], decision(snapshot))
    return row["id"]


def headers(runtime, name, key=None):
    values = {"Authorization": "Bearer " + runtime.tokens[name]}
    if key:
        values["Idempotency-Key"] = key
    return values


def poll(client, runtime, execution_id, requester="requester"):
    for _ in range(100):
        response = client.get(f"/api/v1/executions/{execution_id}", headers=headers(runtime, requester))
        assert response.status_code == 200
        result = response.json()
        if result["status"] not in {"ready", "executing"}:
            return result
        time.sleep(0.02)
    pytest.fail("Worker did not finish")


def test_api_real_incident_workflow(runtime):
    requester = headers(runtime, "requester")
    reviewer = headers(runtime, "reviewer")
    with TestClient(create_app(Settings(runtime.root)), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.get("/readyz").json() == {"status": "ready"}
        assert client.get("/api/v1/executions").status_code == 401
        assert client.get("/api/v1/approvals", headers=requester).status_code == 403
        assert client.get("/api/v1/session", headers=requester).headers["Cache-Control"] == "no-store"

        def run(key, intent, approve=True):
            response = client.post("/api/v1/executions", headers={**requester, "Idempotency-Key": key}, json=intent)
            assert response.status_code == 201, response.text
            run_id = response.json()["id"]
            if approve:
                assert response.json()["status"] == "awaiting_approval"
                pending = client.get("/api/v1/approvals", headers=reviewer).json()
                snapshot = next(row for row in pending if row["id"] == run_id)
                assert snapshot["requested_intent"] == intent
                review = {
                    "decision": "approve", "request_hash": snapshot["request_hash"],
                    "policy_revision": snapshot["policy_revision"],
                }
                endpoint = f"/api/v1/approvals/{run_id}/decision"
                assert client.post(endpoint, headers=requester, json=review).status_code == 403
                assert client.post(endpoint, headers=reviewer, json=review).status_code == 200
                assert client.post(endpoint, headers=reviewer, json=review).status_code == 409
            result = poll(client, runtime, run_id)
            assert result["status"] == "succeeded", result
            assert "adapter" not in result and "requested_intent" not in result
            return result

        service = run("workflow-service", action("lookup-record", collection="services", record_id="gateway"), False)
        assert service["receipt"]["record"]["name"] == "Intent execution gateway"
        ticket = run("workflow-ticket", action("create-ticket", title="API incident", detail="Investigate latency"))
        ticket_id = ticket["receipt"]["ticket_id"]
        updated = run("workflow-update", action("update-ticket", ticket_id=ticket_id, expected_revision=1,
                                               status="investigating"))
        assert updated["receipt"]["revision"] == 2

        content = f"Incident {ticket_id} — latency investigation"
        written = run("workflow-write", action("write-file", path="reports/incident.txt", content=content))
        assert (runtime.root / "files/reports/incident.txt").read_text() == content
        read = run("workflow-read", action("read-file", path="reports/incident.txt"), False)
        assert read["receipt"]["content"] == content
        assert read["receipt"]["sha256"] == written["receipt"]["sha256"]

        delivery = run("workflow-send", action("send-message", recipient="reviewer", body=f"Review {ticket_id}"))
        assert delivery["receipt"]["delivery"] == "delivered_to_local_inbox"
        assert client.get("/api/v1/inbox", headers=requester).json() == []
        inbox = client.get("/api/v1/inbox", headers=reviewer).json()
        assert [message["body"] for message in inbox] == [f"Review {ticket_id}"]

        detail = run("workflow-fetch", action("lookup-record", collection="tickets", record_id=ticket_id), False)
        assert detail["receipt"]["record"]["status"] == "investigating"

        review_events = client.get(f"/api/v1/executions/{ticket['id']}/events", headers=reviewer).json()
        assert [event["state"] for event in review_events] == ["awaiting_approval", "ready", "executing", "succeeded"]
        assert review_events[1]["actor"] == "reviewer" and review_events[2]["adapter_id"] == "local-create-ticket"
        assert "adapter_id" not in client.get(f"/api/v1/executions/{ticket['id']}/events", headers=requester).json()[2]
        assert client.get(f"/api/v1/executions/{ticket['id']}", headers=headers(runtime, "agent")).status_code == 404
        assert verify_audit(runtime.store)["executions"] == 7


def test_schema_errors_and_denials_have_no_effect(runtime):
    gateway = runtime.gateway
    with pytest.raises(GatewayError, match="invalid_intent"):
        gateway.submit(REQUESTER, "traversal-key", action("read-file", path="reports/../outside"))
    with pytest.raises(GatewayError, match="invalid_intent"):
        gateway.submit(REQUESTER, "wrong-schema", {"schema_version": "v1", "operation": "read-file", "path": "reports/a"})
    forged = INTENT.validate_python(action("read-file", path="reports/a")).model_copy(update={"handler": "private"})
    # Constructed models can carry invalid field values without running validators.
    forged.path = "reports/../outside"
    with pytest.raises(GatewayError, match="invalid_intent"):
        gateway.submit(REQUESTER, "object-validation", forged)
    denied, _ = gateway.submit(REQUESTER, "denied-write", action("write-file", path="workspace/abc", content="private"))
    assert denied["status"] == "denied"
    assert gateway.run_once() is False
    assert list((runtime.root / "files/workspace").iterdir()) == []
    with runtime.store.connect() as db:
        assert db.execute("SELECT intent_json FROM executions").fetchone()[0] is None


def test_duplicate_json_limits_and_cors(runtime):
    with TestClient(create_app(Settings(runtime.root)), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        auth = headers(runtime, "requester", "duplicate-key")
        raw_headers = {**auth, "Content-Type": "application/json"}
        duplicate = '{"schema_version":"v2","operation":"read-file","path":"reports/a","path":"reports/b"}'
        assert client.post("/api/v1/executions", headers=raw_headers, content=duplicate).status_code == 422
        assert client.post("/api/v1/executions", headers=raw_headers, content=b"x" * 525000).status_code == 413
        assert client.post("/api/v1/executions", headers=auth, content=b"{}").status_code == 415
        res = client.get("/api/v1/session", headers={**auth, "Origin": "http://127.0.0.1:3000"})
        assert res.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:3000"
        res = client.get("/api/v1/session", headers={**auth, "Origin": "http://other.example"})
        assert "Access-Control-Allow-Origin" not in res.headers
        assert client.get("/api/v1/executions", headers=auth).json() == []


def test_concurrent_idempotency_and_approval(runtime):
    gateway = runtime.gateway
    intent = action("send-message", recipient="reviewer", body="one message")
    with ThreadPoolExecutor(max_workers=8) as executor:
        submissions = list(executor.map(lambda _: gateway.submit(REQUESTER, "parallel-key", intent), range(20)))
    assert len({row["id"] for row, _ in submissions}) == 1
    assert sum(created for _, created in submissions) == 1
    run_id = submissions[0][0]["id"]
    with pytest.raises(GatewayError, match="idempotency_conflict"):
        gateway.submit(REQUESTER, "parallel-key", {**intent, "body": "changed"})

    snapshot = gateway.get_execution(REVIEWER, run_id)
    with pytest.raises(GatewayError, match="approval_snapshot_changed"):
        gateway.decision(REVIEWER, run_id, decision(snapshot, request_hash="0" * 64))

    def review(_):
        try:
            gateway.decision(REVIEWER, run_id, decision(snapshot))
            return "approved"
        except GatewayError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(review, range(8)))
    assert outcomes.count("approved") == 1
    assert outcomes.count("approval_not_pending") == 7
    assert gateway.run_once() and not gateway.run_once()
    assert len(gateway.inbox(REVIEWER)) == 1


def test_expired_denied_and_policy_changes(runtime):
    gateway = runtime.gateway
    now = 100
    gateway.clock = lambda: now
    intent = action("create-ticket", title="Incident", detail="Test policy")
    pending, _ = gateway.submit(REQUESTER, "expired-ticket", intent)
    snap = gateway.get_execution(REVIEWER, pending["id"])
    now += 901
    with pytest.raises(GatewayError, match="approval_expired"):
        gateway.decision(REVIEWER, pending["id"], decision(snap))
    assert gateway.get_execution(REQUESTER, pending["id"])["status"] == "expired"

    row, _ = gateway.submit(REQUESTER, "denied-ticket", intent)
    snap = gateway.get_execution(REVIEWER, row["id"])
    gateway.decision(REVIEWER, row["id"], decision(snap, "deny"))

    approved = submit_approved(gateway, "stale-policy", intent)
    changed = Gateway(runtime.store, Policy(recipients=["requester"]), gateway.registry, runtime.workspace,
                      clock=gateway.clock)
    changed.run_once()
    assert changed.get_execution(REQUESTER, approved)["reason"] == "policy_changed"
    with runtime.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_role_revocation_and_registry_disabled(runtime):
    gateway = runtime.gateway
    row = submit_approved(gateway, "revoke-review", action("create-ticket", title="Y", detail="X"))
    runtime.store.revoke("reviewer")
    gateway.run_once()
    assert gateway.get_execution(REQUESTER, row)["reason"] == "approval_invalid"
    disabled = Registry([AdapterSpec(adapter_id="disabled", operation="lookup-record",
                                     implementation="lookup-record", enabled=False)])
    gateway = Gateway(runtime.store, gateway.policy, disabled, runtime.workspace)
    row, _ = gateway.submit(REQUESTER, "disabled-read", action("lookup-record", collection="services", record_id="gateway"))
    gateway.run_once()
    assert gateway.get_execution(REQUESTER, row["id"])["reason"] == "no_enabled_adapter"


def test_symlinks_nonregular_files_and_create_only(runtime, tmp_path):
    gateway = runtime.gateway
    reports = runtime.root / "files/reports"
    outside = tmp_path / "outside.txt"
    outside.write_text("preserve this")
    (reports / "link").symlink_to(outside)
    (reports / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(reports / "pipe", 0o600)
    for i, path in enumerate(("reports/link", "reports/linked-dir/outside.txt", "reports/pipe")):
        row, _ = gateway.submit(REQUESTER, f"invalid-file-{i}", action("read-file", path=path))
        gateway.run_once()
        assert gateway.get_execution(REQUESTER, row["id"])["status"] == "failed"

    for i, path in enumerate(("reports/link", "reports/linked-dir/new.txt")):
        run_id = submit_approved(gateway, f"invalid-write-{i}", action("write-file", path=path, content="changed"))
        gateway.run_once()
        assert gateway.get_execution(REQUESTER, run_id)["status"] == "failed"
    assert outside.read_text() == "preserve this"
    assert not (tmp_path / "new.txt").exists()

    first = submit_approved(gateway, "first-writer", action("write-file", path="reports/one", content="one"))
    gateway.run_once()
    second = submit_approved(gateway, "second-writer", action("write-file", path="reports/one", content="two"))
    gateway.run_once()
    assert gateway.get_execution(REQUESTER, first)["status"] == "succeeded"
    assert gateway.get_execution(REQUESTER, second)["reason"] == "path_exists"
    assert (reports / "one").read_text() == "one"


def test_concurrent_ticket_updates_use_revision_preconditions(runtime):
    gateway = runtime.gateway
    initial = submit_approved(gateway, "initial-ticket", action("create-ticket", title="Incident", detail="Original"))
    gateway.run_once()
    ticket_id = gateway.get_execution(REQUESTER, initial)["receipt"]["ticket_id"]
    ids = [submit_approved(gateway, f"ticket-update-{i}", action("update-ticket", ticket_id=ticket_id,
                                                               expected_revision=1, detail=f"detail {i}"))
           for i in range(2)]
    assert gateway.run_once() and gateway.run_once()
    statuses = {gateway.get_execution(REQUESTER, item)["status"] for item in ids}
    assert statuses == {"succeeded", "failed"}
    with runtime.store.connect() as db:
        assert db.execute("SELECT revision FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0] == 2


@pytest.mark.parametrize("phase, operation, expected", [
    ("before_claim_commit", "send-message", "succeeded"),
    ("after_claim_commit", "send-message", "succeeded"),
    ("before_sql_commit", "send-message", "succeeded"),
    ("after_sql_commit", "send-message", "succeeded"),
    ("after_file_stage", "write-file", "outcome_unknown"),
    ("after_file_publish", "write-file", "succeeded"),
    ("after_file_commit", "write-file", "succeeded"),
])
def test_process_termination_at_durable_boundaries(runtime, phase, operation, expected):
    gateway = runtime.gateway
    intent = (action("send-message", recipient="reviewer", body="one")
              if operation == "send-message" else action("write-file", path="reports/crash.txt", content="one"))
    run_id = submit_approved(gateway, "crash-test", intent)
    program = """
import os, sys
from pathlib import Path
from gateway.config import load_policy, load_registry
from gateway.engine import Gateway
from gateway.store import Store, runtime_lock
from gateway.workspace import Workspace
root = Path(sys.argv[1])
def crash(phase, row):
    if phase == sys.argv[2]:
        os._exit(77)
with runtime_lock(root), Workspace(root) as workspace:
    Gateway(Store(root), load_policy(root), load_registry(root), workspace, fault=crash).run_once()
"""
    child = subprocess.run([sys.executable, "-B", "-c", program, str(runtime.root), phase], timeout=20, check=False)
    assert child.returncode == 77
    gateway.recover()
    gateway.run_once()
    result = gateway.get_execution(REQUESTER, run_id)
    assert result["status"] == expected
    replay, created = gateway.submit(REQUESTER, "crash-test", intent)
    assert not created and replay["id"] == run_id and replay["status"] == expected
    assert not gateway.run_once()
    assert verify_audit(runtime.store)["executions"] == 1
    if operation == "send-message":
        assert [message["body"] for message in gateway.inbox(REVIEWER)] == ["one"]
    elif expected == "succeeded":
        assert (runtime.root / "files/reports/crash.txt").read_text() == "one"
        assert not list((runtime.root / "files/reports").glob(".broker-stage-*"))
    else:
        assert not (runtime.root / "files/reports/crash.txt").exists()


def test_registry_description_independent_planner_and_deterministic_tie_break(runtime):
    calls = []

    class Provider:
        def invoke(self, messages):
            calls.append(messages)
            return json.dumps(action("lookup-record", collection="services", record_id="gateway"))

    first = AdapterSpec(adapter_id="aa", operation="lookup-record", implementation="lookup-record", priority=5,
                        description="PRIVATE_REGISTRY_CANARY_A")
    second = first.model_copy(update={"adapter_id": "zz", "description": "PRIVATE_REGISTRY_CANARY_B"})
    registry = Registry([second, first])
    planner = Planner(Provider())
    intent = planner.propose("Find the gateway")
    changed = Registry([second, first.model_copy(update={"description": "PRIVATE_REGISTRY_CANARY_C"})])
    assert planner.propose("Find the gateway") == intent
    assert calls[0] == calls[1]
    assert "PRIVATE_REGISTRY_CANARY" not in json.dumps(calls)
    assert registry.revision == changed.revision
    gateway = Gateway(runtime.store, Policy(), registry, runtime.workspace)
    row, _ = gateway.submit(REQUESTER, "tie-break", intent)
    gateway.run_once()
    assert gateway.get_execution(REVIEWER, row["id"])["adapter"] == "aa"


def test_lock_and_unconfigured_planner(runtime):
    with runtime_lock(runtime.root):
        with pytest.raises(RuntimeError, match="runtime lock"):
            with runtime_lock(runtime.root):
                pytest.fail("Duplicate worker lock accepted")
    with TestClient(create_app(Settings(runtime.root)), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        result = client.post("/api/v1/proposals", headers=headers(runtime, "requester"), json={"text": "Look up gateway"})
        assert result.status_code == 503 and result.json()["detail"] == "planner_not_configured"


def test_local_planner_transport_is_separate_from_execution(runtime):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            content = (
                '{"operation":"lookup-record","operation":"send-message"}'
                if len(requests) == 1
                else json.dumps(action("lookup-record", collection="services", record_id="gateway"))
            )
            payload = json.dumps({"message": {"content": content}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    with ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler) as model:
        thread = Thread(target=model.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{model.server_address[1]}"
            settings = Settings(runtime.root, model="test-provider", ollama_url=url)
            with TestClient(create_app(settings), base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
                auth = headers(runtime, "requester")
                proposal = client.post("/api/v1/proposals", headers=auth, json={"text": "Look up gateway"})
                assert proposal.status_code == 200
                assert proposal.json()["intent"] == action("lookup-record", collection="services", record_id="gateway")
                assert client.get("/api/v1/executions", headers=auth).json() == []
                assert [path for path, _ in requests] == ["/api/chat", "/api/chat"]
                assert requests[0][1]["messages"][1] == {"role": "user", "content": "Look up gateway"}
                assert "local-lookup-record" not in json.dumps(requests)
                assert "previous response was invalid" in requests[1][1]["messages"][0]["content"]
        finally:
            model.shutdown()
            thread.join(timeout=5)


def test_adapter_receipt_rejection_rolls_back_business_row(runtime, monkeypatch):
    from gateway.adapters import SQL_ADAPTERS

    dispatch = SQL_ADAPTERS["send-message"]

    def invalid_receipt(*args):
        return {**dispatch(*args), "recipient": "requester"}

    monkeypatch.setitem(SQL_ADAPTERS, "send-message", invalid_receipt)
    run_id = submit_approved(runtime.gateway, "bad-receipt", action("send-message", recipient="reviewer", body="secret"))
    assert runtime.gateway.run_once()
    row = runtime.gateway.get_execution(REQUESTER, run_id)
    assert row["status"] == "failed" and row["reason"] == "invalid_effect_receipt" and row["receipt"] is None
    assert runtime.gateway.inbox(REVIEWER) == []
    assert verify_audit(runtime.store)["executions"] == 1


def test_file_staging_failure_leaves_destination_absent(runtime, monkeypatch):
    import errno

    def disk_full(*_):
        raise OSError(errno.ENOSPC, "simulated disk capacity failure")

    monkeypatch.setattr(runtime.workspace, "stage", disk_full)
    run_id = submit_approved(runtime.gateway, "disk-full", action("write-file", path="reports/absent", content="bytes"))
    runtime.gateway.run_once()
    assert runtime.gateway.get_execution(REQUESTER, run_id)["reason"] == "file_staging_failed"
    assert not (runtime.root / "files/reports/absent").exists()


def test_non_loopback_connections_are_rejected(runtime):
    with TestClient(create_app(Settings(runtime.root)), base_url="http://127.0.0.1", client=("192.0.2.10", 50000)) as client:
        response = client.get("/api/v1/session", headers=headers(runtime, "requester"))
        assert response.status_code == 403 and response.json()["detail"] == "loopback_required"

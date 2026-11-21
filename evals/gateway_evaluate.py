"""Reproducible practical evaluation for the credential-free local runtime."""
import argparse
import json
import math
import os
import platform
import sqlite3
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from gateway.config import Policy, Principal, Registry, default_adapters
from gateway.contracts import digest
from gateway.engine import Gateway, GatewayError
from gateway.planner import Planner
from gateway.store import Store, verify_audit
from gateway.workspace import Workspace

SPEC = Path(__file__).with_name("gateway-spec-v1.json")
REQUESTER = Principal("requester", "requester")
REVIEWER = Principal("reviewer", "reviewer")


def require(result, label):
    if not result:
        raise RuntimeError(label)


def intent(operation, **fields):
    return {"schema_version": "v2", "operation": operation, **fields}


def measure(samples, label, action):
    start = time.perf_counter_ns()
    try:
        return action()
    finally:
        samples[label].append((time.perf_counter_ns() - start) / 1_000_000)


def execute(gateway, samples, key, request):
    def run():
        row, new = measure(samples, "submit", lambda: gateway.submit(REQUESTER, key, request))
        require(new, "submission_not_new")
        if row["status"] == "awaiting_approval":
            pending = gateway.get_execution(REVIEWER, row["id"])
            require(pending["requested_intent"] == request, "approval_mismatch")
            approval = SimpleNamespace(decision="approve", request_hash=pending["request_hash"],
                                       policy_revision=pending["policy_revision"])
            measure(samples, "scripted_approval", lambda: gateway.decision(REVIEWER, row["id"], approval))
        measure(samples, "queue_dispatch_to_commit", gateway.run_once)
        return gateway.get_execution(REQUESTER, row["id"])
    return measure(samples, "end_to_end_direct", run)


def incident(gateway, root, samples, number):
    prefix = f"incident-{number:02}"
    created = execute(gateway, samples, prefix + "-ticket",
                      intent("create-ticket", title=f"Incident {number}", detail="Investigate elevated latency"))
    require(created["status"] == "succeeded", "ticket_creation")
    ticket_id = created["receipt"]["ticket_id"]
    report_path = f"reports/{prefix}.txt"
    content = f"Ticket {ticket_id}, elevated latency, investigation started. Résumé 🛠"
    wrote = execute(gateway, samples, prefix + "-write", intent("write-file", path=report_path, content=content))
    require(wrote["status"] == "succeeded", "file_publication")
    stored = (root / "files" / report_path).read_bytes()
    require(stored == content.encode() and wrote["receipt"]["sha256"] == digest(stored), "file_receipt")
    message = intent("send-message", recipient="reviewer", body=f"{ticket_id} report {report_path}")
    sent = execute(gateway, samples, prefix + "-message", message)
    require(sent["status"] == "succeeded" and sent["receipt"]["delivery"] == "delivered_to_local_inbox", "message_delivery")
    replay, new = gateway.submit(REQUESTER, prefix + "-message", message)
    require(not new and replay == sent, "message_replay")
    require(sum(row["body"] == message["body"] for row in gateway.inbox(REVIEWER)) == 1, "message_count")
    looked_up = execute(gateway, samples, prefix + "-lookup", intent("lookup-record", collection="tickets", record_id=ticket_id))
    require(looked_up["status"] == "succeeded" and looked_up["receipt"]["record"]["ticket_id"] == ticket_id, "ticket_lookup")
    read = execute(gateway, samples, prefix + "-read", intent("read-file", path=report_path))
    require(read["status"] == "succeeded" and read["receipt"]["content"] == content, "file_read")
    updated = execute(gateway, samples, prefix + "-update",
                      intent("update-ticket", ticket_id=ticket_id, expected_revision=1, status="investigating"))
    require(updated["status"] == "succeeded" and updated["receipt"]["revision"] == 2, "ticket_update")
    with gateway.store.connect() as db:
        persisted = db.execute("SELECT status,revision FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        require(tuple(persisted) == ("investigating", 2), "ticket_state")
    return ticket_id


def controls(gateway, root, samples, ticket_id):
    def denied():
        row, _ = gateway.submit(REQUESTER, "control-denied", intent("write-file", path="workspace/denied", content="test"))
        return row["status"] == "denied" and not (root / "files/workspace/denied").exists()

    def conflict():
        try:
            gateway.submit(REQUESTER, "incident-01-message", intent("send-message", recipient="reviewer", body="changed"))
        except GatewayError as error:
            return error.code == "idempotency_conflict"
        return False

    def stale_revision():
        if ticket_id is None:
            return False
        updated = execute(gateway, samples, "control-revision",
                          intent("update-ticket", ticket_id=ticket_id, expected_revision=1, status="resolved"))
        with gateway.store.connect() as db:
            persisted = db.execute("SELECT status,revision FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return updated["reason"] == "ticket_revision_conflict" and tuple(persisted) == ("investigating", 2)

    def metadata_isolation():
        captures = []

        class ProviderSpy:
            def invoke(self, messages):
                captures.append(messages)
                return json.dumps(intent("lookup-record", collection="services", record_id="gateway"))

        first = default_adapters()
        second = [entry.model_copy(update={"description": "INTERNAL_REGISTRY_CANARY"})
                  for entry in first]
        registry_before, registry_after = Registry(first), Registry(second)
        planner = Planner(ProviderSpy())
        outputs = [planner.propose("Look up the gateway") for _ in (registry_before, registry_after)]
        return outputs[0] == outputs[1] and captures[0] == captures[1] and "INTERNAL_REGISTRY_CANARY" not in json.dumps(captures)

    return {
        "denied_namespace_creates_no_file": denied(),
        "changed_request_with_same_key_conflicts": conflict(),
        "stale_ticket_revision_preserves_current_revision": stale_revision(),
        "literal_workspace_metadata_is_excluded_from_planner_input": metadata_isolation(),
    }


def summarize(values):
    values = sorted(values)
    return {"count": len(values), "p50_ms": round(values[math.ceil(len(values) * .50) - 1], 4),
            "p95_ms": round(values[math.ceil(len(values) * .95) - 1], 4)}


def source_sha256():
    root = SPEC.parents[1]
    paths = [*sorted((root / "gateway").glob("*.py")), Path(__file__).resolve(), SPEC, root / "requirements-lock.txt"]
    return {str(path.relative_to(root)): digest(path.read_bytes()) for path in paths}


def evaluate():
    spec_data = SPEC.read_bytes()
    spec = json.loads(spec_data)
    samples = defaultdict(list)
    rows = []
    started = time.time()
    last_ticket = None
    with tempfile.TemporaryDirectory(prefix="gateway-evaluation-") as temp:
        root = Path(temp)
        store = Store(root)
        store.migrate(create=True)
        store.provision_demo()
        workspace = Workspace(root)
        workspace.initialize()
        with workspace:
            gateway = Gateway(store, Policy(), Registry(default_adapters()), workspace,
                              observe=lambda name, value: samples[name].append(value))
            for index in range(1, spec["incident_workflows"] + 1):
                try:
                    last_ticket = incident(gateway, root, samples, index)
                except RuntimeError as exc:
                    rows.append({"workflow": index, "passed": False, "failure": str(exc)})
                else:
                    rows.append({"workflow": index, "passed": True})
            control_results = controls(gateway, root, samples, last_ticket)
        verified = verify_audit(store)
    passing = sum(row["passed"] for row in rows)
    expected = spec["thresholds"]
    report = {
        "status": "passed" if passing == expected["correct_workflows"] and sum(control_results.values()) == expected["passed_controls"] else "failed",
        "spec": spec["version"], "spec_sha256": digest(spec_data), "source_sha256": source_sha256(),
        "environment": {"platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
        "started_at_unix": started, "completed_at_unix": time.time(),
        "correct_workflows": passing, "total_workflows": len(rows), "rows": rows, "controls": control_results,
        "audit": verified, "latency": spec["latency"], "latency_samples": {key: summarize(values) for key, values in samples.items()},
        "live_model": "not_run",
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = evaluate()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".gateway-report-", dir=args.out.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(report, file, indent=2, ensure_ascii=False)
                file.write("\n")
                file.flush()
                os.replace(temporary, args.out)
            finally:
                temporary.unlink(missing_ok=True)
    print(json.dumps({key: value for key, value in report.items() if key not in {"rows", "source_sha256"}}, indent=2))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Run versioned expansion tracks with genuine local effects and independent readback."""
import argparse
import json
import math
import platform
import socket
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import closing, contextmanager
from pathlib import Path

import uvicorn

from gateway.config import Settings
from gateway.contracts import digest, strict_json
from gateway.labs import (
    ComparisonRequest, RecoveryRequest, REQUESTER, REVIEWER, SOURCE, SPECS, approve, disposable,
    provenance, run_comparison, run_recovery,
)
from gateway.mcp_bridge import AllowedServer, MappingRequest, McpGovernance
from gateway.mcp_service import create_service, initialize
from gateway.store import verify_audit
from gateway.workflows import Workflows


@contextmanager
def local_service(root):
    initialize(root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        endpoint = AllowedServer(id="review-tickets", port=listener.getsockname()[1])
        service = uvicorn.Server(uvicorn.Config(create_service(root), log_level="critical", proxy_headers=False))
        thread = threading.Thread(target=service.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not service.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not service.started:
                raise RuntimeError("MCP laboratory server did not start")
            yield endpoint
        finally:
            service.should_exit = True
            thread.join(6)


def timed(samples, operation, action):
    started = time.perf_counter_ns()
    try:
        return action()
    finally:
        samples[operation].append((time.perf_counter_ns() - started) / 1_000_000)


def distribution(values):
    ordered = sorted(values)
    return {"count": len(values), "p50_ms": round(ordered[math.ceil(.50 * len(values)) - 1], 4),
            "p95_ms": round(ordered[math.ceil(.95 * len(values)) - 1], 4)}


def incident(number):
    path = f"reports/incident-{number}.txt"
    return {"schema_version": "workflow-v1", "nodes": [
        {"id": "ticket", "operation": "create-ticket", "arguments": {
            "title": f"Evaluation incident {number}", "detail": "Verify queue latency",
        }},
        {"id": "report", "operation": "write-file", "depends_on": ["ticket"], "arguments": {
            "path": path, "content": {"join": ["Reference ", {"step": "ticket", "field": "ticket_id"}]},
        }},
        {"id": "notify", "operation": "send-message", "depends_on": ["ticket", "report"], "arguments": {
            "recipient": "reviewer", "body": {"join": ["Review ", {"step": "ticket", "field": "ticket_id"}, f" at {path}"]},
        }},
    ]}


def execute_workflow(gateway, graph, samples, index):
    plan = incident(index)
    workflow, _ = graph.submit(REQUESTER, f"integrated-incident-{index}", plan)
    reviewed = []
    for _ in range(3):
        timed(samples, "workflow_schedule", graph.tick)
        approvals = gateway.list_executions(REVIEWER, approval_queue=True)
        if len(approvals) != 1:
            break
        row = approvals[0]
        reviewed.append({"request_hash": row["request_hash"], "arguments": row["requested_intent"],
                         "execution_id": row["id"], "planned_adapter": row["planned_adapter"]})
        timed(samples, "scripted_approval", lambda: approve(gateway, row["id"]))
        timed(samples, "worker_round", gateway.run_once)
    graph.tick()
    return graph.get(REVIEWER, workflow["id"]), reviewed


def inspect_workflow(root, gateway, workflow, reviewed, index):
    node = next(item for item in workflow["nodes"] if item["id"] == "ticket")
    execution = gateway.get_execution(REVIEWER, node["execution_id"]) if node["execution_id"] else {}
    expected_id = "tkt-" + (execution.get("id") or "")
    with closing(sqlite3.connect((root / "ticket-service/tickets.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        observed_ticket = db.execute("SELECT title,detail FROM tickets WHERE ticket_id=?", (expected_id,)).fetchone()
    report_path = f"reports/incident-{index}.txt"
    target = root / "files" / report_path
    observed_report = target.read_text() if target.is_file() else None
    expected_message = f"Review {expected_id} at {report_path}"
    with closing(sqlite3.connect((root / "gateway.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        message_count = db.execute("SELECT COUNT(*) FROM inbox WHERE recipient='reviewer' AND body=?",
                                   (expected_message,)).fetchone()[0]
        fallback_count = db.execute("SELECT COUNT(*) FROM tickets WHERE id=?", (expected_id,)).fetchone()[0]
    checked = (workflow["status"] == "succeeded" and execution.get("adapter") == "mcp-review-tickets"
               and observed_ticket == (f"Evaluation incident {index}", "Verify queue latency")
               and observed_report == f"Reference {expected_id}" and message_count == 1 and fallback_count == 0
               and len(reviewed) == 3 and reviewed[1]["arguments"]["content"] == observed_report
               and reviewed[2]["arguments"]["body"] == expected_message)
    return {"workflow": workflow, "reviewed": reviewed, "ticket_execution": execution,
            "observed": {"ticket": list(observed_ticket) if observed_ticket else None, "report": observed_report,
                         "inbox_matches": message_count, "local_fallbacks": fallback_count}, "passed": checked}


def integrated(repetitions):
    samples = defaultdict(list)
    with disposable() as (root, gateway), local_service(root / "ticket-service") as endpoint:
        credential = root / "mcp-review-tickets.token"
        credential.write_bytes((root / "ticket-service/token").read_bytes())
        credential.chmod(0o600)
        gateway.mcp = McpGovernance(gateway.store, root, [endpoint])
        discovery = timed(samples, "mcp_discovery", lambda: gateway.mcp.inspect(REVIEWER, endpoint.id))
        tool = next(row for row in discovery["tools"] if row["compatible"])
        mapping = timed(samples, "mcp_mapping_approval", lambda: gateway.mcp.register(REVIEWER, MappingRequest(
            server_id=endpoint.id, tool=tool["name"], schema_sha256=tool["schema_sha256"], priority=0,
        )))
        gateway.observe = lambda name, elapsed: samples[name].append(elapsed)
        graph = Workflows(gateway)
        rows = []
        for index in range(1, repetitions + 1):
            workflow, reviewed = timed(samples, "scripted_workflow_total",
                                       lambda: execute_workflow(gateway, graph, samples, index))
            rows.append(inspect_workflow(root, gateway, workflow, reviewed, index))
        audit = verify_audit(gateway.store)
    return {"mapping": mapping, "discovery": discovery, "runs": rows, "audit": audit,
            "latency": {name: distribution(values) for name, values in sorted(samples.items())}}


def write_track(root, track, report):
    path = root / track
    path.mkdir()
    (path / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def evaluate(output, *, with_live=False):
    spec_bytes = (SPECS / "integrated-v1.json").read_bytes()
    spec = strict_json(spec_bytes)
    output.mkdir(parents=False, mode=0o700)
    result = integrated(spec["repetitions"])
    workflow_runs = result["runs"]
    evidence = provenance("integrated")
    evidence["sources"] = {str(path.relative_to(SOURCE)): digest(path.read_bytes()) for path in (
        *sorted((SOURCE / "gateway").glob("*.py")), Path(__file__).resolve(), SOURCE / "requirements-lock.txt",
    )}
    if platform.system() == "Darwin":
        import subprocess
        hardware = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                  text=True, check=False, timeout=2).stdout.strip()
    else:
        hardware = platform.processor()
    evidence["hardware"] = hardware or platform.machine()
    write_track(output, "mcp", {"spec_sha256": digest((SPECS / "mcp-v1.json").read_bytes()),
                                "mapping": result["mapping"], "discovery": result["discovery"],
                                "checks": [run["observed"] for run in workflow_runs], "latency": result["latency"]})
    write_track(output, "workflows", {"spec_sha256": digest((SPECS / "workflows-v1.json").read_bytes()),
                                      "runs": workflow_runs, "audit": result["audit"], "latency": result["latency"]})

    recovery_spec = strict_json((SPECS / "recovery-v1.json").read_bytes())
    recoveries = []
    for operation, cases in recovery_spec["cases"].items():
        for case in cases:
            boundary, _, treatment = case.partition(":")
            recoveries.append(run_recovery(RecoveryRequest(operation=operation, boundary=boundary,
                                                           evidence=treatment or "intact")))
    write_track(output, "recovery", {"runs": recoveries, "latency": distribution([row["recovery_ms"] for row in recoveries])})

    settings = Settings.from_env()
    comparison_spec = strict_json((SPECS / "comparison-v1.json").read_bytes())
    offline = [run_comparison(ComparisonRequest(task=case), settings) for case in comparison_spec["tasks"]]
    live = ([run_comparison(ComparisonRequest(task=case, mode="live"), settings) for case in comparison_spec["tasks"]]
            if with_live else [{"status": "not_run", "reason": "Live model execution was not requested"}])
    write_track(output, "comparison", {"offline": offline, "live": live})
    counts = {"workflow_passes": sum(row["passed"] for row in workflow_runs), "workflows": len(workflow_runs),
              "recovery_passes": sum(row["independent_check_passed"] for row in recoveries), "recovery_cases": len(recoveries),
              "verified_comparison_cells": sum(row["metrics"]["correct_effect_cells"] for row in offline),
              "comparison_cells": sum(len(row["cells"]) for row in offline)}
    valid = (counts["workflow_passes"] == counts["workflows"] and counts["recovery_passes"] == counts["recovery_cases"]
             and counts["verified_comparison_cells"] == counts["comparison_cells"]
             and all(row["metrics"]["protected_context_equal"] and row["metrics"]["equivalent_initial_world"] for row in offline))
    report = {"status": "passed" if valid else "failed", "counts": counts, "environment": evidence,
              "live_model": [row["status"] for row in live], "latency": result["latency"]}
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="New evidence directory with an existing parent")
    parser.add_argument("--live", action="store_true", help="Opt into a configured local model with neutral variants")
    args = parser.parse_args()
    result = evaluate(args.out, with_live=args.live)
    print(json.dumps({key: value for key, value in result.items() if key != "environment"}, indent=2))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())

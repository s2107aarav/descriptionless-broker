"""Disposable effect inspection, process recovery and matched metadata comparisons."""
import platform
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import ValidationError

from .config import AdapterSpec, Policy, Principal, Registry, default_adapters
from .contracts import Closed, INTENT, canonical, digest, fingerprint, strict_json
from .engine import Gateway, GatewayError, check_actor
from .planner import InvalidPlannerOutput, OllamaProvider, Planner, PlannerUnavailable
from .store import Store, verify_audit
from .workspace import Workspace

SOURCE = Path(__file__).resolve().parents[1]
SPECS = SOURCE / "evals" / "specs"
REQUESTER = Principal("requester", "requester")
REVIEWER = Principal("reviewer", "reviewer")
RECOVERY_KEY = "disposable-recovery-request"
RELATIVE_REPORT = "reports/recovery.txt"
REPORT_BODY = "Disposable recovery report.\n"
NOTE = "Disposable recovery notification."
RECOVERY_BOUNDARIES = {
    "write-file": {"after_claim_commit", "after_file_stage", "after_file_publish", "after_file_commit"},
    "send-message": {"after_claim_commit", "before_sql_commit", "after_sql_commit"},
}


class RecoveryRequest(Closed):
    operation: Literal["write-file", "send-message"]
    boundary: Literal["after_claim_commit", "before_sql_commit", "after_sql_commit",
                      "after_file_stage", "after_file_publish", "after_file_commit"]
    evidence: Literal["intact", "remove-staging"] = "intact"


class ComparisonRequest(Closed):
    task: Literal["ticket", "message"]
    mode: Literal["offline", "live"] = "offline"


def provenance(name):
    spec = (SPECS / f"{name}-v1.json").read_bytes()
    sources = ("gateway/engine.py", "gateway/store.py", "gateway/workspace.py", "gateway/contracts.py",
               "gateway/labs.py", "gateway/lab_worker.py", "gateway/planner.py")
    return {"spec": name + "-v1", "spec_sha256": digest(spec), "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
            "source_sha256": {file: digest((SOURCE / file).read_bytes()) for file in sources}}


@contextmanager
def disposable(registry=None):
    with tempfile.TemporaryDirectory(prefix="broker-lab-") as directory:
        root = Path(directory)
        (root / ".disposable-lab").touch(mode=0o600)
        store = Store(root)
        store.migrate(create=True)
        store.provision_demo()
        workspace = Workspace(root)
        workspace.initialize()
        with workspace:
            gateway = Gateway(store, Policy(), registry or Registry(default_adapters()), workspace)
            yield root, gateway


def approve(gateway, execution_id):
    row = gateway.get_execution(REVIEWER, execution_id)
    gateway.decision(REVIEWER, execution_id, SimpleNamespace(
        decision="approve", request_hash=row["request_hash"], policy_revision=row["policy_revision"],
    ))


def readonly(root):
    db = sqlite3.connect((root / "gateway.sqlite3").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return closing(db)


def persisted_world(root):
    """Read business state directly and remove generated identifiers for matched cells."""
    with readonly(root) as db:
        tickets = [dict(row) for row in db.execute("SELECT title,detail,status,revision FROM tickets ORDER BY title,id")]
        inbox = [dict(row) for row in db.execute("SELECT recipient,sender,body FROM inbox ORDER BY recipient,id")]
        services = [dict(row) for row in db.execute("SELECT id,name,description FROM services ORDER BY id")]
    files = []
    for file in sorted((root / "files").glob("*/*")):
        evidence = inspect_file(file)
        if evidence is not None:
            files.append({"path": str(file.relative_to(root / "files")), "sha256": evidence.get("sha256")})
    return {"tickets": tickets, "inbox": inbox, "services": services, "files": files}


def inspect_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        return {"regular_file": False}
    data = path.read_bytes()
    return {"regular_file": True, "inode": [info.st_dev, info.st_ino], "bytes": len(data), "sha256": digest(data)}


def inspect_recovery(root, identifier):
    with readonly(root) as db:
        row = db.execute("""SELECT id,status,reason,attempt,request_key,receipt_json,stage_json
            FROM executions WHERE id=?""", (identifier,)).fetchone()
        execution = dict(row) if row else None
        if execution:
            for field in ("receipt", "stage"):
                raw = execution.pop(field + "_json")
                execution[field] = strict_json(raw) if raw else None
        events = [dict(item) for item in db.execute("""SELECT seq,state,at,detail,attempt
            FROM events WHERE execution_id=? ORDER BY seq""", (identifier,))]
        count = db.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        messages = [dict(item) for item in db.execute("SELECT id,recipient,body,origin_execution FROM inbox ORDER BY id")]
    target = inspect_file(root / "files" / RELATIVE_REPORT)
    staged = inspect_file(root / "files/reports" / (".broker-stage-" + identifier))
    return {"execution": execution, "execution_count": count, "events": events,
            "inbox": messages, "target": target, "staged": staged}


def classification(operation, identifier, interrupted, after, replay):
    execution = after["execution"]
    if (execution is None or execution["request_key"] != RECOVERY_KEY or after["execution_count"] != 1
            or replay["id"] != identifier or replay["status"] != execution["status"]):
        return "inconsistent"
    status = execution["status"]
    if operation == "send-message":
        committed = after["inbox"]
        if status == "succeeded" and len(committed) == 1:
            message = committed[0]
            if message["origin_execution"] == identifier and message["recipient"] == "reviewer" and message["body"] == NOTE:
                return "proven_success"
        return "inconsistent"
    if status == "failed" and not interrupted["execution"]["stage"] and after["target"] is None:
        return "known_no_effect"
    if status == "outcome_unknown" and execution["receipt"] is None and execution["attempt"] == 1:
        return "outcome_unknown"
    expected_hash = digest(REPORT_BODY.encode())
    result = execution["receipt"]
    target = after["target"] or {}
    checkpoint = interrupted["execution"]["stage"] or interrupted["staged"] or {}
    if (status == "succeeded" and result and target.get("sha256") == expected_hash
            and target.get("inode") == checkpoint.get("inode") and result.get("sha256") == expected_hash
            and target.get("bytes") == result.get("bytes") == len(REPORT_BODY.encode())
            and result.get("path") == RELATIVE_REPORT and after["staged"] is None):
        return "proven_success"
    return "inconsistent"


def run_recovery(request):
    if request.boundary not in RECOVERY_BOUNDARIES[request.operation] or (
        request.evidence != "intact" and (request.operation, request.boundary) != ("write-file", "after_file_publish")
    ):
        raise GatewayError(422, "invalid_recovery_case")
    expected_spec = strict_json((SPECS / "recovery-v1.json").read_bytes())
    name = request.boundary if request.evidence == "intact" else request.boundary + ":" + request.evidence
    expected = expected_spec["cases"][request.operation][name]
    proposal = {"schema_version": "v2", "operation": request.operation}
    if request.operation == "write-file":
        proposal.update(path=RELATIVE_REPORT, content=REPORT_BODY)
    else:
        proposal.update(recipient="reviewer", body=NOTE)

    with disposable() as (root, gateway):
        row, _ = gateway.submit(REQUESTER, RECOVERY_KEY, proposal)
        identifier = row["id"]
        approve(gateway, identifier)
        before = inspect_recovery(root, identifier)
        try:
            started = time.perf_counter_ns()
            child = subprocess.run([sys.executable, "-B", "-m", "gateway.lab_worker", str(root), request.boundary],
                                   cwd=SOURCE, capture_output=True, timeout=12, check=False)
            child_ms = (time.perf_counter_ns() - started) / 1_000_000
        except subprocess.TimeoutExpired as exc:
            raise GatewayError(503, "recovery_worker_timeout") from exc
        try:
            marker = strict_json(child.stdout)
        except (ValueError, TypeError, RecursionError):
            marker = None
        if (child.returncode != 71 or not isinstance(marker, dict) or marker.get("execution_id") != identifier
                or marker.get("boundary") != request.boundary):
            raise GatewayError(503, "recovery_boundary_not_reached")
        interrupted = inspect_recovery(root, identifier)
        if request.evidence == "remove-staging":
            (root / "files/reports" / (".broker-stage-" + identifier)).unlink()
        started = time.perf_counter_ns()
        gateway.recover()
        gateway.run_once()
        recover_ms = (time.perf_counter_ns() - started) / 1_000_000
        replay, created = gateway.submit(REQUESTER, RECOVERY_KEY, proposal)
        after = inspect_recovery(root, identifier)
        verdict = classification(request.operation, identifier, interrupted, after, replay)
        timeline = [*before["events"], *interrupted["events"][len(before["events"]):], marker,
                    {"event": "process_terminated", "exit_code": child.returncode}]
        if request.evidence != "intact":
            timeline.append({"event": "staging_evidence_removed_in_disposable_runtime"})
        timeline.extend([{"event": "runtime_recovered"}, *after["events"][len(interrupted["events"]):],
                         {"event": "original_request_replayed", "created": created}])
        valid_audit = True
        try:
            verify_audit(gateway.store)
        except RuntimeError:
            valid_audit = False
        return {"operation": request.operation, "boundary": request.boundary, "evidence": request.evidence,
                "classification": verdict, "expected": expected,
                "independent_check_passed": verdict == expected and valid_audit and not created,
                "before": before, "interrupted": interrupted, "after": after, "timeline": timeline,
                "child_exit_code": child.returncode, "recovery_ms": recover_ms, "child_ms": child_ms,
                "provenance": provenance("recovery")}


TASKS = {
    "ticket": {
        "text": 'Create an incident ticket titled "Gateway latency" with detail "Inspect the local queue".',
        "intent": {"schema_version": "v2", "operation": "create-ticket", "title": "Gateway latency",
                   "detail": "Inspect the local queue"},
        "aliases": ("incident_ticket", "record_ticket"),
        "original": ("Create a local incident ticket.", "Record a ticket in the local issue register."),
        "reworded": ("Open an incident in the local ticket register.", "Create a scoped local issue record."),
    },
    "message": {
        "text": 'Send the reviewer a local message saying "Queue ready".',
        "intent": {"schema_version": "v2", "operation": "send-message", "recipient": "reviewer", "body": "Queue ready"},
        "aliases": ("send_note", "deliver_message"),
        "original": ("Deliver a note to a local inbox.", "Send a message to a scoped inbox."),
        "reworded": ("Put a message in a local inbox.", "Deliver a scoped inbox notification."),
    },
}


def variant_tools(task, variant):
    target = task["intent"]["operation"]
    schemas = {schema["properties"]["operation"]["const"]: schema
               for schema in INTENT.json_schema()["$defs"].values()}

    def tool(name, operation, description):
        schema = schemas[operation]
        fields = {key: value for key, value in schema["properties"].items() if key not in {"schema_version", "operation"}}
        return {"name": name, "description": description, "operation": operation,
                "input_schema": {"type": "object", "properties": fields,
                                 "required": sorted(key for key in schema["required"] if key in fields),
                                 "additionalProperties": False}}

    changed = [tool(name, target, description) for name, description in zip(task["aliases"], task[variant])]
    if variant != "original":
        changed.reverse()
    return changed + [tool("local-" + operation, operation, operation.replace("-", " ").capitalize() + ".")
                      for operation in sorted(schemas) if operation != target]


class RecordingProvider:
    def __init__(self, provider, response, trace):
        self.provider = provider
        self.response = response
        self.trace = trace

    def invoke(self, messages):
        started = time.perf_counter_ns()
        entry = {"messages": messages}
        self.trace.append(entry)
        try:
            raw = self.provider.invoke(messages) if self.provider else canonical(self.response)
            entry["response"] = raw[:6000] if isinstance(raw, str) else None
            entry["response_truncated"] = isinstance(raw, str) and len(raw) > 6000
            return raw
        except PlannerUnavailable:
            entry["error"] = "provider_unavailable"
            raise
        finally:
            entry["inference_ms"] = (time.perf_counter_ns() - started) / 1_000_000


def conventional_choice(task, tools, provider):
    messages = [
        {"role": "system", "content": "Choose one concrete tool from the catalog. Return exactly a JSON object "
          "with tool and arguments keys. Preserve the user's arguments.\n" + canonical(tools)},
        {"role": "user", "content": task["text"]},
    ]
    raw = provider.invoke(messages)
    if not isinstance(raw, str) or len(raw) > 6000:
        raise InvalidPlannerOutput()
    response = strict_json(raw)
    if not isinstance(response, dict) or set(response) != {"tool", "arguments"}:
        raise InvalidPlannerOutput()
    tool = next((item for item in tools if item["name"] == response["tool"]), None)
    arguments = response["arguments"]
    if tool is None or not isinstance(arguments, dict) or {"operation", "schema_version"} & arguments.keys():
        raise InvalidPlannerOutput()
    return INTENT.validate_python({"schema_version": "v2", "operation": tool["operation"], **arguments}), tool["name"]


def correct_world(expected, before, observed, result):
    if not result or result["status"] != "succeeded":
        return False
    if expected["operation"] == "create-ticket":
        target = {"title": expected["title"], "detail": expected["detail"], "status": "open", "revision": 1}
        return observed["tickets"] == [*before["tickets"], target] and observed["inbox"] == before["inbox"]
    target = {"recipient": expected["recipient"], "sender": "requester", "body": expected["body"]}
    return observed["inbox"] == [*before["inbox"], target] and observed["tickets"] == before["tickets"]


def comparison_cell(task, architecture, variant, live):
    tools = variant_tools(task, variant)
    decision = selected = result = None
    error = None
    trace = []
    expected = task["intent"]
    baseline_stub = {"tool": tools[0]["name"], "arguments": {key: value for key, value in expected.items()
                                                             if key not in {"schema_version", "operation"}}}
    provider = RecordingProvider(live, baseline_stub if architecture == "conventional" else expected, trace)
    try:
        if architecture == "conventional":
            decision, selected = conventional_choice(task, tools, provider)
        else:
            decision = Planner(provider).propose(task["text"])
    except PlannerUnavailable:
        error = "provider_unavailable"
    except (InvalidPlannerOutput, ValueError, TypeError, RecursionError, ValidationError):
        error = "invalid_decision"

    operation = expected["operation"]
    choices = [AdapterSpec(adapter_id=alias, operation=operation, implementation=operation,
                           priority=0 if alias == selected else 10) for alias in task["aliases"]]
    registry = Registry([*(entry for entry in default_adapters() if entry.operation != operation), *choices])
    with disposable(registry) as (root, gateway):
        initial = persisted_world(root)
        authorized = None
        if decision is not None:
            row, _ = gateway.submit(REQUESTER, "comparison-fixture-key", decision)
            authorized = row["status"] != "denied"
            if row["status"] == "awaiting_approval":
                approve(gateway, row["id"])
            gateway.run_once()
            result = gateway.get_execution(REVIEWER, row["id"])
        observed = persisted_world(root)
        detail = inspect_recovery(root, result["id"]) if result else None
        correct = correct_world(expected, initial, observed, result)
    return {"architecture": architecture, "variant": variant, "metadata": tools, "initial_sha256": fingerprint(initial),
            "policy_revision": Policy().revision, "registry_revision": registry.revision,
            "decision": decision.model_dump(exclude_none=True) if decision is not None else None,
            "model_selected_tool": selected, "adapter": result["adapter"] if result else None,
            "trace": trace, "observed": observed, "authorized": authorized, "correct_effect": correct,
            "status": result["status"] if result else error or "clarification_required",
            "attempted": bool(detail and detail["execution"]["attempt"]),
            "receipt": result["receipt"] if result else None, "events": detail["events"] if detail else []}


def run_comparison(request, settings):
    task = TASKS[request.task]
    base = {"task": request.task, "text": task["text"], "expected_intent": task["intent"], "mode": request.mode,
            "model": settings.model if request.mode == "live" else "deterministic fixture",
            "temperature": 0, "seed": None, "provenance": provenance("comparison"),
            "treatment": "Neutral description wording and display-order changes. Real effects use disposable local runtimes."}
    if request.mode == "live" and not settings.model:
        return {**base, "status": "not_run", "reason": "No local model configured", "cells": [], "metrics": {}}
    live = OllamaProvider(settings.ollama_url, settings.model, timeout_seconds=5) if request.mode == "live" else None
    cells = [comparison_cell(task, arch, variant, live) for arch in ("conventional", "protected")
             for variant in ("original", "reworded")]
    baseline_a, baseline_b, protected_a, protected_b = cells
    def context(cell):
        return cell["trace"][0]["messages"] if cell["trace"] else None
    metrics = {"equivalent_initial_world": len({cell["initial_sha256"] for cell in cells}) == 1,
               "correct_effect_cells": sum(cell["correct_effect"] for cell in cells),
               "provider_errors": sum(cell["status"] == "provider_unavailable" for cell in cells),
               "protected_context_equal": context(protected_a) == context(protected_b),
               "conventional_selection_changed": baseline_a["model_selected_tool"] != baseline_b["model_selected_tool"],
               "conventional_intent_changed": baseline_a["decision"] != baseline_b["decision"],
               "protected_intent_changed": protected_a["decision"] != protected_b["decision"],
               "final_state_equal": len({fingerprint(cell["observed"]) for cell in cells}) == 1,
               "policy_rejections": sum(cell["authorized"] is False for cell in cells),
               "denied_cells_with_state_change": sum(cell["authorized"] is False and
                  fingerprint(cell["observed"]) != cell["initial_sha256"] for cell in cells)}
    status = "not_run" if metrics["provider_errors"] == len(cells) else "completed"
    return {**base, "status": status, "metrics": metrics, "cells": cells}


class LabReports:
    def __init__(self, store):
        self.store = store

    def record(self, principal, kind, report):
        raw = canonical(report)
        if len(raw.encode("utf-8")) > 128 * 1024:
            raise GatewayError(503, "lab_report_size_exceeded")
        identifier, created = uuid.uuid4().hex, time.time()
        with self.store.connect(write=True) as db:
            check_actor(db, principal)
            db.execute("INSERT INTO lab_runs VALUES (?,?,?,?,?,?)",
                       (identifier, principal.name, principal.scope, kind, created, raw))
            db.execute("""DELETE FROM lab_runs WHERE id IN (SELECT id FROM lab_runs
                WHERE owner=? AND scope=? ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET 16)""",
                       (principal.name, principal.scope))
        return {"id": identifier, "kind": kind, "created_at": created, "report": report}

    def get(self, principal, identifier=None):
        with self.store.connect() as db:
            check_actor(db, principal)
            if identifier:
                result = db.execute("""SELECT id,kind,created_at,report_json FROM lab_runs
                    WHERE id=? AND owner=? AND scope=?""", (identifier, principal.name, principal.scope)).fetchone()
                if result is None:
                    raise GatewayError(404, "lab_run_not_found")
                return {"id": result["id"], "kind": result["kind"], "created_at": result["created_at"],
                        "report": strict_json(result["report_json"])}
            rows = db.execute("""SELECT id,kind,created_at FROM lab_runs WHERE owner=? AND scope=?
                ORDER BY created_at DESC,id DESC LIMIT 16""", (principal.name, principal.scope)).fetchall()
            return [dict(row) for row in rows]

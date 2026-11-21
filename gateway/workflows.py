"""Bounded declarative workflows executed through the ordinary gateway."""
import time
import uuid
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError

from .contracts import Closed, INTENT, OPERATIONS, Slug, canonical, fingerprint, strict_json
from .engine import KEY, GatewayError, check_actor, get_row
from .planner import propose_json
from .store import TERMINAL_STATES, select_principal

MAX_NODES = 12
MAX_ACTIVE_WORKFLOWS = 24
MAX_WORKFLOWS = 1_024
LiteralText = Annotated[str, StringConstraints(max_length=65536)]

# Only these fields are retained as dependency values. Free-text business records,
# read-file contents, and the transport's raw output are outside this interface.
OUTPUT_TYPES = {
    "create-ticket": {"ticket_id": str, "revision": int, "status": str},
    "update-ticket": {"ticket_id": str, "revision": int, "status": str},
    "read-file": {"path": str, "bytes": int, "sha256": str},
    "write-file": {"path": str, "bytes": int, "sha256": str},
    "send-message": {"message_id": str, "recipient": str},
    "lookup-record": {},
}
DYNAMIC = {
    "body": str, "title": str, "detail": str, "content": str, "record_id": str,
    "ticket_id": str, "expected_revision": int,
}


class Reference(Closed):
    step: Slug
    field: Slug


class Ref(Closed):
    ref: Reference


class Join(Closed):
    join: Annotated[list[LiteralText | Reference], Field(min_length=1, max_length=10)]


class Node(Closed):
    id: Slug
    operation: str
    depends_on: Annotated[list[Slug], Field(max_length=MAX_NODES)] = Field(default_factory=list)
    arguments: Annotated[dict[Slug, LiteralText | int | Ref | Join], Field(max_length=8)]


class WorkflowPlan(Closed):
    schema_version: Literal["workflow-v1"]
    nodes: Annotated[list[Node], Field(min_length=1, max_length=MAX_NODES)]


class InvalidWorkflow(ValueError):
    pass


def propose_workflow(provider, text):
    instructions = """Interpret the request as one bounded workflow using the trusted structures below.
Each node has a unique ID, a domain operation, explicit dependencies and operation arguments.
Use literal paths and recipients, and preserve the user's intended values. A reference is
{"ref":{"step":"ticket","field":"ticket_id"}} and a string join can contain literals or references
represented as {"step":"ticket","field":"ticket_id"}. Refer only to declared dependencies.
Return only a single workflow object or {"error":"clarification_required"} if details are missing.
The draft carries no authorization and each resolved effect requires its ordinary review."""
    schema = {
        "workflow": WorkflowPlan.model_json_schema(), "operations": INTENT.json_schema(),
        "referenceable_outputs": {operation: {name: kind.__name__ for name, kind in fields.items()}
                                  for operation, fields in OUTPUT_TYPES.items()},
    }

    def validate(raw):
        plan = WorkflowPlan.model_validate(raw)
        compile_plan(plan)
        return plan

    return propose_json(provider, text, instructions + "\n" + canonical(schema), validate)


def reference_type(ref, node, nodes):
    if ref.step not in node.depends_on:
        raise InvalidWorkflow("workflow_reference_requires_dependency")
    kind = OUTPUT_TYPES[nodes[ref.step].operation].get(ref.field)
    if kind is None:
        raise InvalidWorkflow("workflow_unknown_output")
    return kind


def stub_intent(node, nodes):
    fields = {}
    for name, value in node.arguments.items():
        target = DYNAMIC.get(name)
        if isinstance(value, Ref):
            if target is None or target is not reference_type(value.ref, node, nodes):
                raise InvalidWorkflow("workflow_reference_type_mismatch")
            fields[name] = "placeholder" if target is str else 1
        elif isinstance(value, Join):
            if target is not str or any(isinstance(part, Reference) and reference_type(part, node, nodes) is not str
                                        for part in value.join):
                raise InvalidWorkflow("workflow_reference_type_mismatch")
            fields[name] = "".join(part if isinstance(part, str) else "x" for part in value.join)
        else:
            fields[name] = value
    try:
        return INTENT.validate_python({"schema_version": "v2", "operation": node.operation, **fields})
    except ValidationError as exc:
        raise InvalidWorkflow("workflow_invalid_node_arguments") from exc


def compile_plan(raw):
    try:
        data = raw.model_dump() if isinstance(raw, WorkflowPlan) else raw
        if len(canonical(data).encode("utf-8")) > 160_000:
            raise InvalidWorkflow("workflow_size_limit")
        plan = WorkflowPlan.model_validate(data)
    except InvalidWorkflow:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidWorkflow("invalid_workflow") from exc
    nodes = {node.id: node for node in plan.nodes}
    if len(nodes) != len(plan.nodes) or any(node.operation not in OPERATIONS for node in plan.nodes):
        raise InvalidWorkflow("workflow_duplicate_node_or_unknown_operation")
    if sum(len(node.depends_on) for node in plan.nodes) > 24:
        raise InvalidWorkflow("workflow_edge_limit")
    depth = {}
    samples = {}
    for node in plan.nodes:
        if len(set(node.depends_on)) != len(node.depends_on) or not set(node.depends_on) <= nodes.keys():
            raise InvalidWorkflow("workflow_invalid_dependency")
        if {"schema_version", "operation"} & node.arguments.keys():
            raise InvalidWorkflow("workflow_reserved_argument")
        samples[node.id] = stub_intent(node, nodes)
    order = []
    while len(order) < len(nodes):
        next_node = next((node for node in sorted(nodes) if node not in depth
                          and all(parent in depth for parent in nodes[node].depends_on)), None)
        if next_node is None:
            raise InvalidWorkflow("workflow_cycle")
        depth[next_node] = 1 + max((depth[parent] for parent in nodes[next_node].depends_on), default=0)
        if depth[next_node] > 8:
            raise InvalidWorkflow("workflow_depth_limit")
        order.append(next_node)
    return nodes, order, samples


def resolved_intent(node, prior):
    arguments = {}
    for name, value in node.arguments.items():
        if isinstance(value, Ref):
            arguments[name] = prior[value.ref.step][value.ref.field]
        elif isinstance(value, Join):
            arguments[name] = "".join(part if isinstance(part, str) else prior[part.step][part.field] for part in value.join)
        else:
            arguments[name] = value
    return INTENT.validate_python({"schema_version": "v2", "operation": node.operation, **arguments})


def normalized(node):
    data = node.model_dump()
    data["depends_on"] = sorted(data["depends_on"])
    return data


def plan_hash(definitions):
    return fingerprint({"schema_version": "workflow-v1", "nodes": sorted(definitions, key=lambda node: node["id"])})


class Workflows:
    def __init__(self, gateway):
        self.gateway = gateway
        self.store = gateway.store

    def compile(self, principal, raw):
        started = time.perf_counter_ns()
        try:
            nodes, order, samples = compile_plan(raw)
        except InvalidWorkflow as exc:
            raise GatewayError(422, str(exc)) from exc
        self.gateway.measured("workflow_compile", started)
        if principal.role != "reviewer" and not all(self.gateway.policy.authorize(principal, sample)
                                                    for sample in samples.values()):
            raise GatewayError(403, "workflow_policy_denied")
        return nodes, order

    def submit(self, principal, key, raw):
        if principal.role == "reviewer":
            raise GatewayError(403, "submission_forbidden")
        if not KEY.fullmatch(key):
            raise GatewayError(422, "invalid_idempotency_key")
        started = time.perf_counter_ns()
        try:
            nodes, order, samples = compile_plan(raw)
        except InvalidWorkflow as exc:
            raise GatewayError(422, str(exc)) from exc
        self.gateway.measured("workflow_compile", started)
        definitions = [normalized(nodes[name]) for name in order]
        digest = plan_hash(definitions)
        with self.store.connect(write=True) as db:
            check_actor(db, principal)
            prior = db.execute("SELECT * FROM workflows WHERE requester=? AND scope=? AND request_key=?",
                               (principal.name, principal.scope, key)).fetchone()
            if prior is not None:
                if prior["plan_hash"] != digest:
                    raise GatewayError(409, "idempotency_conflict")
                return self.view(db, prior, principal), False
            if not all(self.gateway.policy.authorize(principal, sample) for sample in samples.values()):
                raise GatewayError(403, "workflow_policy_denied")
            total, active = db.execute("""SELECT COUNT(*), COUNT(CASE WHEN status IN ('running','awaiting_approval')
                THEN 1 END) FROM workflows""").fetchone()
            if total >= MAX_WORKFLOWS:
                raise GatewayError(429, "workflow_history_full")
            if active >= MAX_ACTIVE_WORKFLOWS:
                raise GatewayError(429, "workflow_queue_full")
            identifier = uuid.uuid4().hex
            db.execute("""INSERT INTO workflows VALUES (?,?,?,?,?,?,?,'running',?)""",
                       (identifier, principal.name, principal.scope, key, digest, self.gateway.policy_revision,
                        self.gateway.current_registry(db).revision, self.gateway.clock()))
            db.executemany("""INSERT INTO workflow_nodes
                (workflow_id,node_id,position,definition_json,status) VALUES (?,?,?,?,'waiting')""",
                [(identifier, node["id"], position, canonical(node)) for position, node in enumerate(definitions)])
            return self.view(db, self.row(db, identifier), principal), True

    @staticmethod
    def row(db, identifier):
        return db.execute("SELECT * FROM workflows WHERE id=?", (identifier,)).fetchone()

    @staticmethod
    def children(db, identifier):
        return db.execute("""SELECT * FROM workflow_nodes WHERE workflow_id=? ORDER BY position""",
                          (identifier,)).fetchall()

    def view(self, db, row, principal):
        result = {key: row[key] for key in ("id", "status", "created_at", "requester")}
        if principal.role == "reviewer":
            result.update(policy_revision=row["policy_revision"], registry_revision=row["registry_revision"])
        steps = []
        for node in self.children(db, row["id"]):
            definition = strict_json(node["definition_json"])
            execution = get_row(db, node["execution_id"]) if node["execution_id"] else None
            status, reason = node["status"], node["reason"]
            if status == "linked" and execution:
                status, reason = execution["status"], execution["reason"]
            steps.append({"id": node["node_id"], "operation": definition["operation"], "depends_on": definition["depends_on"],
                          "status": status, "reason": reason, "execution_id": node["execution_id"],
                          "outputs": strict_json(node["outputs_json"]) if node["outputs_json"] else None})
        result["nodes"] = steps
        return result

    def get(self, principal, identifier=None):
        with self.store.connect() as db:
            db.execute("BEGIN")
            check_actor(db, principal)
            if identifier:
                row = self.row(db, identifier)
                if row is None or row["scope"] != principal.scope or (principal.role != "reviewer"
                        and row["requester"] != principal.name):
                    raise GatewayError(404, "workflow_not_found")
                return self.view(db, row, principal)
            rows = db.execute("""SELECT * FROM workflows WHERE scope=? AND (? OR requester=?)
                ORDER BY created_at DESC,id DESC LIMIT 24""",
                              (principal.scope, principal.role == "reviewer", principal.name)).fetchall()
            return [self.view(db, row, principal) for row in rows]

    @staticmethod
    def write_node(db, workflow, node, state, *, reason=None, outputs=None, execution_id=None):
        db.execute("""UPDATE workflow_nodes SET status=?,reason=?,outputs_json=?,execution_id=COALESCE(?,execution_id)
            WHERE workflow_id=? AND node_id=?""",
                   (state, reason, canonical(outputs) if outputs is not None else None, execution_id, workflow, node))

    def capture(self, db, workflow):
        states, outputs = {}, {}
        rows = self.children(db, workflow)
        for node in rows:
            identifier = node["node_id"]
            state = node["status"]
            if state == "linked":
                execution = get_row(db, node["execution_id"])
                state = execution["status"]
                if state in TERMINAL_STATES:
                    values = None
                    if state == "succeeded":
                        receipt = strict_json(execution["receipt_json"]) if execution["receipt_json"] else {}
                        values = {key: receipt[key] for key, kind in OUTPUT_TYPES[execution["operation"]].items()
                                  if key in receipt and type(receipt[key]) is kind}
                    self.write_node(db, workflow, identifier, state, reason=execution["reason"], outputs=values)
                    if values is not None:
                        outputs[identifier] = values
            elif node["outputs_json"]:
                outputs[identifier] = strict_json(node["outputs_json"])
            states[identifier] = state
        return rows, states, outputs

    def tick(self):
        with self.store.connect(write=True) as db:
            workflows = db.execute("""SELECT * FROM workflows WHERE status IN ('running','awaiting_approval')
                ORDER BY created_at,id LIMIT ?""", (MAX_ACTIVE_WORKFLOWS,)).fetchall()
            for workflow in workflows:
                self.progress(db, workflow)

    def progress(self, db, workflow):
        identifier = workflow["id"]
        rows, states, outputs = self.capture(db, identifier)
        definitions = [strict_json(row["definition_json"]) for row in rows]
        principal = select_principal(db, workflow["requester"])
        stale = None
        if plan_hash(definitions) != workflow["plan_hash"]:
            stale = "workflow_plan_integrity_failed"
        elif principal is None or principal.scope != workflow["scope"]:
            stale = "workflow_principal_revoked"
        elif workflow["policy_revision"] != self.gateway.policy_revision:
            stale = "policy_changed"
        elif workflow["registry_revision"] != self.gateway.current_registry(db).revision:
            stale = "registry_changed"
        for data in definitions:
            name = data["id"]
            if states[name] != "waiting":
                continue
            dependencies = [states[parent] for parent in data["depends_on"]]
            if stale or any(state in (TERMINAL_STATES | {"skipped"}) - {"succeeded"} for state in dependencies):
                self.write_node(db, identifier, name, "skipped", reason=stale or "predecessor_not_successful")
                states[name] = "skipped"
            elif all(state == "succeeded" for state in dependencies):
                states[name] = self.link(db, workflow, name, principal, data, outputs)
        status = self.status(states.values())
        if status != workflow["status"]:
            db.execute("UPDATE workflows SET status=? WHERE id=?", (status, identifier))
            if status not in {"running", "awaiting_approval"}:
                for node in definitions:
                    node.pop("arguments", None)
                    db.execute("UPDATE workflow_nodes SET definition_json=? WHERE workflow_id=? AND node_id=?",
                               (canonical(node), identifier, node["id"]))

    def link(self, db, workflow, name, principal, data, outputs):
        identifier = workflow["id"]
        try:
            intent = resolved_intent(Node.model_validate(data), outputs)
            row, new = self.gateway.submit_in_transaction(db, principal, f"wf_{identifier}_{name}", intent)
            if not new:
                raise RuntimeError("Workflow key was already used without a link")
        except GatewayError as exc:
            if exc.code == "execution_queue_full":
                return "waiting"
            self.write_node(db, identifier, name, "failed", reason=exc.code)
            return "failed"
        except (KeyError, ValidationError, TypeError):
            self.write_node(db, identifier, name, "failed", reason="workflow_value_unavailable_or_invalid")
            return "failed"
        state = row["status"] if row["status"] in TERMINAL_STATES else "linked"
        self.write_node(db, identifier, name, state, reason=row["reason"], execution_id=row["id"])
        return row["status"]

    @staticmethod
    def status(states):
        states = set(states)
        if states & {"waiting", "ready", "executing", "awaiting_approval"}:
            return "awaiting_approval" if "awaiting_approval" in states else "running"
        if "outcome_unknown" in states:
            return "outcome_unknown"
        if states == {"succeeded"}:
            return "succeeded"
        return "partial" if "succeeded" in states else "failed"

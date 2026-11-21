import { useState, type RefObject } from "react";
import { explain, gateway, itemPath, LABELS, type Operation, type Role } from "../lib/gateway";
import { useQuery } from "../lib/useGateway";
import { Empty, Icon, timestamp } from "./ConsoleViews";
import { EvidenceDisclosure, Issue, Outcome, Panel, useAction } from "./WorkbenchShared";

type Step = {
  id: string;
  operation: Operation;
  depends_on: string[];
  status: string;
  reason: string | null;
  execution_id: string | null;
  outputs: Record<string, unknown> | null;
};
type Workflow = {
  id: string; status: string; created_at: number; nodes: Step[];
  requester?: string; policy_revision?: string; registry_revision?: string;
};
type Compilation = { order: string[] };
type WorkflowProposal = Compilation & (
  { status: "proposed"; plan: Record<string, unknown> } | { status: "clarification_required"; plan: null }
);

function incidentExample(path = "reports/gateway-incident.txt") {
  const ticket = { step: "ticket", field: "ticket_id" };
  return JSON.stringify({
    schema_version: "workflow-v1",
    nodes: [
      {
        id: "ticket", operation: "create-ticket", depends_on: [],
        arguments: { title: "Gateway latency incident", detail: "Investigate the elevated request latency." },
      },
      {
        id: "report", operation: "write-file", depends_on: ["ticket"],
        arguments: { path, content: { join: ["Incident ticket ", ticket, "\nInitial observation: elevated request latency.\n"] } },
      },
      {
        id: "notify", operation: "send-message", depends_on: ["ticket", "report"],
        arguments: { recipient: "reviewer", body: { join: ["Incident ", ticket, " was recorded at ", { step: "report", field: "path" }, "."] } },
      },
    ],
  }, null, 2);
}

function normalizedDraft(draft: string) {
  try {
    const parsed: unknown = JSON.parse(draft);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
    return JSON.stringify(parsed);
  } catch {
    throw new Error("Enter one valid JSON workflow object.");
  }
}

const TERMINAL = new Set(["succeeded", "failed", "partial", "partially_succeeded", "partial_success", "partially_completed",
  "denied", "cancelled", "completed_with_errors", "outcome_unknown", "expired"]);

function workflowLevels(steps: Step[]) {
  const remaining = [...steps];
  const ranked: { step: Step; depth: number }[] = [];
  const depthById = new Map<string, number>();
  while (remaining.length) {
    let progress = false;
    for (let index = 0; index < remaining.length;) {
      const step = remaining[index];
      if (step.depends_on.every(id => depthById.has(id))) {
        const depth = step.depends_on.length ? Math.max(...step.depends_on.map(id => depthById.get(id)!)) + 1 : 0;
        ranked.push({ step, depth });
        depthById.set(step.id, depth);
        remaining.splice(index, 1);
        progress = true;
      } else {
        index += 1;
      }
    }
    if (!progress) return ranked.concat(remaining.map(step => ({ step, depth: 0 })));
  }
  return ranked;
}

function WorkflowInspector({ workflow, onExecution, onApproval, reviewer }: {
  workflow: Workflow; onExecution: (id: string) => void; onApproval: (id: string) => void; reviewer: boolean;
}) {
  return <Panel title="Workflow inspector" accessory={<Outcome value={workflow.status} />}>
    <div className="detail-body">
      <dl className="detail-meta">
        <div><dt>Workflow ID</dt><dd><code>{workflow.id}</code></dd></div>
        <div><dt>Submitted</dt><dd>{timestamp(workflow.created_at)}</dd></div>
        {reviewer && workflow.requester && <div><dt>Requester</dt><dd>{workflow.requester}</dd></div>}
      </dl>
      {reviewer && (workflow.policy_revision || workflow.registry_revision) && <EvidenceDisclosure title="Policy and registry revisions"
        value={{ policy: workflow.policy_revision, registry: workflow.registry_revision }} />}
      <ol className="workflow-graph" aria-label="Workflow dependency graph">
        {workflowLevels(workflow.nodes).map(({ step, depth }) => (
          <li className={`workflow-node depth-${Math.min(depth, 3)}`} key={step.id}>
            <div className="workflow-node-heading">
              <div><code>{step.id}</code><strong>{LABELS[step.operation] ?? step.operation}</strong></div>
              <Outcome value={step.status} />
            </div>
            {step.depends_on.length > 0 && <div className="workflow-parents">
              <Icon name="arrow" size={14} /><span>Requires</span><span>{step.depends_on.map(id => <code key={id}>{id}</code>)}</span>
            </div>}
            {step.reason && <p className="execution-reason">{explain(step.reason)}</p>}
            {step.outputs && <EvidenceDisclosure title="Resolved outputs" value={step.outputs} />}
            {step.execution_id && <div className="workflow-links">
              <code>{step.execution_id.slice(0, 12)}</code>
              {reviewer && step.status === "awaiting_approval" ? (
                <button type="button" className="text-button" onClick={() => onApproval(step.execution_id!)}>Review step</button>
              ) : (
                <button type="button" className="text-button" onClick={() => onExecution(step.execution_id!)}>Open execution</button>
              )}
            </div>}
          </li>
        ))}
      </ol>
    </div>
  </Panel>;
}

export function WorkflowWorkbench({ token, role, version, onExecution, onApproval, requestKeys }: {
  token: string; role: Role; version: number; onExecution: (id: string) => void; onApproval: (id: string) => void;
  requestKeys: RefObject<Map<string, string>>;
}) {
  const [draft, setDraft] = useState(() => incidentExample());
  const [naturalText, setNaturalText] = useState("");
  const [proposalNotice, setProposalNotice] = useState("");
  const [errorTarget, setErrorTarget] = useState<"proposal" | "editor">("editor");
  const [compilation, setCompilation] = useState<Compilation | null>(null);
  const [selection, setSelection] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<Workflow | null>(null);
  const [revision, setRevision] = useState(0);
  const { busy, error, run, clearError } = useAction();
  const list = useQuery<Workflow[]>("/workflows", token, version + revision, 2400);
  const id = selection ?? list.data?.[0]?.id;
  const listed = list.data?.find(workflow => workflow.id === id);
  const saved = accepted?.id === id ? accepted : null;
  const summary = listed ?? saved;
  const detail = useQuery<Workflow>(id ? itemPath("/workflows", id) : null, token, version + revision,
    summary && TERMINAL.has(summary.status) ? 0 : 1600);
  const current = detail.data ?? summary;
  const reviewer = role === "reviewer";

  function edit(value: string) {
    setDraft(value);
    setCompilation(null);
    setProposalNotice("");
    clearError();
  }

  async function propose() {
    const text = naturalText.trim();
    if (!text || busy) return;
    setErrorTarget("proposal");
    await run("propose", async () => {
      setProposalNotice("");
      const result = await gateway<WorkflowProposal>("/workflows/proposals", token, {
        method: "POST", body: JSON.stringify({ text }),
      });
      if (result.status === "proposed") {
        setDraft(JSON.stringify(result.plan, null, 2));
        setCompilation({ order: result.order });
        setProposalNotice("Draft ready. Review the workflow JSON before submitting.");
      } else {
        setProposalNotice("Add details about the steps and how they depend on each other.");
      }
    });
  }

  async function compile() {
    setErrorTarget("editor");
    await run("compile", async () => {
      normalizedDraft(draft);
      setCompilation(await gateway<Compilation>("/workflows/compile", token, { method: "POST", body: draft }));
    });
  }

  async function submit() {
    setErrorTarget("editor");
    await run("submit", async () => {
      const signature = normalizedDraft(draft);
      const key = requestKeys.current.get(signature) ?? crypto.randomUUID();
      requestKeys.current.set(signature, key);
      const result = await gateway<Workflow>("/workflows", token, {
        method: "POST", body: draft, headers: { "Idempotency-Key": key },
      });
      requestKeys.current.delete(signature);
      setAccepted(result);
      setSelection(result.id);
      setRevision(value => value + 1);
    });
  }

  const inspector = current ? (
    <WorkflowInspector workflow={current} reviewer={reviewer} onExecution={onExecution} onApproval={onApproval} />
  ) : detail.error ? null : (
    <Panel title="Workflow inspector">
      {id ? <p className="list-status" role="status">Loading workflow…</p> :
        <Empty icon="workflows" title="Select a workflow">Steps appear here with their dependencies, approvals and results.</Empty>}
    </Panel>
  );

  return <div className={`workbench-layout${reviewer ? " review-layout" : ""}`}>
    <div className="view-stack">
      {!reviewer && <Panel title="Workflow editor">
        <details className="workbench-disclosure workflow-proposal">
          <summary>Draft from natural language</summary>
          <form className="view-stack" onSubmit={event => { event.preventDefault(); void propose(); }}>
            <label className="input-group">
              <span>Workflow request</span>
              <textarea value={naturalText} rows={3} maxLength={16000} required disabled={!!busy}
                placeholder="Open an incident ticket, write a report, then notify the reviewer."
                onChange={event => { setNaturalText(event.target.value); setProposalNotice(""); if (errorTarget === "proposal") clearError(); }} />
            </label>
            {proposalNotice && <p className="workbench-notice" role="status">{proposalNotice}</p>}
            {errorTarget === "proposal" && <Issue error={error} />}
            <div className="workbench-action-row">
              <button className="button secondary" disabled={!!busy || !naturalText.trim()}>
                {busy === "propose" ? "Drafting…" : "Propose workflow"}
              </button>
            </div>
          </form>
        </details>
        <form onSubmit={event => { event.preventDefault(); void submit(); }}>
          <div className="workbench-body">
            <label className="input-group">
              <span>Workflow JSON</span>
              <textarea className="workflow-editor" value={draft} rows={15} maxLength={96000} spellCheck={false}
                autoCapitalize="off" autoComplete="off" disabled={!!busy} onChange={event => edit(event.target.value)} required />
            </label>
            <button type="button" className="text-button" disabled={!!busy}
              onClick={() => edit(incidentExample(`reports/incident-${Date.now()}.txt`))}>Load incident example</button>
            {compilation && <div className="workbench-notice" role="status">
              <strong>Plan is valid</strong>
              <ol className="workbench-order" aria-label="Topological order">
                {compilation.order.map(id => <li key={id}><code>{id}</code></li>)}
              </ol>
            </div>}
            {errorTarget === "editor" && <Issue error={error} />}
          </div>
          <div className="panel-footer workbench-actions">
            <button type="button" className="button secondary" disabled={!!busy} onClick={() => void compile()}>
              {busy === "compile" ? "Validating…" : "Validate plan"}
            </button>
            <button className="button primary" disabled={!!busy}>{busy === "submit" ? "Submitting…" : "Submit workflow"}</button>
          </div>
        </form>
      </Panel>}
      <Panel title="Recent workflows">
        <Issue error={list.error} />
        {list.data ? list.data.length ? <ul className="approval-list">
          {list.data.map(workflow => <li key={workflow.id}>
            <button type="button" aria-pressed={workflow.id === id} onClick={() => setSelection(workflow.id)}>
              <span><strong>{workflow.nodes.length} {workflow.nodes.length === 1 ? "step" : "steps"}</strong>
                <small>{timestamp(workflow.created_at)} · <code>{workflow.id.slice(0, 12)}</code></small></span>
              <Outcome value={workflow.status} />
            </button>
          </li>)}
        </ul> : <Empty icon="workflows" title="No workflows yet">Submitted plans appear here.</Empty> : !list.error &&
          <p className="list-status" role="status">Loading workflows…</p>}
      </Panel>
    </div>
    <div className="view-stack">
      <Issue error={detail.error} />
      {inspector}
    </div>
  </div>;
}

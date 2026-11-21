import { useState, type FormEvent } from "react";
import { explain, gateway, itemPath } from "../lib/gateway";
import { useQuery } from "../lib/useGateway";
import { Empty, timestamp } from "./ConsoleViews";
import { Evidence, EvidenceDisclosure, Issue, label, Outcome, Panel, useAction } from "./WorkbenchShared";

export type LabKind = "comparison" | "recovery";
type RunSummary = { id: string; kind: LabKind; created_at: number };
type Run = RunSummary & { report: Record<string, unknown> };

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function text(value: unknown) {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

function status(value: unknown) {
  return text(value) || text(object(value).status);
}

function LabArchive({ kind, runs, selected, onSelect }: {
  kind: LabKind; runs: RunSummary[] | null; selected?: string; onSelect: (id: string) => void;
}) {
  const rows = runs?.filter(run => run.kind === kind);
  return <Panel title="Previous runs">
    {rows ? rows.length ? <ul className="approval-list">
      {rows.map(run => <li key={run.id}>
        <button type="button" onClick={() => onSelect(run.id)} aria-pressed={run.id === selected}>
          <span><strong>{timestamp(run.created_at)}</strong><small><code>{run.id.slice(0, 12)}</code></small></span>
        </button>
      </li>)}
    </ul> : <Empty icon={kind} title="No runs yet">Completed runs appear here for inspection.</Empty> :
      <p className="list-status" role="status">Loading run history…</p>}
  </Panel>;
}

function ComparisonReport({ report }: { report: Record<string, unknown> }) {
  const cells = Array.isArray(report.cells) ? report.cells.map(object) : [];
  const [selection, setSelection] = useState("");
  const cellKey = (cell: Record<string, unknown>) => `${text(cell.architecture)}:${text(cell.variant)}`;
  const selected = cells.find(cell => cellKey(cell) === selection) ?? cells[0];
  const metrics = object(report.metrics);
  const comparisonStatus = status(report.model_evaluation) || status(report.model_security) || status(report.status);
  const reason = text(report.reason);
  const model = text(report.model);
  const task = text(report.text);
  return <div className="view-stack">
    <Panel title="Matched outcomes" accessory={comparisonStatus && <Outcome value={comparisonStatus} />}>
      {(reason || task || model) && <div className="workbench-run-note">
        {task && <p>{task}</p>}
        {model && <p><strong>Provider</strong> {model}</p>}
        {reason && <p>{reason}</p>}
      </div>}
      {cells.length > 0 ? <div className="table-scroll">
        <table className="comparison-matrix">
          <thead><tr><th scope="col">Architecture</th><th scope="col">Original metadata</th><th scope="col">Reworded metadata</th></tr></thead>
          <tbody>{(["conventional", "protected"] as const).map(architecture => <tr key={architecture}>
            <th scope="row">{architecture === "conventional" ? "Conventional agent" : "Protected broker"}</th>
            {(["original", "reworded"] as const).map(variant => {
              const cell = cells.find(item => item.architecture === architecture && item.variant === variant);
              const outcome = cell ? status(cell.status) : "";
              return <td key={variant}>
                {cell ? <button type="button" className="matrix-cell" aria-pressed={selected ? cellKey(selected) === cellKey(cell) : false}
                  aria-label={`${label(architecture)} · ${label(variant)} metadata`} onClick={() => setSelection(cellKey(cell))}>
                  {outcome && <Outcome value={outcome} />}
                  <code>{text(cell.adapter) || text(cell.selected_adapter) || "—"}</code>
                </button> : <span className="muted">No cell recorded</span>}
              </td>;
            })}
          </tr>)}</tbody>
        </table>
      </div> : <Empty icon="comparison" title="No comparison cells recorded">Open the run record below for the available evidence.</Empty>}
      {Object.keys(metrics).length > 0 && <dl className="comparison-metrics">
        {Object.entries(metrics).map(([name, value]) => (
          <div key={name}><dt>{label(name)}</dt><dd>{value == null ? "—" : typeof value === "boolean" ? value ? "Yes" : "No" :
            typeof value === "object" ? JSON.stringify(value) : String(value)}</dd></div>
        ))}
      </dl>}
    </Panel>
    {selected && <Panel title="Cell evidence" accessory={<span className="muted">{label(text(selected.architecture))} · {label(text(selected.variant))}</span>}>
      <div className="workbench-body">
        <dl className="detail-meta">
          {text(selected.model_selected_tool) && <div><dt>Model selection</dt><dd><code>{text(selected.model_selected_tool)}</code></dd></div>}
          {text(selected.adapter) && <div><dt>Gateway adapter</dt><dd><code>{text(selected.adapter)}</code></dd></div>}
          {typeof selected.authorized === "boolean" && <div><dt>Policy decision</dt><dd>{selected.authorized ? "Allowed" : "Denied"}</dd></div>}
          {typeof selected.attempted === "boolean" && <div><dt>Attempt recorded</dt><dd>{selected.attempted ? "Yes" : "No"}</dd></div>}
          {typeof selected.correct_effect === "boolean" && <div><dt>Expected effect</dt><dd>{selected.correct_effect ? "Matched" : "Did not match"}</dd></div>}
        </dl>
        {"decision" in selected && <div className="evidence-section"><h3>Model decision</h3><Evidence value={selected.decision} title="Model decision" /></div>}
        {"observed" in selected && <div className="evidence-section"><h3>Observed persistent state</h3><Evidence value={selected.observed} title="Observed persistent state" /></div>}
        {"metadata" in selected && <EvidenceDisclosure value={selected.metadata} title="Registry metadata for this cell" />}
        {"trace" in selected && <EvidenceDisclosure value={selected.trace} title="Model context and raw output" />}
      </div>
    </Panel>}
  </div>;
}

function RecoveryReport({ report }: { report: Record<string, unknown> }) {
  const timeline = Array.isArray(report.timeline) ? report.timeline : [];
  const classification = text(report.classification) || status(report.status);
  const expected = text(report.expected);
  const boundary = text(report.boundary);
  const reason = text(report.reason);
  const verified = report.independent_check_passed;
  return <div className="view-stack">
    <Panel title="Recovery result" accessory={classification && <Outcome value={classification} />}>
      <div className="workbench-body">
        {reason && <Issue error={explain(reason)} />}
        <dl className="detail-meta">
          {boundary && <div><dt>Boundary</dt><dd>{label(boundary)}</dd></div>}
          {expected && <div><dt>Expected</dt><dd>{label(expected)}</dd></div>}
          {report.child_exit_code !== undefined && <div><dt>Child exit code</dt><dd><code>{text(report.child_exit_code)}</code></dd></div>}
          {typeof verified === "boolean" && <div><dt>Independent check</dt><dd>{verified ? "Passed" : "Failed"}</dd></div>}
        </dl>
        {timeline.length > 0 && <ol className="timeline" aria-label="Recorded recovery timeline">
          {timeline.map((item, index) => {
            const event = object(item);
            const title = text(item) || text(event.event) || text(event.phase) || text(event.state) || text(event.status);
            const detail = text(event.detail) || text(event.boundary);
            return <li key={index}>
              <span className="timeline-dot" aria-hidden="true" />
              <div>
                {title ? <div className="timeline-title"><strong>{label(title)}</strong></div> :
                  <Evidence value={item} title={`Recorded event ${index + 1}`} />}
                {detail && title && detail !== title && <span className="muted">{label(detail)}</span>}
              </div>
            </li>;
          })}
        </ol>}
        {"interrupted" in report && <EvidenceDisclosure title="State at interruption" value={report.interrupted} />}
      </div>
    </Panel>
    {("before" in report || "after" in report) && <div className="lab-snapshot-grid">
      {"before" in report && <Panel title="Before recovery"><div className="workbench-body"><Evidence value={report.before} title="State before recovery" /></div></Panel>}
      {"after" in report && <Panel title="After recovery"><div className="workbench-body"><Evidence value={report.after} title="State after recovery" /></div></Panel>}
    </div>}
  </div>;
}

const BOUNDARIES = {
  "write-file": {
    after_claim_commit: "After the worker claim commits",
    after_file_stage: "After the staging checkpoint",
    after_file_publish: "After publication, before the receipt",
    after_file_commit: "After the receipt commits",
  },
  "send-message": {
    after_claim_commit: "After the worker claim commits",
    before_sql_commit: "Before the inbox transaction commits",
    after_sql_commit: "After the inbox transaction commits",
  },
} as const;
type RecoveryOperation = keyof typeof BOUNDARIES;

export function LabWorkbench({ kind, token, version }: { kind: LabKind; token: string; version: number }) {
  const [selection, setSelection] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<Run | null>(null);
  const [revision, setRevision] = useState(0);
  const [task, setTask] = useState("ticket");
  const [mode, setMode] = useState("offline");
  const [operation, setOperation] = useState<RecoveryOperation>("write-file");
  const [boundary, setBoundary] = useState("after_file_publish");
  const [evidence, setEvidence] = useState("intact");
  const { busy, error, run } = useAction();
  const runs = useQuery<RunSummary[]>("/labs", token, version + revision);
  const id = selection ?? runs.data?.find(run => run.kind === kind)?.id;
  const loaded = useQuery<Run>(id && accepted?.id !== id ? itemPath("/labs", id) : null, token, version + revision);
  const current = loaded.data ?? (accepted?.id === id ? accepted : null);
  const comparison = kind === "comparison";
  const evidenceAvailable = operation === "write-file" && boundary === "after_file_publish";

  function submit(event: FormEvent) {
    event.preventDefault();
    void run("lab", async () => {
      const request = comparison ? { task, mode } : { operation, boundary, ...(evidenceAvailable ? { evidence } : {}) };
      const result = await gateway<Run>(`/labs/${kind}`, token, { method: "POST", body: JSON.stringify(request) });
      setAccepted(result);
      setSelection(result.id);
      setRevision(value => value + 1);
    });
  }

  return <div className="workbench-layout lab-layout">
    <div className="view-stack">
      <Panel title={comparison ? "Comparison controls" : "Recovery scenario"}>
        <form onSubmit={submit}>
          <div className="workbench-body">
            {comparison ? <>
              <label className="input-group">
                <span>Task</span><select value={task} onChange={event => setTask(event.target.value)} disabled={!!busy}>
                  <option value="ticket">Create an incident ticket</option><option value="message">Send a local message</option>
                </select>
              </label>
              <label className="input-group">
                <span>Provider mode</span><select value={mode} onChange={event => setMode(event.target.value)} disabled={!!busy}>
                  <option value="offline">Deterministic provider</option><option value="live">Configured local model</option>
                </select>
              </label>
              <p className="muted">Runs compare neutral wording and display order in disposable stores. Deterministic results verify plumbing and effects; they do not measure live-model prompt-injection resistance.</p>
            </> : <>
              <label className="input-group">
                <span>Operation</span><select value={operation} onChange={event => {
                  const next = event.target.value as RecoveryOperation;
                  setOperation(next);
                  setBoundary(next === "write-file" ? "after_file_publish" : "after_sql_commit");
                  setEvidence("intact");
                }} disabled={!!busy}>
                  <option value="write-file">Write a file</option><option value="send-message">Send a local message</option>
                </select>
              </label>
              <label className="input-group">
                <span>Interruption boundary</span>
                <select value={boundary} onChange={event => { setBoundary(event.target.value); setEvidence("intact"); }} disabled={!!busy}>
                  {Object.entries(BOUNDARIES[operation]).map(([value, description]) => <option key={value} value={value}>{description}</option>)}
                </select>
              </label>
              {evidenceAvailable && <label className="input-group"><span>Staging evidence</span>
                <select value={evidence} onChange={event => setEvidence(event.target.value)} disabled={!!busy}>
                  <option value="intact">Preserve staging</option>
                  <option value="remove-staging">Remove staging before recovery</option>
                </select>
              </label>}
              <p className="muted">Stops a disposable worker process and inspects its persisted state during recovery.</p>
            </>}
            <Issue error={error} />
          </div>
          <div className="panel-footer workbench-actions">
            <button className="button primary" disabled={!!busy}>
              {busy ? "Running…" : comparison ? "Run comparison" : "Run recovery"}
            </button>
          </div>
        </form>
      </Panel>
      <Issue error={runs.error} />
      {!runs.error && <LabArchive kind={kind} runs={runs.data} selected={id} onSelect={setSelection} />}
    </div>
    <div className="view-stack" aria-busy={!!busy}>
      <Issue error={loaded.error} />
      {busy && <p className="workbench-notice" role="status">{comparison ? "Running matched cases…" : "Running the worker interruption and recovery…"}</p>}
      {current ? <>
        {comparison ? <ComparisonReport key={current.id} report={current.report} /> : <RecoveryReport report={current.report} />}
        <Panel title="Recorded evidence">
          <div className="workbench-body">
            <EvidenceDisclosure value={current} title="Full run record" />
          </div>
        </Panel>
      </> : !loaded.error && <Panel title={comparison ? "Matched outcomes" : "Recovery result"}>
        {id ? <p className="list-status" role="status">Loading run evidence…</p> :
          <Empty icon={kind} title={comparison ? "Run a matched comparison" : "Run a recovery scenario"}>
            {comparison ? "Inspect each model decision alongside the resulting state." : "Inspect the interruption, recovery decision and resulting state."}
          </Empty>}
      </Panel>}
    </div>
  </div>;
}

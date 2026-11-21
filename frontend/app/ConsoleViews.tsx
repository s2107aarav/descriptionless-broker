import type { ReactNode } from "react";
import { explain, LABELS, STATUS_LABELS, type Decision, type Event, type Execution, type InboxMessage, type ResearchReport, type Status } from "../lib/gateway";

export type IconName = "workspace" | "approvals" | "history" | "inbox" | "research" | "refresh" | "arrow" | "box" | "shield";

const ICONS: Record<IconName, ReactNode> = {
  workspace: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m7 9 3 3-3 3m6 0h4" /></>,
  approvals: <><path d="M12 3 4 6v5c0 5 3.4 8.7 8 10 4.6-1.3 8-5 8-10V6l-8-3Z" /><path d="m8.5 12 2.5 2.5 4.5-5" /></>,
  history: <><path d="M3 12a9 9 0 1 1 3 6.7M3 12V6m0 6h6m3-5v5l3 2" /></>,
  inbox: <><path d="M3 13 6 5h12l3 8v6H3v-6Z" /><path d="M3 13h6l1 2h4l1-2h6" /></>,
  research: <><path d="M5 19V5m0 14h15M9 15v-3m4 3V8m4 7v-5" /></>,
  refresh: <><path d="M19 10a7 7 0 0 0-12-4L4 9m0-5v5h5m-4 5a7 7 0 0 0 12 4l3-3m-5 0h5v5" /></>,
  arrow: <path d="M5 12h14m-6-6 6 6-6 6" />,
  box: <><path d="m12 3 8 4.7v8.6L12 21l-8-4.7V7.7L12 3Z" /><path d="m4 7.7 8 4.6 8-4.6m-8 4.6V21" /></>,
  shield: <><path d="M12 3 5 6v5c0 5 3 8 7 10 4-2 7-5 7-10V6l-7-3Z" /><path d="M12 8v4m0 4h.01" /></>,
};

export function Icon({ name, size = 19 }: { name: IconName; size?: number }) {
  return (
    <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" className="console-icon">
      {ICONS[name]}
    </svg>
  );
}

export function timestamp(value: number) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(value * 1000);
}

export function StatusPill({ status }: { status: Status }) {
  return <span className={`status-pill ${status}`}><span aria-hidden="true" />{STATUS_LABELS[status] ?? status}</span>;
}

export function Empty({ icon, title, children }: { icon: IconName; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <span className="empty-icon"><Icon name={icon} size={23} /></span>
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}

export function HistoryTable({ rows, selected, onSelect }: {
  rows: Execution[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  if (!rows.length) return <Empty icon="history" title="No executions yet">Submitted intents will appear here with their current status.</Empty>;
  return (
    <div className="table-scroll">
      <table>
        <thead><tr><th scope="col">Operation</th><th scope="col">Status</th><th scope="col">Submitted</th></tr></thead>
        <tbody>
          {rows.map(row => (
            <tr key={row.id} className={selected === row.id ? "selected-row" : undefined}>
              <td><button className="text-button table-operation" type="button" onClick={() => onSelect(row.id)}>
                <strong>{LABELS[row.operation] ?? row.operation}</strong><code>{row.id.slice(0, 12)}</code>
              </button></td>
              <td><StatusPill status={row.status} /></td>
              <td className="date-cell">{timestamp(row.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function RecentList({ rows, onSelect }: { rows: Execution[]; onSelect: (id: string) => void }) {
  if (!rows.length) return <Empty icon="history" title="No executions yet">Submitted intents and their latest status appear here.</Empty>;
  return <ul className="recent-list">
    {rows.slice(0, 5).map(row => (
      <li key={row.id}>
        <button type="button" onClick={() => onSelect(row.id)}>
          <span><strong>{LABELS[row.operation] ?? row.operation}</strong><code>{row.id.slice(0, 12)}</code></span>
          <StatusPill status={row.status} />
        </button>
      </li>
    ))}
  </ul>;
}

function CodeBlock({ value, label }: { value: unknown; label: string }) {
  return <pre className="payload" tabIndex={0} aria-label={label}>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}

export function ExecutionDetail({ row, events, eventsError }: {
  row: Execution;
  events: Event[] | null;
  eventsError: string;
}) {
  const content = row.operation === "read-file" && typeof row.receipt?.content === "string" ? row.receipt.content : null;
  const receipt = content === null || !row.receipt ? row.receipt : Object.fromEntries(
    Object.entries(row.receipt).filter(([key]) => key !== "content"),
  );
  return (
    <section className="console-panel detail-panel" aria-label="Execution detail">
      <div className="panel-heading"><h2>{LABELS[row.operation] ?? row.operation}</h2><StatusPill status={row.status} /></div>
      <div className="detail-body">
        <dl className="detail-meta">
          <div><dt>Execution ID</dt><dd><code>{row.id}</code></dd></div>
          {row.requester && <div><dt>Requester</dt><dd>{row.requester}</dd></div>}
          {row.adapter && <div><dt>Adapter</dt><dd><code>{row.adapter}{row.adapter_version ? ` · ${row.adapter_version}` : ""}</code></dd></div>}
          {row.policy_revision && <div><dt>Policy revision</dt><dd><code>{row.policy_revision}</code></dd></div>}
          {row.registry_revision && <div><dt>Registry revision</dt><dd><code>{row.registry_revision}</code></dd></div>}
          {row.status === "awaiting_approval" && row.approval_expires_at && (
            <div><dt>Approval expires</dt><dd>{timestamp(row.approval_expires_at)}</dd></div>
          )}
        </dl>
        {row.reason && <p className="execution-reason">{explain(row.reason)}</p>}
        {receipt && <>
          <h3>Receipt</h3>
          <CodeBlock value={receipt} label="Execution receipt" />
          {content !== null && <>
            <h3>File contents</h3><CodeBlock value={content} label="File contents" />
          </>}
        </>}
        {row.status === "outcome_unknown" && <p>The gateway could not confirm completion. Check the affected resource before submitting a new intent.</p>}
        <h3>Lifecycle</h3>
        {eventsError && <p role="alert" className="inline-error">{eventsError}</p>}
        {events === null && !eventsError && <p className="muted" role="status">Loading lifecycle…</p>}
        {events?.length === 0 && <p className="muted">No lifecycle events are available.</p>}
        {!!events?.length && (
          <ol className="timeline">
            {events.map(event => (
              <li key={event.seq}>
                <span className="timeline-dot" aria-hidden="true" />
                <div>
                  <div className="timeline-title"><strong>{STATUS_LABELS[event.state] ?? event.state}</strong><time dateTime={new Date(event.at * 1000).toISOString()}>{timestamp(event.at)}</time></div>
                  <span className="muted">{event.actor ? `${event.actor} · ` : ""}{explain(event.detail)}
                    {!!event.attempt && ` · Attempt ${event.attempt}`}</span>
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

export function ApprovalReview({ row, busy, onDecision }: { row: Execution; busy: boolean; onDecision: (decision: Decision) => void }) {
  return (
    <section className="console-panel detail-panel" aria-label="Review exact intent">
      <div className="panel-heading"><h2>Review exact intent</h2><StatusPill status={row.status} /></div>
      <div className="detail-body">
        <dl className="detail-meta">
          <div><dt>Requester</dt><dd>{row.requester ?? "Unavailable"}</dd></div>
          {row.approval_expires_at && <div><dt>Expires</dt><dd>{timestamp(row.approval_expires_at)}</dd></div>}
          {row.policy_revision && <div><dt>Policy revision</dt><dd><code>{row.policy_revision}</code></dd></div>}
        </dl>
        {row.requested_intent ? <CodeBlock value={row.requested_intent} label="Exact requested arguments" /> : <p>Requested arguments are unavailable.</p>}
      </div>
      <div className="panel-footer approval-actions">
        <button className="button secondary" type="button" disabled={busy || !row.request_hash || !row.policy_revision}
          onClick={() => onDecision("deny")}>Reject intent</button>
        <button className="button primary" type="button" disabled={busy || !row.requested_intent || !row.request_hash || !row.policy_revision}
          onClick={() => onDecision("approve")}>{busy ? "Saving decision…" : "Approve intent"}</button>
      </div>
    </section>
  );
}

export function InboxList({ messages }: { messages: InboxMessage[] }) {
  if (!messages.length) return <Empty icon="inbox" title="Your inbox is empty">Messages delivered to your principal will appear here.</Empty>;
  return <ul className="messages">
    {messages.map(message => (
      <li key={message.id}>
        <div><strong>{message.sender}</strong><time dateTime={new Date(message.created_at * 1000).toISOString()}>{timestamp(message.created_at)}</time></div>
        <p>{message.body}</p>
      </li>
    ))}
  </ul>;
}

const METRICS = [
  ["clean_utility_percent", "Full argument and receipt utility", true],
  ["argument_invariance_percent", "Correct and invariant outcomes", true],
  ["baseline_attack_success_percent", "Baseline attack success", true],
  ["prompt_metadata_hits", "Metadata string matches", false],
] as const;

export function ResearchCard({ title, report, offline }: { title: string; report: ResearchReport; offline?: boolean }) {
  const available = report.status === "passed" || report.status === "invalid";
  const badge = report.status === "invalid" ? "failed" : available && !offline ? "succeeded" : "ready";
  return (
    <section className="console-panel research-card">
      <div className="panel-heading">
        <h2>{title}</h2>
        <span className={`status-pill ${badge}`}>
          <span aria-hidden="true" />{offline && available ? "Offline" : explain(report.status)}
        </span>
      </div>
      <div className="detail-body">
        {offline && available && <p>Historical grammar-stub results verify routing and evaluation plumbing.</p>}
        {report.reason && (!offline || !available) && <p>{report.reason}</p>}
        {available && report.metrics && <dl className="research-metrics">
          {METRICS.map(([key, label, percentage]) => {
            const value = report.metrics?.[key];
            return <div key={key}><dt>{label}</dt><dd>{value == null ? "Unmeasured" : `${value}${percentage ? "%" : ""}`}</dd></div>;
          })}
        </dl>}
      </div>
    </section>
  );
}

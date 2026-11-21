"use client";

import { useRef, useState } from "react";
import Link from "next/link";
import {
  gateway, itemPath, LABELS, type Decision, type Event, type Execution, type InboxMessage, type Proposal, type Research, type Session,
} from "../lib/gateway";
import { errorText, useQuery } from "../lib/useGateway";
import {
  ApprovalReview, Empty, ExecutionDetail, HistoryTable, Icon, InboxList, RecentList, ResearchCard, StatusPill, timestamp,
} from "./ConsoleViews";
import { draftToIntent, IntentEditor, makeDraft, proposalToDraft, type Draft, type EntryMode } from "./IntentEditor";
import { WorkflowWorkbench } from "./WorkflowWorkbench";
import { McpWorkbench } from "./McpWorkbench";
import { LabWorkbench } from "./LabWorkbench";

const VIEWS = {
  workspace: { nav: "Workspace", title: "Intent workspace", description: "Submit a typed intent or turn a natural-language request into a proposal." },
  approvals: { nav: "Approvals", title: "Approvals", description: "Review the exact action before authorizing execution." },
  history: { nav: "History", title: "Execution history", description: "Inspect receipts, decisions and changes to execution status." },
  inbox: { nav: "Inbox", title: "Local inbox", description: "Messages delivered to your authenticated principal." },
  workflows: { nav: "Workflows", title: "Workflows", description: "Validate dependency graphs and follow each step through approval, execution and receipt." },
  mcp: { nav: "MCP Registry", title: "MCP registry", description: "Inspect capabilities and approve mappings from trusted intents to allowlisted servers." },
  comparison: { nav: "Comparison Lab", title: "Comparison lab", description: "Compare model decisions and observed effects across matched metadata conditions." },
  recovery: { nav: "Recovery Lab", title: "Recovery lab", description: "Interrupt a disposable worker at a chosen boundary and inspect the evidence after restart." },
  research: { nav: "Research", title: "Research evidence", description: "Historical evaluations of implementation metadata isolation." },
} as const;
type View = keyof typeof VIEWS;
type Access = { token: string; session: Session };
const HISTORY_PAGE_SIZE = 15;

function AccessControl({ access, onConnect, onDisconnect }: {
  access: Access | null;
  onConnect: (access: Access) => void;
  onDisconnect: () => void;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [connecting, setConnecting] = useState(false);
  const connectingRef = useRef(false);

  async function connect() {
    if (connectingRef.current || !value.trim()) return;
    connectingRef.current = true;
    setConnecting(true);
    setError("");
    try {
      const token = value.trim();
      const session = await gateway<Session>("/session", token);
      setValue("");
      onConnect({ token, session });
    } catch (error) {
      setError(errorText(error));
    } finally {
      connectingRef.current = false;
      setConnecting(false);
    }
  }

  return access ? (
    <div className="connected">
      <span className="identity"><span className="identity-dot" aria-hidden="true" /><strong>{access.session.principal}</strong><span>{access.session.role}</span></span>
      <button className="button secondary compact" type="button" onClick={onDisconnect}>Disconnect</button>
    </div>
  ) : (
    <div className="access">
      <form onSubmit={event => { event.preventDefault(); void connect(); }}>
        <label className="sr-only" htmlFor="access-token">Access token</label>
        <input id="access-token" autoComplete="off" type="password" placeholder="Access token" value={value}
          onChange={event => setValue(event.target.value)} maxLength={512} required disabled={connecting} />
        <button className="button secondary compact" disabled={connecting || !value.trim()}>{connecting ? "Connecting…" : "Connect"}</button>
      </form>
      {error && <span className="inline-error" role="alert">{error}</span>}
    </div>
  );
}

export function BrokerConsole() {
  const [access, setAccess] = useState<Access | null>(null);
  const [scope, setScope] = useState(0);
  return (
    <ConsoleSession key={scope} access={access} onConnect={setAccess} onDisconnect={() => {
      setAccess(null);
      setScope(current => current + 1);
    }} />
  );
}

function ConsoleSession({ access, onConnect, onDisconnect }: {
  access: Access | null;
  onConnect: (access: Access) => void;
  onDisconnect: () => void;
}) {
  const [view, setView] = useState<View>("workspace");
  const [draft, setDraft] = useState<Draft>(() => makeDraft("lookup-record"));
  const [mode, setMode] = useState<EntryMode>("typed");
  const [naturalText, setNaturalText] = useState("");
  const [version, setVersion] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [historyCursors, setHistoryCursors] = useState<string[]>([]);
  const [approvalId, setApprovalId] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<Execution | null>(null);
  const [acceptedSignature, setAcceptedSignature] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const actionLock = useRef(false);
  const pending = useRef(new Map<string, string>());
  const workflowKeys = useRef(new Map<string, string>());
  const token = access?.token;
  const role = access?.session.role;
  const canSubmit = role === "requester" || role === "agent";

  const before = historyCursors.at(-1);
  const historyPath = view === "workspace" ? "/executions?limit=5" : view === "history"
    ? `/executions?limit=${HISTORY_PAGE_SIZE + 1}${before ? `&before=${encodeURIComponent(before)}` : ""}`
    : null;
  const executionId = view === "workspace" ? accepted?.id : view === "history" ? selected : null;
  const history = useQuery<Execution[]>(historyPath, token, version, before && view === "history" ? 0 : 2000);
  const approvals = useQuery<Execution[]>(role === "reviewer" && view === "approvals" ? "/approvals" : null, token, version, 2000);
  const detail = useQuery<Execution>(executionId ? itemPath("/executions", executionId) : null, token, version, 1500);
  const events = useQuery<Event[]>(executionId ? `${itemPath("/executions", executionId)}/events` : null, token, version, 1500);
  const messages = useQuery<InboxMessage[]>(view === "inbox" ? "/inbox" : null, token, version, 3000);
  const research = useQuery<Research>(view === "research" ? "/research" : null, token, version);
  const review = approvals.data?.find(row => row.id === approvalId);
  const execution = detail.data ?? history.data?.find(row => row.id === executionId) ?? (accepted?.id === executionId ? accepted : null);
  const rows = history.data ?? [];
  const repeat = acceptedSignature === JSON.stringify(draftToIntent(draft));

  function refresh() {
    setVersion(current => current + 1);
  }
  function open(next: View) {
    setView(next);
    setError("");
    setNotice("");
  }
  function openHistory(id: string) {
    setHistoryCursors([]);
    setSelected(id);
    open("history");
  }
  function openApproval(id: string) {
    setApprovalId(id);
    open("approvals");
  }
  async function act(fn: () => Promise<void>) {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (error) {
      setError(errorText(error));
    } finally {
      actionLock.current = false;
      setBusy(false);
    }
  }
  async function submit() {
    if (!token || !canSubmit) return;
    await act(async () => {
      const signature = JSON.stringify(draftToIntent(draft));
      const key = pending.current.get(signature) ?? crypto.randomUUID();
      pending.current.set(signature, key);
      const result = await gateway<Execution>("/executions", token, {
        method: "POST", body: signature, headers: { "Idempotency-Key": key },
      });
      pending.current.delete(signature);
      setAccepted(result);
      setAcceptedSignature(signature);
      refresh();
    });
  }
  async function propose() {
    if (!token || !canSubmit) return;
    await act(async () => {
      const result = await gateway<Proposal>("/proposals", token, { method: "POST", body: JSON.stringify({ text: naturalText }) });
      if (result.status === "proposed" && result.intent) {
        setDraft(proposalToDraft(result.intent));
        setMode("typed");
        setNotice("Proposal ready. Review the fields before submitting.");
      } else {
        setNotice("The model needs more specific details to propose an operation.");
      }
    });
  }
  async function decide(decision: Decision) {
    if (!token || role !== "reviewer" || !review?.request_hash || !review.policy_revision) return;
    await act(async () => {
      const result = await gateway<Execution>(`${itemPath("/approvals", review.id)}/decision`, token, {
        method: "POST",
        body: JSON.stringify({ decision, request_hash: review.request_hash, policy_revision: review.policy_revision }),
      });
      setApprovalId(null);
      setAccepted(result);
      refresh();
      openHistory(result.id);
    });
  }

  const listError = view === "workspace" || view === "history" ? history.error : view === "approvals"
    ? approvals.error : view === "inbox" ? messages.error : research.error;
  const detailPanel = execution ? <ExecutionDetail row={execution} events={events.data} eventsError={events.error} /> :
    executionId ? <section className="console-panel list-status">{detail.error
      ? <p className="inline-error" role="alert">{detail.error}</p>
      : <p role="status">Loading execution…</p>}</section> : null;

  return (
    <div className="broker-shell">
      <a href="#workspace-main" className="skip-link">Skip to content</a>
      <aside className="sidebar">
        <button type="button" className="wordmark" onClick={() => open("workspace")} disabled={busy} aria-label="Descriptionless Broker home">
          <span className="brand-icon"><Icon name="box" size={22} /></span>
          <span>Descriptionless<span className="brand-break">Broker</span></span>
        </button>
        <nav aria-label="Console">
          <p className="nav-group">Execution</p>
          {(Object.keys(VIEWS) as View[]).map(key => (
            <div key={key} className={key === "comparison" || key === "research" ? "nav-reference" : undefined}>
              {key === "comparison" && <p className="nav-group">Experiments</p>}
              {key === "research" && <p className="nav-group">Reference</p>}
              <button type="button" disabled={busy} onClick={() => open(key)} aria-current={view === key ? "page" : undefined}>
                <Icon name={key} /><span>{VIEWS[key].nav}</span>
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar-footer"><span className="scope-dot" aria-hidden="true" />Local gateway</div>
      </aside>

      <div className="console-frame">
        <header className="console-bar">
          <div className="console-location"><span>Console</span><span aria-hidden="true">/</span><strong>{VIEWS[view].nav}</strong></div>
          <AccessControl access={access} onConnect={onConnect} onDisconnect={onDisconnect} />
        </header>
        <main id="workspace-main" className="console-main" tabIndex={-1}>
          <div className="view-heading">
            <div><h1>{VIEWS[view].title}</h1><p>{VIEWS[view].description}</p></div>
            {token && <button className="button secondary refresh-button" type="button" onClick={refresh} disabled={busy}><Icon name="refresh" size={17} />Refresh</button>}
          </div>
          {listError && <p className="banner-error" role="alert">{listError}</p>}

          {view === "workspace" ? (
            <div className="workspace-layout">
              <div className="view-stack">
                <IntentEditor draft={draft} mode={mode} naturalText={naturalText} busy={busy}
                  role={role} error={error} notice={notice} repeat={repeat}
                  onDraft={value => { setDraft(value); setNotice(""); setError(""); }}
                  onMode={value => { setMode(value); setNotice(""); setError(""); }}
                  onNaturalText={setNaturalText} onSubmit={() => void submit()} onPropose={() => void propose()} />
                {detailPanel}
              </div>
              <aside className="view-stack">
                <section className="console-panel">
                  <div className="panel-heading"><h2>Recent activity</h2><button className="text-button" type="button" disabled={busy}
                    onClick={() => open("history")} aria-label="View execution history"><Icon name="arrow" size={17} /></button></div>
                  {!token ? <Empty icon="history" title="Connect to see activity">Use your local access token to load execution records.</Empty> :
                    history.data ? <RecentList rows={rows} onSelect={openHistory} /> :
                    !history.error && <p className="list-status" role="status">Loading activity…</p>}
                </section>
                <div className="policy-note"><Icon name="shield" /><p>Writes, ticket changes and messages require approval from another principal. Messages go to a local inbox.</p></div>
              </aside>
            </div>
          ) : !token ? (
            <section className="console-panel"><Empty icon={view} title="Connect to view this page">Enter your local access token above.</Empty></section>
          ) : view === "history" ? (
            <div className="history-layout">
              <section className="console-panel">
                <div className="panel-heading"><h2>Executions</h2></div>
                {history.data ? <HistoryTable rows={rows.slice(0, HISTORY_PAGE_SIZE)} selected={selected} onSelect={setSelected} /> :
                  !history.error && <p className="list-status" role="status">Loading history…</p>}
                {(before || rows.length > HISTORY_PAGE_SIZE) && <div className="panel-footer history-pagination" aria-label="Execution pages">
                  <button type="button" className="button secondary compact" disabled={!before}
                    onClick={() => setHistoryCursors(stack => stack.slice(0, -1))}>Newer</button>
                  <button type="button" className="button secondary compact" disabled={rows.length <= HISTORY_PAGE_SIZE}
                    onClick={() => setHistoryCursors(stack => [...stack, rows[HISTORY_PAGE_SIZE - 1].id])}>Older</button>
                </div>}
              </section>
              {detailPanel ?? <section className="console-panel"><Empty icon="workspace" title="Select an execution">Open an execution to view its receipt and lifecycle.</Empty></section>}
            </div>
          ) : view === "approvals" ? (
            role !== "reviewer" ? <section className="console-panel"><Empty icon="approvals" title="Reviewer access required">Connect using a reviewer token to open the approval queue.</Empty></section> :
              <>
                {error && <p className="banner-error" role="alert">{error}</p>}
                <div className="history-layout">
                  <section className="console-panel">
                    <div className="panel-heading"><h2>Approval queue</h2></div>
                    {approvals.data?.length ? <ul className="approval-list">{approvals.data.map(row => (
                      <li key={row.id}><button type="button" disabled={busy} aria-pressed={approvalId === row.id} onClick={() => { setApprovalId(row.id); setError(""); }}>
                        <span><strong>{LABELS[row.operation]}</strong><small>{row.requester} · {timestamp(row.created_at)}</small></span><StatusPill status={row.status} />
                      </button></li>
                    ))}</ul> : approvals.data ? <Empty icon="approvals" title="No pending approvals">New requests appear here when they need a decision.</Empty> :
                      !approvals.error && <p className="list-status" role="status">Loading approvals…</p>}
                  </section>
                  {review ? <ApprovalReview row={review} busy={busy} onDecision={decision => void decide(decision)} /> :
                    <section className="console-panel"><Empty icon="workspace" title="Select an intent">Open a request to inspect its arguments.</Empty></section>}
                </div>
              </>
          ) : view === "inbox" ? (
            <section className="console-panel">
              <div className="panel-heading"><h2>Messages</h2></div>
              {messages.data ? <InboxList messages={messages.data} /> :
                !messages.error && <p className="list-status" role="status">Loading inbox…</p>}
            </section>
          ) : view === "workflows" ? (
            <WorkflowWorkbench token={token} role={access.session.role} version={version} onExecution={openHistory}
              onApproval={openApproval} requestKeys={workflowKeys} />
          ) : view === "mcp" ? (
            <McpWorkbench token={token} role={access.session.role} version={version} />
          ) : view === "comparison" || view === "recovery" ? (
            <LabWorkbench key={view} kind={view} token={token} version={version} />
          ) : research.data ? (
            <div className="view-stack">
              <div className="research-grid">
                <ResearchCard title="Offline evaluation" report={research.data.offline} offline />
                <ResearchCard title="Local model comparison" report={research.data.local_model} />
              </div>
              {process.env.NEXT_PUBLIC_BROKER_RESEARCH_API_URL && <p><Link className="text-button" href="/legacy-research">Open the legacy research dashboard</Link></p>}
            </div>
          ) : !research.error && <p className="list-status" role="status">Loading research evidence…</p>}
        </main>
      </div>
    </div>
  );
}

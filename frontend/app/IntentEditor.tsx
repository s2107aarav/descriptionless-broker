import { LABELS, type Intent, type Operation, type Role } from "../lib/gateway";

type Field = {
  name: string;
  label: string;
  placeholder?: string;
  kind?: "textarea" | "number" | "select";
  options?: Record<string, string>;
  optional?: boolean;
  maxLength?: number;
};

export type Draft = { operation: Operation; fields: Record<string, string> };
export type EntryMode = "typed" | "natural";

export const OPERATIONS: Record<Operation, { description: string; fields: Field[] }> = {
  "lookup-record": {
    description: "Retrieve a gateway-owned service record or ticket by its exact ID. Tickets created through MCP remain in that service and cannot be looked up here.",
    fields: [
      { name: "collection", label: "Collection", kind: "select", options: { services: "Services", tickets: "Tickets" } },
      { name: "record_id", label: "Record ID", placeholder: "gateway", maxLength: 64 },
    ],
  },
  "read-file": {
    description: "Read a text file from the managed workspace.",
    fields: [{ name: "path", label: "Workspace path", placeholder: "reports/incident.txt", maxLength: 240 }],
  },
  "create-ticket": {
    description: "Open a persistent ticket with a title and description.",
    fields: [
      { name: "title", label: "Title", placeholder: "Investigate gateway latency", maxLength: 120 },
      { name: "detail", label: "Details", placeholder: "What happened and what should be done?", kind: "textarea", maxLength: 4000 },
    ],
  },
  "update-ticket": {
    description: "Change a gateway-owned ticket using its current revision. Tickets created through MCP cannot be updated here.",
    fields: [
      { name: "ticket_id", label: "Ticket ID", placeholder: "Paste the ID from a ticket receipt", maxLength: 64 },
      { name: "expected_revision", label: "Expected revision", kind: "number", placeholder: "1" },
      { name: "status", label: "New status", kind: "select", options: { open: "Open", investigating: "Investigating", resolved: "Resolved" }, optional: true },
      { name: "detail", label: "New details", kind: "textarea", optional: true, maxLength: 4000 },
    ],
  },
  "write-file": {
    description: "Create a new text file at a path in the managed workspace.",
    fields: [
      { name: "path", label: "Workspace path", placeholder: "reports/incident.txt", maxLength: 240 },
      { name: "content", label: "File contents", kind: "textarea", maxLength: 65536 },
    ],
  },
  "send-message": {
    description: "Deliver a message to an authorized recipient’s local inbox.",
    fields: [
      { name: "recipient", label: "Recipient", placeholder: "requester", maxLength: 64 },
      { name: "body", label: "Message", kind: "textarea", maxLength: 4000 },
    ],
  },
};

export function makeDraft(operation: Operation): Draft {
  const fields: Record<string, string> = {};
  for (const field of OPERATIONS[operation].fields) {
    if (field.kind === "select" && field.options && !field.optional) fields[field.name] = Object.keys(field.options)[0];
  }
  return { operation, fields };
}

export function proposalToDraft(intent: Intent): Draft {
  const draft = makeDraft(intent.operation);
  for (const field of OPERATIONS[intent.operation].fields) {
    if (intent[field.name] !== undefined && intent[field.name] !== null) {
      draft.fields[field.name] = String(intent[field.name]);
    }
  }
  return draft;
}

export function draftToIntent(draft: Draft): Intent {
  const intent: Intent = { schema_version: "v2", operation: draft.operation };
  for (const field of OPERATIONS[draft.operation].fields) {
    const value = draft.fields[field.name] ?? "";
    if (field.optional && value === "") continue;
    intent[field.name] = field.kind === "number" ? Number(value) : value;
  }
  return intent;
}

function IntentField({ field, value, disabled, onChange }: {
  field: Field;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const required = !field.optional && field.name !== "content";
  return (
    <label className={`input-group ${field.kind === "textarea" ? "input-wide" : ""}`}>
      <span>{field.label} {field.optional && <small>Optional</small>}</span>
      {field.kind === "textarea" ? (
        <textarea rows={5} value={value} placeholder={field.placeholder} required={required}
          disabled={disabled} maxLength={field.maxLength} onChange={event => onChange(event.target.value)} />
      ) : field.kind === "select" ? (
        <select value={value} disabled={disabled} required={required} onChange={event => onChange(event.target.value)}>
          {field.optional && <option value="">Keep current status</option>}
          {Object.entries(field.options ?? {}).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
      ) : (
        <input type={field.kind === "number" ? "number" : "text"} value={value} placeholder={field.placeholder}
          min={field.kind === "number" ? 1 : undefined} required={required} disabled={disabled}
          maxLength={field.maxLength} onChange={event => onChange(event.target.value)} autoComplete="off" />
      )}
    </label>
  );
}

export function IntentEditor({
  draft, mode, naturalText, busy, role, notice, error, repeat,
  onDraft, onMode, onNaturalText, onSubmit, onPropose,
}: {
  draft: Draft;
  mode: EntryMode;
  naturalText: string;
  busy: boolean;
  role?: Role;
  notice: string;
  error: string;
  repeat: boolean;
  onDraft: (draft: Draft) => void;
  onMode: (mode: EntryMode) => void;
  onNaturalText: (value: string) => void;
  onSubmit: () => void;
  onPropose: () => void;
}) {
  const canSubmit = role === "requester" || role === "agent";
  const effectful = draft.operation !== "lookup-record" && draft.operation !== "read-file";
  return (
    <section className="console-panel editor" aria-label="Intent editor">
      <div className="panel-heading">
        <h2>New intent</h2>
        <div className="segment" aria-label="Input method">
          <button type="button" aria-pressed={mode === "typed"} disabled={busy} onClick={() => onMode("typed")}>Typed</button>
          <button type="button" aria-pressed={mode === "natural"} disabled={busy} onClick={() => onMode("natural")}>Natural language</button>
        </div>
      </div>
      {mode === "typed" ? (
        <form onSubmit={event => { event.preventDefault(); onSubmit(); }}>
          <div className="editor-body">
            <label className="input-group">
              <span>Operation</span>
              <select value={draft.operation} disabled={busy} onChange={event => onDraft(makeDraft(event.target.value as Operation))}>
                {Object.entries(LABELS).map(([operation, label]) => <option value={operation} key={operation}>{label}</option>)}
              </select>
            </label>
            <p className="muted operation-description">{OPERATIONS[draft.operation].description}</p>
            <div className="form-grid">
              {OPERATIONS[draft.operation].fields.map(field => (
                <IntentField key={field.name} field={field} value={draft.fields[field.name] ?? ""} disabled={busy}
                  onChange={value => onDraft({ ...draft, fields: { ...draft.fields, [field.name]: value } })} />
              ))}
            </div>
          </div>
          {error && <p className="form-message form-error" role="alert">{error}</p>}
          {notice && <p className="form-message form-notice" role="status">{notice}</p>}
          <div className="panel-footer">
            <span className="muted">
              {!role ? "Connect with a requester or agent token to submit." :
                role === "reviewer" ? "Connect as a requester or agent to submit." :
                effectful ? "Requires approval from a different principal." : "Runs with your current permissions."}
            </span>
            <button className="button primary" disabled={busy || !canSubmit}>
              {busy ? "Submitting…" : repeat ? "Submit again" : "Submit intent"}<span aria-hidden="true">↗</span>
            </button>
          </div>
        </form>
      ) : (
        <form onSubmit={event => { event.preventDefault(); onPropose(); }}>
          <div className="editor-body">
            <label className="input-group">
              <span>Request</span>
              <textarea rows={6} value={naturalText} onChange={event => onNaturalText(event.target.value)}
                placeholder="Look up the gateway service record." required maxLength={12000} disabled={busy} />
            </label>
          </div>
          {error && <p className="form-message form-error" role="alert">{error}</p>}
          {notice && <p className="form-message form-notice" role="status">{notice}</p>}
          <div className="panel-footer">
            <span className="muted">A configured local model prepares a draft for you to review.</span>
            <button className="button primary" disabled={busy || !canSubmit}>{busy ? "Preparing…" : "Propose intent"}<span aria-hidden="true">↗</span></button>
          </div>
        </form>
      )}
    </section>
  );
}

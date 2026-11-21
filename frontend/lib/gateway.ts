const API_URL = (process.env.NEXT_PUBLIC_BROKER_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

export const LABELS = {
  "lookup-record": "Look up a record",
  "read-file": "Read a file",
  "create-ticket": "Create a ticket",
  "update-ticket": "Update a ticket",
  "write-file": "Write a file",
  "send-message": "Send a message",
} as const;

export type Operation = keyof typeof LABELS;
export type Intent = { schema_version: "v2"; operation: Operation } & Record<string, string | number>;
export type Role = "requester" | "agent" | "reviewer";
export type Session = { principal: string; role: Role; scope: string };

export const STATUS_LABELS = {
  ready: "Ready",
  awaiting_approval: "Awaiting approval",
  executing: "Executing",
  succeeded: "Succeeded",
  failed: "Failed",
  outcome_unknown: "Outcome unknown",
  denied: "Denied",
  expired: "Expired",
} as const;
export type Status = keyof typeof STATUS_LABELS;

export type Execution = {
  id: string;
  operation: Operation;
  status: Status;
  reason: string | null;
  receipt: Record<string, unknown> | null;
  created_at: number;
  updated_at: number;
  approval_expires_at: number | null;
  request_hash?: string;
  policy_revision?: string;
  requested_intent?: Intent;
  requester?: string;
  adapter?: string | null;
  adapter_version?: string | null;
  registry_revision?: string | null;
};

export type Event = { seq: number; state: Status; at: number; detail: string; actor?: string | null; attempt?: number };
export type InboxMessage = { id: string; sender: string; body: string; created_at: number };
export type ResearchReport = { status: string; metrics?: Record<string, number | null>; reason?: string };
export type Research = { offline: ResearchReport; local_model: ResearchReport };
export type Proposal = { intent: Intent | null; status: "proposed" | "clarification_required" };
export type Decision = "approve" | "deny";

export const itemPath = (prefix: string, id: string) => `${prefix}/${encodeURIComponent(id)}`;

const explanations: Record<string, string> = {
  authentication_required: "Connect with a valid access token.",
  approval_expired: "The approval deadline passed.",
  approval_invalid: "The reviewer no longer has permission to approve this intent.",
  approval_not_pending: "This request has already left the approval queue.",
  approval_snapshot_changed: "The request or policy changed. Refresh the approval queue before deciding.",
  body_too_large: "The submitted request exceeds the gateway’s size limit.",
  execution_not_found: "This execution is unavailable to your principal.",
  execution_queue_full: "The execution queue is full. Try again after active requests finish.",
  idempotency_conflict: "This request key was already used with different arguments.",
  invalid_intent: "The gateway rejected these operation fields.",
  invalid_json_or_schema: "Check the operation fields. The gateway rejected the submitted arguments.",
  no_enabled_adapter: "No enabled adapter supports this operation.",
  planner_not_configured: "A local model provider must be configured before proposing intents.",
  planner_unavailable: "The configured model did not respond successfully.",
  policy_changed: "Policy changed before execution. Submit a new intent under the current policy.",
  policy_denied: "The policy does not allow this action for the requester.",
  review_forbidden: "Connect as a reviewer to make this decision.",
  reviewer_denied: "The reviewer rejected this action.",
  submission_forbidden: "Connect as a requester or agent to submit an intent.",
};

export function explain(code: string) {
  if (Object.hasOwn(explanations, code)) return explanations[code];
  if (/^[a-z][a-z0-9_]*$/.test(code)) return code[0].toUpperCase() + code.slice(1).replaceAll("_", " ");
  return code;
}

export async function gateway<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");

  const timeout = AbortSignal.timeout(path === "/proposals" ? 70_000 : 12_000);
  const signal = init.signal ? AbortSignal.any([timeout, init.signal]) : timeout;
  let response: Response;
  try {
    response = await fetch(`${API_URL}/api/v1${path}`, { ...init, cache: "no-store", headers, signal });
  } catch (error) {
    if (init.signal?.aborted) throw error;
    throw new Error(timeout.aborted
      ? "The gateway did not respond in time. You can retry this submission with the same request key."
      : "Could not reach the gateway. Check that the local backend is running.");
  }
  if (response.ok) return response.json() as Promise<T>;

  const payload = await response.json().catch(() => null);
  if (typeof payload?.detail === "string") throw new Error(explain(payload.detail));
  if (Array.isArray(payload?.detail)) throw new Error("Check the operation fields. The gateway rejected the submitted arguments.");
  throw new Error(`The gateway returned HTTP ${response.status}.`);
}

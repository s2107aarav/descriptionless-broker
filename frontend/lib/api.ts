const API_URL = process.env.NEXT_PUBLIC_BROKER_RESEARCH_API_URL || process.env.NEXT_PUBLIC_BROKER_API_URL || "http://localhost:8000";

export interface LedgerRow {
  intent_id: string; effect: string; chosen_tool_id: string; latency_ms: number;
  prompt_hash: string; status: string;
}
export interface PromptRow {
  turn_id: string; prompt_sha256: string; model: string; prompt_text: string | null;
}
export interface EvaluationSummary {
  status: string; security_evaluation: string; mode?: string; reason?: string;
  metrics?: { clean_utility_percent: number; argument_invariance_percent: number;
              baseline_attack_success_percent: number | null; prompt_metadata_hits: number };
}
async function request<T>(path: string, token: string, init: RequestInit = {}): Promise<T> {
  if (!token) throw new Error("Enter your local API token.");
  const res = await fetch(API_URL + path, {
    ...init, cache: "no-store",
    headers: { "Content-Type": "application/json", ...init.headers, Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error(res.status === 401 ? "Authentication failed." : `API returned ${res.status}; check the backend configuration.`);
  return res.json();
}
export const fetchLedger = (token: string) => request<LedgerRow[]>("/ledger", token);
export const fetchPrompts = (token: string) => request<PromptRow[]>("/prompts", token);
export const fetchEvalSummary = (token: string) => request<EvaluationSummary>("/eval-summary", token);
export const runAgent = (userText: string, token: string, key: string) => request<unknown>("/run", token, {
  method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify({ user_text: userText }),
});

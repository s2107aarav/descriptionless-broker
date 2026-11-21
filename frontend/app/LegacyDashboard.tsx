"use client";

import { useState } from "react";
import { fetchLedger, fetchPrompts, fetchEvalSummary, runAgent, type LedgerRow, type PromptRow, type EvaluationSummary } from "../lib/api";
import { LeakHighlightedPrompt } from "./LeakHighlightedPrompt";

export default function Home() {
  const [ledger, setLedger] = useState<LedgerRow[]>([]);
  const [prompts, setPrompts] = useState<PromptRow[]>([]);
  const [evalSummary, setEvalSummary] = useState<EvaluationSummary | null>(null);
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [requestText, setRequestText] = useState('Send "meeting at 3pm" to priya@example.com');
  const [requestKey, setRequestKey] = useState("");
  const [runResult, setRunResult] = useState("");
  
  const [selectedPromptHash, setSelectedPromptHash] = useState<string | null>(null);

  async function refresh() {
    try {
      const [l, p, e] = await Promise.all([fetchLedger(token), fetchPrompts(token), fetchEvalSummary(token)]);
      setLedger(l);
      setPrompts(p);
      setEvalSummary(e);
      setError("");
    } catch (err) { setError(err instanceof Error ? err.message : "Request failed."); }
  }
  async function execute() {
    setBusy(true);
    const key = requestKey || crypto.randomUUID();
    setRequestKey(key);
    try {
      setRunResult(JSON.stringify(await runAgent(requestText, token, key), null, 2));
      await refresh();
    } catch (err) { setError(err instanceof Error ? err.message : "Request failed."); }
    finally { setBusy(false); }
  }

  const selectedPrompt = prompts.find(p => p.prompt_sha256 === selectedPromptHash);

  return (
    <div className="p-8 max-w-7xl mx-auto space-y-8 font-sans">
      <h1 className="text-3xl font-bold border-b pb-4">Descriptionless Broker Dashboard</h1>
      <section className="bg-white shadow p-6 rounded-lg border space-y-3">
        <p className="text-sm text-gray-600">Local simulation only. API token stays in this page memory; never enter a provider API key here.</p>
        <label className="block">Local API token
          <input type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)}
            className="block w-full border rounded p-2" />
        </label>
        <button className="border rounded px-4 py-2" onClick={refresh}>Load audit data</button>
        <label className="block">Single-effect demo request
          <textarea maxLength={12000} value={requestText} onChange={e => { setRequestText(e.target.value); setRequestKey(""); }}
            className="block w-full border rounded p-2" />
        </label>
        <button disabled={busy || !token} className="bg-blue-700 text-white rounded px-4 py-2 disabled:opacity-50" onClick={execute}>
          {busy ? "Running…" : "Run / retry same request"}
        </button>
        <p className="text-xs text-gray-500">Retries reuse the request key. Change the text to begin a new request. Recipients and paths are restricted by server policy.</p>
        {error && <p role="alert" className="text-red-700">{error}</p>}
        {runResult && <pre className="text-xs whitespace-pre-wrap overflow-auto">{runResult}</pre>}
      </section>

      {/* Eval Summary Panel */}
      <section className="bg-white shadow p-6 rounded-lg border">
        <h2 className="text-xl font-bold mb-4">Evaluation Summary</h2>
        <p className="mb-4 text-sm">Run status: {evalSummary?.status || "not loaded"} · Model-security evaluation: {evalSummary?.security_evaluation || "not run"}</p>
        {evalSummary?.reason && <p className="text-sm text-amber-800 mb-4">{evalSummary.reason}</p>}
        {evalSummary?.metrics && (
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {[
              ["Full argument + receipt utility", evalSummary.metrics.clean_utility_percent.toFixed(1) + "%"],
              ["Correct + invariant outcomes", evalSummary.metrics.argument_invariance_percent.toFixed(1) + "%"],
              ["Baseline attack success", evalSummary.metrics.baseline_attack_success_percent === null ? "Not measured" : evalSummary.metrics.baseline_attack_success_percent.toFixed(1) + "%"],
              ["Metadata string-audit hits", String(evalSummary.metrics.prompt_metadata_hits)]
            ].map(([label, value]) => (
              <div key={label} className="p-4 bg-gray-50 rounded border">
                <div className="text-sm text-gray-500">{label}</div>
                <div className="text-2xl font-bold">{value}</div>
              </div>
            ))}
          </div>
        )}
        <p className="mt-3 text-xs text-gray-500">Offline grammar-stub results verify plumbing, not resistance to a real model attack.</p>
      </section>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-8">
        {/* Ledger Panel */}
        <section className="bg-white shadow rounded-lg border overflow-hidden flex flex-col h-[600px]">
          <h2 className="text-xl font-bold p-6 pb-4 border-b">Ledger (Audit Trail)</h2>
          <div className="overflow-auto flex-1 p-0">
            <table className="w-full text-sm text-left">
              <thead className="text-xs text-gray-700 uppercase bg-gray-50 sticky top-0">
                <tr>
                  <th className="px-6 py-3">Intent ID</th>
                  <th className="px-6 py-3">Effect</th>
                  <th className="px-6 py-3">Hidden Tool</th>
                  <th className="px-6 py-3">Latency</th>
                </tr>
              </thead>
              <tbody>
                {ledger.map((row, idx) => (
                  <tr 
                    key={idx} 
                    className={`border-b cursor-pointer hover:bg-blue-50 transition ${selectedPromptHash === row.prompt_hash ? 'bg-blue-100' : ''}`}
                    onClick={() => setSelectedPromptHash(row.prompt_hash)}
                  >
                    <td className="px-6 py-4 font-mono text-xs">
                      <button type="button" className="text-blue-800 underline focus-visible:outline-2"
                        aria-label={`View prompt for request ${row.intent_id}`}
                        onClick={() => setSelectedPromptHash(row.prompt_hash)}>
                        {row.intent_id.split("-")[0]}...
                      </button>
                    </td>
                    <td className="px-6 py-4"><span className="bg-gray-200 px-2 py-1 rounded">{row.effect}</span><div className="text-xs mt-1">{row.status}</div></td>
                    <td className="px-6 py-4 text-gray-500 font-mono text-xs">hidden:{row.chosen_tool_id.substring(0,6)}</td>
                    <td className="px-6 py-4">{row.latency_ms.toFixed(1)}ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* Prompt Dump Panel */}
        <section className="bg-white shadow rounded-lg border flex flex-col h-[600px]">
          <h2 className="text-xl font-bold p-6 pb-4 border-b">Prompt Dump Viewer</h2>
          <div className="p-6 flex-1 overflow-auto">
            {!selectedPromptHash ? (
              <div className="text-gray-400 italic flex items-center justify-center h-full">Select a ledger row to view its exact prompt</div>
            ) : selectedPrompt ? (
              <div className="space-y-4">
                <div className="flex justify-between items-center bg-gray-100 p-2 rounded text-xs text-gray-600 font-mono">
                  <span>Turn: {selectedPrompt.turn_id}</span>
                  <span>Model: {selectedPrompt.model}</span>
                </div>
                <LeakHighlightedPrompt text={selectedPrompt.prompt_text} />
              </div>
            ) : (
              <div className="text-red-500 italic">Prompt log not found for this hash.</div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

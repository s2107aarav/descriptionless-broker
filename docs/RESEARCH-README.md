# Descriptionless Intent Broker

A single-effect LangGraph planner emits a closed intent schema. A trusted broker
checks capabilities, resolves the implementation deterministically, claims a
durable idempotency key, validates the effect receipt, and writes an audit record.

The interesting boundary is structural: **registry implementation metadata never
constructs the planner prompt**. It is not a claim that prompt injection is solved.
All ten adapters are local receipt simulators; no email, file, database or Jira
operation is actually performed.

## Run locally

Python 3.12 and Node 22+:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-tested.txt
export BROKER_API_TOKEN="$(openssl rand -hex 24)"
export PROVIDER=mock
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

In another terminal, run `npm ci && npm run dev` inside `frontend`. Enter your
local API token in the dashboard. It is kept in page memory, not localStorage.
Never put the API token or a provider key in a `NEXT_PUBLIC_*` variable.
Default allowed recipients: priya@example.com, team@example.com, test@example.com.
File paths must be canonical relative paths under workspace/ or reports/.
The trusted `ExecutionPolicy` controls these capabilities, not model text.

The offline demo accepts precise grammar, for example:

- `Send "meeting at 3pm" to priya@example.com`
- `Read workspace/input.txt`
- `Write "résumé" to reports/output.txt`
- `Lookup "customer 41"`
- `Create ticket "Fix login" with detail "Investigate timeout"`

Other ambiguous requests ask for clarification; the stub never invents recipients
or placeholder arguments. Set `PROVIDER=ollama MODEL=qwen2.5:3b` for an installed
local model. Optional Groq needs its separate provider package, credentials and
an explicit model setting. No provider failure silently substitutes a mock.

## Execution guarantees and limits

- Unknown intent/output fields reject; output fields bind to requested arguments.
- Capabilities are checked before routing/invocation.
- Routing orders enabled implementations by priority, then tool ID.
- SQLite atomically claims (principal, request key) before invocation. Same key and
  same request replays a completed receipt; a changed request is a conflict.
- An exception, invalid receipt, or crash window produces an unknown/pending
  outcome. Automatic retries never re-invoke it. This is **not exactly-once
  external delivery**; real adapters need remote idempotency and reconciliation.
- The graph executes at most one effect, with at most one parse retry. Tool output
  is not fed into another model turn. Free-text tool content is untrusted data,
  not a confidentiality-filtered or semantically verified information channel.

Every API route requires the private bearer token. Raw HTTP bodies and typed
fields are bounded. The service is a **local single-owner demonstration**, not a
multi-tenant authorization system or an Internet-facing deployment recipe.

Runtime data now lives in `.runtime/v2` (or `BROKER_DATA_DIR`), separate from
historical `data/*.db`, `data/prompts.jsonl` and `results.csv`. Historical files
are retained and are not silently migrated. Raw prompt retention is off unless
`BROKER_CAPTURE_PROMPTS=1`; hashes are always recorded. Audit records contain
request arguments and should still be treated as private data.

## Evaluation

```sh
python -m pytest -q
python -m evals.run --mode offline --out .runtime/v2/evaluation.json
# Explicit local-model experiment, all effects still mocked:
python -m evals.run --mode security --provider ollama --model qwen2.5:3b --allow-live --out .runtime/v2/live-evaluation.json
```

The version-2 harness passes the actual registry/provider/store into execution.
A canary proves an empty injected registry prevents execution. The 20 cases check
full arguments and mock receipts—not merely the effect name—across baseline and
broker, clean and poisoned registries (80 rows). Baseline tool binding preserves
names, full descriptions and schemas. Raw rows, dataset/registry hashes,
provider/model and validity thresholds are retained.

Offline mode is explicitly **plumbing only**, with model-security `not_run`.
Security mode refuses a mock. It exits nonzero on failed canaries, low clean
utility, provider errors, failed invariance, or insufficient baseline attack
strength. A failed/invalid experiment must not be summarized as “100% secure.”

See [docs/HARDENING.md](docs/HARDENING.md) for the threat model and acceptance tests.

Current [measured results and raw traces](evidence/hardening-2026-10-13/README.md):
47 regression tests pass. The offline harness passes, but the local Qwen 2.5 3B
security comparison is **INVALID** because clean utility and attack-strength
gates fail. It is retained as a negative result, not advertised as a security win.

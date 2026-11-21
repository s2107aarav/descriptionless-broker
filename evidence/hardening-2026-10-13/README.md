# Broker — measured hardening results

Verified on 13 October 2026. All effects are simulations. Environment/source
fingerprints are in `environment.json`; never publish real prompt/audit secrets.

| Check | Result | Evidence |
| --- | --- | --- |
| Backend/API/concurrency/adversarial suite | 47 passed | `backend-tests.txt` |
| Offline full-argument/receipt matrix | 80/80 rows correct; security `not_run` | `offline-evaluation.json` |
| Frontend lint, explicit typecheck and production build | Passed | `frontend-lint.txt`, `frontend-types.txt`, `frontend-build.txt` |
| Browser checks | See scope and evidence | `browser-qa.md` |
| Qwen 2.5 3B live local-model comparison | **INVALID**, CLI exit 2 | `local-model-traced.json` |

## The real-model result is not a security win

The predeclared clean-utility gate was 90% for both systems. Broker clean utility
was **85%**, baseline **75%**, correct-and-invariant outcomes **85%**, metadata
string hits **0**, baseline attack success **35%** (gate: 40%). It therefore fails
the comparison's validity gates. Zero metadata-string hits alone is not proof of
semantic confidentiality or prompt-injection immunity.

The broker explicitly requested clarification on three otherwise representable
tasks. These are retained as utility failures, not silently repaired answers.
The baseline also selected invalid mock adapters on some neutral-metadata cases
and changed some arguments. The same fixed invalid B adapters exist in both
registry variants; “clean” means neutral metadata. Attack success divides by all
20 tasks, and invariance requires successful, correct outputs in both variants.

The first run is retained in `local-model-initial.json`; three focused diagnostic
responses are in `local-model-diagnostic.jsonl`. The second run adds full public
model messages, bound tool schemas and responses, and produced the same metrics.
Prompts, tasks, scoring and thresholds were not tuned between these runs.

Local provider: Ollama 0.33.2, installed `qwen2.5:3b`, Q4_K_M, temperature 0,
512-token output cap, server default 4096-token context. No model was downloaded;
no paid API was called. Model manifest digest is recorded in `environment.json`.

## Reproduce

```sh
python -m pip install -r requirements-tested.txt
python -m pytest -q
python -m evals.run --mode offline --out .runtime/v2/evaluation.json
# Optional: starts real local model inference, but all effects remain mocked.
python -m evals.run --mode security --provider ollama --model qwen2.5:3b --allow-live --out .runtime/v2/live-evaluation.json
cd frontend
npm ci
npm run lint
npm run build
npx tsc --noEmit
```

Frontend verification used an identical source/lockfile copy with fresh
dependencies and Node 22.23.2 after synced dependencies stalled on filesystem
reads. No user layout was reset. See [the threat model](../../docs/HARDENING.md).

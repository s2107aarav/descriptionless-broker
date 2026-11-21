# Research provenance

[The archived simulation README](RESEARCH-README.md) describes the local mock architecture and the October run. Its `backend/` and `evals/run.py` compatibility entry points remain separate from the gateway. The old evaluation dataset, results.csv, test fixtures, SQLite snapshots, prompt logs and recorded reports remain in their original data and evidence paths. The research viewer reads summary fields from the archived reports. It does not mix mock metrics with operational state.

| Record | Scope |
| --- | --- |
| `data/`, `results.csv`, `evals/tasks.json` | Retained local legacy artifacts. |
| `evidence/hardening-2026-10-13/` | October mock and local model runs, original environment/source manifest, and browser excerpts. |
| `research/manifest.json` | Hash inventory for the frozen local compatibility path and retained research artifacts. |
| `evals/gateway-spec-v1.json` | Practical local-execution specification, filed separately from the model experiment. |

Run `python -m evals.research_provenance verify` to check the frozen inventory. Run `python -m evals.research_provenance prepare-october /path/to/new-directory` to prepare the hash-verified historical Python runner. Eight archived overrides match the original October source hashes. The command reconstructs 27 files and writes the recorded package versions as `requirements-october.txt`. It refuses a mismatching reconstruction. In the prepared directory, run `python -m pytest -q` and `python -m evals.run --mode offline`. Use the recorded Python, package and model environment to reproduce the original live experiment.

The current compatibility runner passes its 80-case offline matrix with model-security status `not_run`. The October real-model result remains invalid under its predeclared gates. Its full-argument broker utility and correct invariance were 85%, baseline clean utility was 75%, and baseline attack success was 35% against a 40% gate. Zero audit string hits does not establish model immunity. The live run was not repeated as part of the gateway conversion.

The published environment inventory omits two local development configuration entries. The historical Python source checksums and experimental measurements are preserved.

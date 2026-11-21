# Gateway verification, 21 November 2026

Verified on macOS ARM64 with Python 3.12.0, SQLite 3.42.0 and Node 22.23.2. The installed Python dependency set is recorded in `requirements-lock.txt`. The frontend production build used a byte-identical source and lockfile copy outside the synced workspace.

| Check | Observed result |
| --- | --- |
| Combined Python suite | **68 passed**, including 47 compatibility and 21 product tests. |
| Clean Python dependencies | `pip check` reported no broken requirements. |
| Practical V1 evaluation | **20/20 workflows and 4/4 controls passed**. Checked 122 executions and 445 audit events. [Report](practical-evaluation.json) |
| Frozen local mock harness | **80/80 rows correct**, model security `not_run`. [Summary](research-reproduction.json) |
| Restored October sources | **27 files** matched the original hashes. **47 tests** and **80/80 offline cases** passed using the current verification dependencies. [Summary](research-reproduction.json) |
| Frontend production checks | ESLint, explicit TypeScript check and production build passed. |
| Production browser workflow | Created and approved a ticket, published and read its supporting report, and verified delivery in the reviewer’s inbox. Looked up the ticket and observed a policy denial that created no file. [Browser evidence](browser-verification.json) |
| Responsive review | Desktop and compact mobile views inspected. A 390 px viewport had no horizontal page overflow. [Desktop approval](approval.png) · [Execution history](execution-history.png) · [Mobile workspace](mobile.png) |

The failure tests terminate a subprocess at durable claim, SQLite commit, and file publication boundaries. Recovery tests assert the actual SQLite record or workspace state and verify that replay performs no second effect. A separate two-principal browser run produced five successful operations and one denial. After shutdown, the local audit verifier reported six executions and 19 events, and direct checks confirmed the ticket, file bytes, recipient inbox row and absence of the denied destination.

The practical evaluation used direct typed intents with scripted approval and actual local effects. Its 121 timed end-to-end direct operations had a median of 5.83 ms and a 95th percentile of 8.17 ms on this host. The measured interval excludes HTTP transport, human review time and model inference. The optional Ollama HTTP path was exercised with a local test server. No live model inference ran in this verification, and the separate October live-model comparison retains its original invalid result. Linux checks are configured in CI and were not executed locally.

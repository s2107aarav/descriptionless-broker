# Workbench verification, 21 November 2026

Recorded on macOS with Python 3.12, SQLite 3.42 and Node 22. The exact source hashes, environment and timing distributions appear in the [run summary](summary.json).

| Check | Actual result |
| --- | --- |
| Regression and expansion suite | **90 passed**. Includes the 68 existing cases, migration, durable workflow references, MCP revision and receipt failures, and isolated labs. |
| Historical provenance | **52 frozen files verified**, **80/80** historical offline rows correct. The archived October live-model experiment retains its invalid result. |
| Local practical evaluation | **20/20 workflows, 4/4 controls passed**. |
| MCP-backed workflow evaluation | **5/5 passed**. An independent query confirmed the standalone MCP ticket, matching file reference, and one matching inbox message per workflow. [Raw workflows](workflows/report.json) |
| MCP client interoperability | Official Python SDK 1.30.0 negotiated revision 2025-11-25, discovered both tools, created and independently read a persisted ticket, and replayed the same key with one ticket row. [Client proof](mcp/official-client.json) |
| Recovery | **8/8** expected outcomes verified after actual process termination. Included a published file that correctly remained unknown after its staging evidence was removed. [Raw recovery runs](recovery/report.json) |
| Matched comparison | **8/8** deterministic fixture cells committed their expected effect. Normalized initial worlds and paired protected prompts matched. The fixture's selected implementation changed with catalog order while the verified business effects stayed equal. Live runs recorded `not_run`. [Context and effect records](comparison/report.json) |
| Production frontend | ESLint, TypeScript and Next.js production build passed. |
| Browser demonstration | Reviewed the governed MCP mapping and approved each resolved step. Verified the resulting ticket, file and inbox, then inspected the comparison and both proven/unknown recovery runs. A deterministic proposal fixture populated the editable workflow draft without creating any executions or effects. [Persistent-state verification](browser/proof.json) · [Draft verification](browser/draft-proof.json) |

### Selected browser captures

[Registry](browser/mcp-registry.png) · [Exact approval](browser/exact-approval.png) · [Workflow result](browser/workflow-inspector.png) · [Editable workflow draft](browser/workflow-draft.png) · [Comparison](browser/comparison.png) · [Recovered success](browser/recovery-success.png) · [Unknown outcome](browser/recovery-unknown.png) · [Mobile workflow](browser/workflow-mobile.png) · [Mobile recovery](browser/recovery-mobile.png)

### Measurement scope

Five scripted workflows had a median completion time of 44.45 ms and a p95 of 44.69 ms on this host. The five loopback MCP execution samples had a median of 19.04 ms and a p95 of 19.33 ms. Scripted review uses the actual approval transaction and excludes human waiting. The comparison combines neutral description rewording and display-order changes, so these records do not isolate either treatment on its own. They provide effect and context evidence for the offline fixture, with no measurement of live-model attack resistance.

This evidence package records local macOS verification. The local Docker CLI had no running Linux daemon. The configured Linux CI job runs after publication.

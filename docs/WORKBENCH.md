# Descriptionless Broker workbench

The workbench extends the V2 gateway with a governed MCP ticket adapter, durable workflows, and disposable laboratories. The typed compiler, approved mappings, and experiments all submit concrete operations through the gateway's existing authorization, approval, idempotency, and execution machinery.

## Local demonstration

Install the pinned dependencies and initialize the gateway as described in the [README](../README.md). For the demonstration, use fresh private directories. Use the displayed paths in the other terminals and keep the gateway credentials in their original terminal.

```sh
export GATEWAY_DATA_DIR="$(mktemp -d)"
export BROKER_TICKET_DIR="$(mktemp -d)"
printf 'Gateway %s\nTicket service %s\n' "$GATEWAY_DATA_DIR" "$BROKER_TICKET_DIR"
python -m gateway init
python -m gateway.mcp_service init --root "$BROKER_TICKET_DIR"
install -m 600 "$BROKER_TICKET_DIR/token" "$GATEWAY_DATA_DIR/mcp-tickets.token"
(umask 077; printf '[{"id":"tickets","port":8765}]\n' > "$GATEWAY_DATA_DIR/mcp-servers.json")
```

Start the services in separate terminals using those two directory values.

```sh
python -m gateway.mcp_service serve --root "$BROKER_TICKET_DIR" --port 8765
```

```sh
python -m uvicorn gateway.api:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Start the Next.js console on loopback as in the README. Connect as reviewer, open **MCP Registry**, discover the `create_incident_ticket` capability, review its schema and argument mapping, and enable it with priority 0. The local adapter has priority 10. Connect as requester in another tab and navigate to **Workflows**. Load the incident example, validate the graph, and submit it.

In the reviewer tab, approve the three steps as they become ready. First inspect the incident title, detail, and planned MCP adapter. The report and notification now show the exact ticket reference returned by the prior step. Review those resolved arguments before approving each step. The requester can inspect the workflow and each linked execution. The reviewer can confirm the notification in the inbox.

Use an independent local inspection to confirm the effects.

```sh
sqlite3 -readonly "$BROKER_TICKET_DIR/tickets.sqlite3" 'SELECT ticket_id,title,detail FROM tickets;'
sqlite3 -readonly "$GATEWAY_DATA_DIR/gateway.sqlite3" 'SELECT status,adapter_id,mapping_revision FROM executions ORDER BY created_at;'
sqlite3 -readonly "$GATEWAY_DATA_DIR/gateway.sqlite3" 'SELECT recipient,body FROM inbox;'
```

Open **Comparison Lab** and run the offline ticket scenario. Inspect the four model-context and effect records and their independent business-state observations. Open **Recovery Lab**, choose a file write and **After file publication**, then run once with intact evidence and once with staging evidence removed. Compare the recovered receipt and the retained unknown outcome. The lab starts a dedicated disposable worker in each run.

For a scripted reproduction, create an output parent and use a new directory for each run.

```sh
mkdir -p .runtime
python -m evals.expansion_evaluate --out .runtime/expansion-evaluation
```

Set `GATEWAY_OLLAMA_MODEL` to enable optional natural-language workflow drafts and live neutral-metadata comparisons. A workflow draft receives only the trusted schemas and reference vocabulary. Review the editable plan before submitting it. Use `--live` to opt into model execution in the evaluator. The workbench and CLI report `not_run` when model inference is unavailable. No hosted provider is required.

## Architecture and trust boundaries

| Boundary | Established behavior |
| --- | --- |
| Model to gateway | The protected planner receives only the trusted intent schema and user text. It has no path to MCP discovery, credentials, raw adapter results, or registry rows. Schema-valid decisions still pass the normal authorization checks. |
| Registry to MCP | A local operator places fixed numeric loopback service ports in an owner-only allowlist. A reviewer discovers capabilities and approves a compatible input/output contract digest. The tool maps to the trusted create-ticket intent with fixed translations of execution ID, title, and detail. Registry descriptions remain data. |
| MCP protocol and effects | The bridge uses the authenticated 2025-11-25 Streamable HTTP JSON response profile with no redirects, proxy inheritance, protocol sessions, external hosts or arbitrary program launch. The service returns typed structured output and independently persists by execution ID. The gateway requires a matching readback before recording success. This acknowledgment depends on the configured service. [MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) |
| Approval and routing | The canonical intent, policy revision, and routing snapshot are pinned at submission. The reviewer sees the planned adapter. The worker rechecks the snapshot at claim and validates MCP schema and endpoint binding before dispatch. A change to the enabled routing snapshot invalidates pending work pinned to the previous snapshot. Changes after dispatch cannot undo an in-flight action. |
| Crash and adapter uncertainty | The selected MCP mapping revision is durable before any external call. Lost, malformed, or inconsistent acknowledgments and interrupted MCP claims remain unknown. The gateway does not substitute a local ticket or retry the external operation automatically. Local SQLite effects and file checkpoints keep their original recovery rules. |
| Workflow boundary | A closed, versioned DAG checks uniqueness, acyclicity, resource bounds, argument types, and explicit dependencies. It resolves a bounded string join or a typed reference from allowlisted receipt fields. Paths and recipients remain static. A scheduler atomically links each resolved node to a scoped execution with a reserved key. Existing approvals bind the fully resolved arguments. |
| Evidence boundary | Lab data and credentials belong to temporary isolated runtimes. Recovery kills only the lab child process. An independent reader compares business rows, files, digests, execution attempts, and audit events. Lab results saved by the API are scoped to the initiating principal. |

The operator is trusted to provision the correct loopback MCP endpoint and protect service files. The gateway forwards only that service's token. Tool descriptions and input schemas are displayed only to reviewers, with the configured token redacted if echoed. Raw structured or text tool output is never put in model context. The product does not apply OS sandboxing to arbitrary MCP peers. That arbitrary execution path is outside the supported profile.

The workbench uses the gateway's existing single local scope. The standalone ticket service owns its own ticket database. A local `lookup-record` in the gateway queries gateway-owned tickets, so use the MCP service's read tool or direct local inspection for a ticket created there. Registering a mapping creates an immutable revision. Local execution remains selectable when the MCP adapter is disabled and the caller submits a fresh request.

## Durability and limits

A workflow has at most 12 nodes, 24 dependency edges, depth 8, and a 160 KB normalized input. After a node succeeds, the workflow captures only stable receipt fields needed to resolve later references. Pending linked receipts are protected from pruning until the workflow captures their results. Once a workflow terminates, its raw arguments are removed from node definitions. Completed steps are retained, failed or uncertain predecessors skip dependents, and independent branches can complete. There is no implicit rollback across business effects.

The gateway admits up to 24 active and 1,024 retained workflow identities, 10,000 execution identities, 512 mapping revisions, and 16 retained experimental reports per principal. Each retained lab report is limited to 128 KiB. One laboratory run and two discovery requests can run concurrently. Temporary lab working state is removed when the run exits. The owner should continue to manage runtime storage and backups.

SQLite schema revision 2 migrates an existing V1 runtime transactionally. Old active local work has no approved registry snapshot. Such work requires fresh submission rather than being routed under a newly introduced mapping. Completed records and known or uncertain historical outcomes are retained.

## How to read the experiments

- The comparison fixture changes only neutral descriptions and the display order of two equivalent implementations. Both architectures use the same domain capabilities and normal policy checks. The conventional path sees the catalog and selects an implementation; the protected planner sees the trusted schema. In offline mode, tool and intent outputs come from a deterministic fixture, so differences demonstrate instrumentation rather than model behavior.
- Each cell runs on a fresh, normalized synthetic world. Recorded implementation choice, policy result, attempted execution and independently queried final effects are separate fields. A run can legitimately show identical effects despite a different implementation choice.
- Recovery classification uses the actual process exit and independently inspected records. An existing target is insufficient to prove publication when its staging evidence is missing. This is a process-kill experiment on the tested filesystem. Hardware power loss and hostile processes sharing the service account are separate threats.

## API inventory

| Endpoint | Access and result |
| --- | --- |
| `POST /api/v1/workflows/compile` | Validates a bounded plan and returns deterministic topological order. |
| `POST /api/v1/workflows/proposals` | Optional local model drafts a workflow for authorized requesters and agents. |
| `POST /api/v1/workflows` | Requester or agent submits with a workflow idempotency key. |
| `GET /api/v1/workflows[/{id}]` | Own workflow state or the reviewer scope. Links to ordinary execution history. |
| `GET /api/v1/mcp` | Reviewer-only allowlist and active mapping heads. |
| `GET /api/v1/mcp/servers/{id}/tools` | Reviewer-only discovery of an allowlisted service. |
| `POST /api/v1/mcp/mappings` | Reviewer publishes an enabled or disabled mapping revision. |
| `POST /api/v1/labs/comparison`, `POST /api/v1/labs/recovery` | Run fixed, isolated scenarios and return stored evidence. |
| `GET /api/v1/labs[/{id}]` | Read the caller's own evidence and trace results. |

**Portfolio description.** Descriptionless Broker is a governed local execution gateway that routes typed model proposals through independent approval and deterministic adapter mapping. Its workbench demonstrates MCP-backed workflows, matched model-context comparisons, and independently inspected subprocess crash recovery.

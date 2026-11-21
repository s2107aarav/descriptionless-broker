# Gateway architecture and operations

The operational console uses `gateway.api:app`. The [research simulator](RESEARCH.md) has its own API, storage, and reports.

## Components and ownership

| Component | Responsibility |
| --- | --- |
| `gateway/contracts.py` | Closed V2 intent and receipt types. |
| `gateway/planner.py` | Builds messages from approved schema and user text. Bounded local model transport and a single format retry. |
| `gateway/config.py` | Server-controlled identity scope, capability policy, and immutable startup registry snapshot. |
| `gateway/engine.py` | Boundary revalidation, request binding, approval decisions, deterministic selection, execution state and projection. |
| `gateway/store.py` | SQLite schema, transaction and runtime lock, state updates, audit events and local maintenance. |
| `gateway/adapters.py`, `gateway/workspace.py` | Real SQLite and descriptor-relative filesystem operations. |
| `gateway/api.py` | Bearer authentication, restricted browser origins, body limits, readiness, and one background worker. |

The trust boundary is a private runtime directory, a dedicated local service account and loopback HTTP. The host operator, executable adapter code, policy, SQLite file and adapter registrations are trusted. A hostile process running as the service user can interfere with that trust boundary. Protect the account and its directory, including backups. The server refuses broad browser origins. It is designed for a local machine with one runtime owner and one worker.

## Intent contract

Every request includes `schema_version: "v2"` and one `operation`. Extra fields, invalid types, duplicate JSON keys and inconsistent model objects are rejected before execution.

| Operation | Fields | Authorization and effect |
| --- | --- | --- |
| `send-message` | `recipient`, `body` | Allowed active local recipient, review required. Commits an inbox item and a bound receipt in one SQLite transaction. |
| `read-file` | `path` | Requester capability under `workspace/` or `reports/` by default. Returns UTF-8 bytes as text for the requester, with size and SHA-256. |
| `write-file` | `path`, `content` | `reports/` by default, review required. Creates a new private file using create-only publication and returns a checked path, size and digest. |
| `lookup-record` | `collection`, `record_id` | Scoped exact-key lookup in gateway-owned `services` or `tickets`. Ticket detail is projected out for an agent principal. |
| `create-ticket` | `title`, `detail` | Review required. Creates a scoped ticket with stable ID, status open and revision 1. |
| `update-ticket` | `ticket_id`, `expected_revision`, optional `status` and/or `detail` | Gateway-owned tickets only, review required. Revision must match. Status moves Open → Investigating → Resolved. At least one value must change. |

Text is bounded. Files have a 64 KiB UTF-8 limit. The API accepts at most 512 KiB of raw HTTP input. A path is an exact relative name under a managed namespace, with at most eight segments beyond the namespace. The operator provisions any nested directories as owner-only before they can be used. Workflow steps are independent executions. Ticket updates use optimistic revisions and file writes refuse an occupied destination.

An approved MCP mapping can route `create-ticket` to an independent service database. Its tickets are not copied into the gateway database. Local lookup or update of such an ID returns `record_not_found`; it does not query or modify the external service. See the [workbench boundaries](WORKBENCH.md#architecture-and-trust-boundaries).

## Authentication and API

Application tokens are random 256-bit credentials. Only their SHA-256 digests are stored. The server derives principal, role and scope from the credential. Clients send `Authorization: Bearer <token>`. The browser holds the credential in memory for the session. The reviewer can view only the local scope, and requester and agent history queries are restricted by identity.

| Method and path | Contract |
| --- | --- |
| `GET /healthz`, `GET /readyz` | Minimal process and worker/storage health. |
| `GET /api/v1/session` | Returns principal, role and scope. |
| `POST /api/v1/proposals` | `{"text":"…"}`. Returns a typed proposal or clarification. Provider disabled/unavailable returns 503. |
| `POST /api/v1/executions` | V2 intent and 8–128 character `Idempotency-Key`. Returns 201 for a recorded request, including a policy denial, and 200 for a replay. |
| `GET /api/v1/executions` | Bounded recent history with `before` execution ID cursor and optional `limit`. |
| `GET /api/v1/executions/{id}`, `GET /api/v1/executions/{id}/events` | Authorized status, result and lifecycle. Unknown or inaccessible IDs return 404. |
| `GET /api/v1/approvals` | Reviewer-only queue with exact arguments, expiry, canonical request digest and policy revision. |
| `POST /api/v1/approvals/{id}/decision` | Reviewers send `decision` (approve or deny), `request_hash` and `policy_revision` from the reviewed request. |
| `GET /api/v1/inbox`, `GET /api/v1/research` | Most recent local messages addressed to the caller, or historical mock metrics. |

All writes, messages and ticket changes require approval by another principal. The transaction binds the authenticated reviewer to the pending execution and validates the snapshot and deadline. The worker rechecks current policy, requester and reviewer before claiming the adapter. Approval expires after 15 minutes, including when the action has been approved but is still queued. Changed policy invalidates old approvals. Approving the other principal is a role separation guarantee. It does not verify that the people holding the two example tokens are distinct individuals.

The runtime supports the three demo roles in one local scope. Optionally put a closed `policy.json` in the private runtime with `requester_operations`, `agent_operations`, `read_namespaces`, `write_namespaces` and `recipients`. Missing fields use defaults. Restart to activate a new policy revision. The default agent cannot read raw workspace files. A user with OS access can configure a different `registry.json` there. Each row declares `adapter_id`, `operation`, `implementation`, optional `version` (currently `"1"`), `enabled`, `priority` and `description`. Compatible enabled adapters are sorted by ascending priority then ID. Registry descriptions are excluded from the routing fingerprint and never read by the planner. Invalid configuration fails startup. The worker records its selected adapter and registry revision at claim. It never falls back after an uncertain effect.

## Lifecycle, retries and recovery

Initial outcomes are `denied`, `awaiting_approval` or `ready`. Pending approval can become ready, denied, or expired. Ready jobs are checked again and then claimed as executing, denied, or failed if no implementation can run. Executing jobs become succeeded, failed, or outcome unknown. The worker can requeue interrupted SQL effects and read-only work where persisted state proves the previous attempt did not commit. Recovery stops after three interrupted attempts.

Idempotency keys are unique within the authenticated requester and scope. The same canonical intent returns the existing execution even while it is pending. A changed intent returns `409 idempotency_conflict`. SQLite claims are durable before effects run. Duplicate requests and approvals are serialized. A dropped HTTP response leaves the operation discoverable by key, and replay does not repeat it. Recoveries and ordinary retries keep their original keys.

- **SQLite effects.** The business mutation, validated receipt and success event commit in one transaction. A durable executing state after restart means that the transaction did not commit. A committed receipt is replayed without re-execution.
- **Filesystem writes.** The worker walks trusted directory descriptors and rejects symbolic links, hardlinked input files, unsuitable file types or permissions, oversized data and non-UTF-8 reads. It stages an owner-only file in the target directory, fsyncs it, and checkpoints its inode, digest and parent identity. A no-replacement hard link publishes it atomically. Directory synchronization and verification precede terminal success. Cleanup of the staging link is tracked independently so it can survive a crash after the success commit. Unlink or directory-sync failure leaves success intact and cleanup pending. A retry synchronizes the directory even if the staging link is already absent. Until a remaining staging link is removed, the ordinary file reader rejects the target's extra hard link.
- **MCP effects.** The service commits the ticket in a separate database. The gateway records success only after a schema-valid acknowledgment and matching readback bind to the execution ID and requested title and detail. An interrupted claim or inconsistent evidence yields `outcome_unknown`, with no automatic retry or local fallback. The guarantee depends on truthful acknowledgments from the configured service.
- **Unknown outcome.** A write can be recovered as successful only if its staged and published entries still match the persisted inode and digest. When no durable staging checkpoint exists, the interrupted attempt never reached publication. Missing proof after the checkpoint yields `outcome_unknown`, preserves the evidence and never republishes. Stop the gateway and run `python -m gateway inspect-unknown <execution-id>` to view the saved path, inode and digest before inspecting the file and deciding whether to make a new request with a fresh key and destination. This command does not change the recorded outcome.

Health checks report the current process independently of an individual adapter’s success. Only a loopback Ollama endpoint is supported for model inference, with bounded network reads, timeouts and two concurrent proposals. Queue limits cap active work at 128 globally and 32 per principal. Receipt and audit queries are bounded. The OS operator is responsible for disk capacity and backups.

## Runtime maintenance and audit

Stop the worker for maintenance. Run `python -m gateway migrate` to validate schema version and integrity, `python -m gateway verify-audit` to check lifecycle consistency and transactional effect linkage, `python -m gateway rotate-token requester` to issue a new credential, or `python -m gateway revoke agent` to disable a principal.

The runtime contains `gateway.sqlite3`, its SQLite sidecars, and the private `files/` tree. Back up the entire directory after orderly shutdown, with permissions intact. To reset the demo, use a fresh `GATEWAY_DATA_DIR` and initialize again. This keeps the old runtime available for rollback and preserves research data.

Lifecycle events record the actor, policy version, adapter identity, attempt and state. The redacted journal does not contain message bodies, file data or raw prompts. Pending and executing rows hold exact typed arguments so authorized reviewers can inspect them. They are cleared on terminal outcomes. Authorized receipts and actual ticket/inbox data have separate retention needs. `python -m gateway prune --days 30` clears aged transient receipt data without freeing idempotency keys or removing real business objects. SQLite and filesystem backups retain their own copies until managed through the host’s retention process. The audit is transactional and consistency-checkable. Host administrator tampering is outside its guarantee.

Plain text returned by an adapter remains data. The planner API never consumes previous adapter outputs or execution errors. A client that sends those data to another model is responsible for that client’s context boundary.

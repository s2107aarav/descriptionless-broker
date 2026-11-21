# Descriptionless Broker

Descriptionless Broker is a local gateway for governed intent execution. A typed request or optional local model proposes a domain operation. Trusted code validates the intent, checks permissions, obtains any required approval, selects the adapter, and records the effect. The model sees the intent definitions and user request. The registry and implementation metadata stay outside that path.

The gateway creates and updates real tickets, reads and creates real workspace files, retrieves persisted records, and delivers messages to a local inbox. A successful inbox receipt confirms the database commit. It makes no claim about email delivery or whether someone read the message.

## Run the gateway

Use Python 3.12 and Node 22 on macOS or Linux. Keep operational state on a private local filesystem that supports hard links and directory synchronization.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m gateway init
python -m uvicorn gateway.api:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Initialization prints three generated application credentials once. Keep them private. The requester and agent submit operations. The reviewer approves them and can inspect the scoped audit trail. The runtime defaults to `~/Library/Application Support/Descriptionless Broker/gateway` on macOS and `$XDG_STATE_HOME/descriptionless-broker/gateway` on Linux, using `~/.local/state` when `XDG_STATE_HOME` is unset. Configure another private local root with `GATEWAY_DATA_DIR`. See [.env.example](.env.example) for the other settings.

Start the console in a second terminal.

```sh
cd frontend
npm ci
npm run dev -- --hostname 127.0.0.1
```

Open [the local execution console](http://127.0.0.1:3000), connect with the requester credential, and use the typed editor. For a production frontend build, use `npm run build` followed by `npm run start -- --hostname 127.0.0.1`. Set `NEXT_PUBLIC_BROKER_API_URL` at build time if the API uses a different loopback port. Configure the corresponding frontend origin on the backend.

### Walk through an incident

1. Connect as the requester. Submit **Create a ticket** with a title and detail.
2. Connect as the reviewer in a second tab. In **Approvals**, check the exact request and approve it. Use **History** to read the resulting ticket ID.
3. As the requester, submit **Write a file** with a fresh path in `reports/` and include the ticket ID in its content. Submit **Send a message** to `reviewer` with the ticket ID and report path.
4. Approve both requests as the reviewer. The requester can verify the report with **Read a file** and the ticket with **Look up a record** in the Tickets collection. The reviewer can verify delivery in **Inbox**.
5. Submit **Update a ticket** with the current revision and set its status to Investigating. The revision in the resulting receipt is incremented. A stale revision fails without updating the ticket.

Each step has its own approval and receipt. The incident sequence can partially succeed. Review the history before retrying an operation with an uncertain outcome.

The typed path runs without a model or third-party credentials. For natural-language proposals, set `GATEWAY_OLLAMA_MODEL` to an installed local model before starting the API. The console displays the proposed fields for review before submission. If the provider is unavailable, the gateway returns an explicit error.

## Verify

```sh
python -m pytest -q
python -m evals.gateway_evaluate --out .runtime/gateway-evaluation.json
python -m evals.run --mode offline --out .runtime/research-evaluation.json
cd frontend
npm run lint
npm exec -- tsc --noEmit
npm run build
```

[Architecture and operations](docs/GATEWAY.md) describes the public API, approval policy, data model, failure states, and recovery. [Research provenance](docs/RESEARCH.md) explains the retained mock harness and historical evidence. [Practical verification results](evidence/gateway-2026-11-21/README.md) records the executed workload and browser checks.

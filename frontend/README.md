# Descriptionless Broker console

Run the local gateway using the project root [setup instructions](../README.md). Then install the console dependencies and start Next.js with Node 22 or later.

```sh
npm ci
npm run dev -- --hostname 127.0.0.1
```

Open `http://127.0.0.1:3000` and connect using a gateway access token. Open a second browser tab with a reviewer credential to process approval requests. Each tab holds its access token in memory. Disconnecting clears the token and the displayed execution data.

The workspace supports direct typed submissions. Its natural-language mode requests a proposal from a configured local provider and opens that proposal in the typed editor for review. Approvals show the exact submitted arguments. History shows execution receipts and lifecycle events, and Inbox shows messages delivered to your connected principal.

## API origins

The console defaults to `http://127.0.0.1:8000`. Set `NEXT_PUBLIC_BROKER_API_URL` before starting development or building for another gateway origin. The gateway must allow the console origin. Use local access tokens in the console and keep provider credentials server-side.

Research shows historical evaluation summaries. To use the preserved simulation dashboard, launch the legacy backend separately and set `NEXT_PUBLIC_BROKER_RESEARCH_API_URL` to its origin before building or starting development. The legacy dashboard is at `/legacy-research` and uses the legacy API token.

## Checks and production preview

```sh
npm run lint
npm run build
npm exec -- tsc --noEmit
npm run start -- --hostname 127.0.0.1
```

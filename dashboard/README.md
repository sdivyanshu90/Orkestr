# Orkestr dashboard

[Documentation home](../docs/README.md) · [API reference](../docs/reference.md)
· [Operations](../docs/operations.md)

The dashboard is a React/TypeScript/React Flow client for the trusted local API.
It visualizes durable run data; it does not execute model work in the browser or
supply an independent verifier. A displayed task status or evidence badge must
not be treated as final request acceptance.

## Start the service and client

From the repository root, using the installed Python environment:

```bash
python -m pip install fastapi uvicorn
python -m agent_squad.api.server --host 127.0.0.1 --port 8080 --data-dir .agent-squad
```

FastAPI/uvicorn are optional and there is no `api` project extra. Install and
validate optional dependencies explicitly; the base release lock is not proof
of the optional service environment.

In a second terminal:

```bash
cd dashboard
npm ci
npm run dev
```

Use the existing npm lockfile. The project was developed with Node 22; validate
the Node/Vite environment you intend to support rather than inferring a runtime
matrix from one successful build. Vite defaults to port 5173 and may select a
free port if it is occupied. Follow the URL printed by the development server.

`vite.config.ts` proxies `/api` and `/healthz` to `http://127.0.0.1:8080`.
`AGENT_SQUAD_API` changes the backend origin and `DASHBOARD_PORT` changes the dev
port. A production build does not automatically inherit the development proxy;
provide an appropriate same-origin/reverse-proxy deployment if you serve it.

```bash
npm run typecheck
npm run build
npm run preview
```

## Data and interpretation

| View | Source | Interpret carefully |
|---|---|---|
| Runs / new run | Run list and create-run API | Create-run fields are narrower than RunConfig |
| DAG | Graph endpoint | Node state describes task execution |
| Timeline | Agent started/finished events | Derived timing, not a separate authoritative scheduler record |
| Task | Run/task data and check records | A passed task is not automatically an accepted request |
| Events / messages | Durable events and typed payloads | Informational messages are not authority grants |
| Artifacts | Metadata/provenance | A hash identifies content, not correctness |
| Approvals | Approval endpoints | Authorizes an exact protected effect, not the final implementation |
| Cancellation | Cancel endpoint | Cancellation/recovery can still require resource/effect reconciliation |

The selected run is polled; terminal execution status can stop polling. The run
list and health checks have their own intervals. Incremental events use
`after_seq`. Inspect the source when changing polling behavior instead of assuming
polling is an event-streaming guarantee.

The API also exposes `GET /api/runs/{run_id}/contract`. The current UI must be
checked separately for how much contract detail it renders; do not invent a
contract panel merely because the endpoint exists. Use the run JSON or contract
endpoint to verify `accepted`, `rejected`, `inconclusive`, `timed_out` or `cancelled`.

## Local authority and failures

Keep the backend on loopback and share the correct data directory with the CLI.
The API has no established public multi-user authentication model. An actor label
on an approval is not authentication. The new-run form's default development
profile is not a complete hardened-execution configuration interface.

When the backend is unreachable, examine health/API errors and keep run-data
locations consistent. An empty list may mean a different data directory, not a
failed model. Read check errors, contract evidence and ownership history before
using a visual badge as the basis for a retry or integration decision.

## Source map

`src/App.tsx` owns layout/polling; `src/api.ts` wraps endpoints;
`src/types.ts` describes response shapes; `src/components/` renders views;
`src/lib/` derives graph layout, timeline, formatting and evidence presentation.
Changes to API JSON or acceptance semantics need corresponding UI type/rendering
and regression checks. A shared database does not automatically make labels
semantically consistent.

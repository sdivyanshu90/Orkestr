# agent-squad dashboard

A React + TypeScript + React Flow dashboard for the **agent-squad** multi-agent
orchestration runtime.

It shows the *actual* execution state of a run. Every value on screen comes
from the FastAPI service in `src/agent_squad/api/server.py`, which is a
read-mostly view over the same SQLite state the CLI uses. There is no mock
data, no simulation and no synthetic "demo" run: if the backend says a task
is `running`, the dashboard says `running`.

## Requirements

- Node.js 18 or newer (developed on Node 22)
- The agent-squad API service running locally

## Running it

**1. Start the backend** (from the repository root):

```bash
python -m agent_squad.api.server
```

It listens on `http://127.0.0.1:8080` and serves the run state from the
`.agent-squad` data directory. Use `--data-dir` and `--port` to change either:

```bash
python -m agent_squad.api.server --port 8080 --data-dir .agent-squad
```

If you want the dashboard to show a run, produce one first:

```bash
agent-squad run "Add a rounding helper" --repo /path/to/repo \
    --spec examples/request-rounding.json
```

**2. Start the dashboard** (from this directory):

```bash
npm install
npm run dev
```

Then open <http://localhost:5173>.

The Vite dev server proxies `/api` and `/healthz` to `http://127.0.0.1:8080`, so
the browser only ever talks to the dev server's own origin. No CORS setup and
no environment-specific base URL are needed.

### Other commands

```bash
npm run typecheck   # tsc --noEmit
npm run build       # typecheck + production build into dist/
npm run preview     # serve the production build
```

## What the dashboard shows

| View | Source endpoint | What it renders |
|---|---|---|
| **Runs** (sidebar) | `GET /api/runs` | Every stored run with status, strategy and update time. Click to select. |
| **New run** | `POST /api/runs` | Objective, description, repo path, strategy, max workers (plus optional provider/model). Starts the run in the background and selects it. |
| **DAG** | `GET /api/runs/{run_id}/graph` | The task graph in React Flow. Nodes are coloured by task status and show title, role, attempt count, token count and the highest verification level reached. Arrows point from a prerequisite to the task that depends on it. |
| **Timeline** | `GET /api/runs/{run_id}/events` | A Gantt built from `agent.started` / `agent.finished` events, one row per task, coloured by outcome (finished ok / finished with error / still running). |
| **Task** | `GET /api/runs/{run_id}` + `/graph` | Status, role, attempts, tokens, cost, owner, timings, the acceptance criteria with their verification state, and the recorded check records (command, exit code, output excerpt, error). |
| **Events** | `GET /api/runs/{run_id}/events?after_seq=` | A live tail of the event log, filterable by event type and by task/actor. Failures are shown in red. |
| **Artifacts** | `GET /api/runs/{run_id}/artifacts` | Artifact kind, truncated sha256, size, media type and full provenance (producer, task, attempt, base revision). |
| **Approvals** | `GET /api/approvals?run_id=` | Pending approval requests with Approve / Deny buttons calling `POST /api/approvals/{approval_id}`. |
| **Messages** | `GET /api/runs/{run_id}/messages` | Inter-agent messages and their typed payloads. |
| **Cancel run** | `POST /api/runs/{run_id}/cancel` | Cancels the selected run with a recorded reason. |

### Polling

The selected run is polled every **2 seconds**. Polling stops automatically once
the run reaches a terminal status (`succeeded`, `failed`, `cancelled`,
`completed_with_failures`); the header then reads *run finished, polling
stopped*. The run list and the backend health are checked every 5 seconds.

Events are fetched incrementally with `after_seq`, so the log is appended to
rather than reloaded.

### Verification levels

The dashboard distinguishes all seven levels of the evidence ladder:

```
none  <  worker_reported  <  patch_produced  <  validation_executed
     <  checks_passed     <  integration_validated  <  human_approved
```

Each level has its own colour and a full legend is shown in the sidebar and in
the Task view. Wherever a level appears it is labelled as the **highest level
reached**.

The distinction the ladder exists to protect is stated in the UI itself:

> `checks_passed` means: *the specified checks ran and produced these results.*
> It does **not** mean the change is correct.

A task can sit at `checks_passed` and still be wrong; the Task view shows the
individual check records so you can judge the result rather than the badge.

### Failure handling

- **Backend unreachable** — `/healthz` is the liveness signal. If it fails, the
  sidebar and main panel switch to an explicit *backend unreachable* state and
  the dashboard keeps retrying, so it recovers on its own once the service is
  back.
- **API errors** — a banner shows the endpoint and the server's `detail`
  message. For example, starting a run against a missing repository reports
  `400 repository not found: /path`.
- **Empty state** — when the backend has no runs yet, both the sidebar and the
  main panel say so instead of showing an empty table.

## Layout

```
dashboard/
├── index.html
├── package.json
├── tsconfig.json
├── vite.config.ts          dev server + /api and /healthz proxy
└── src/
    ├── main.tsx
    ├── App.tsx             layout, polling, tab routing
    ├── api.ts              typed fetch wrappers for every endpoint
    ├── types.ts            response types
    ├── styles.css          plain CSS with custom properties
    ├── components/
    │   ├── ApprovalsPanel.tsx
    │   ├── ArtifactsPanel.tsx
    │   ├── DagView.tsx
    │   ├── ErrorBanner.tsx
    │   ├── EventLog.tsx
    │   ├── MessagesPanel.tsx
    │   ├── NewRunForm.tsx
    │   ├── RunList.tsx
    │   ├── StatusPill.tsx
    │   ├── TaskDetail.tsx
    │   ├── TaskNode.tsx
    │   ├── TimelineView.tsx
    │   └── VerificationLegend.tsx
    └── lib/
        ├── events.ts       event severity classification
        ├── format.ts       formatting helpers
        ├── gantt.ts        timeline derived from the event log
        ├── layout.ts       layered DAG layout
        └── verification.ts the verification ladder
```

`src/api.ts` holds a typed wrapper for every endpoint the API exposes;
`src/types.ts` holds the matching response types. TypeScript runs in `strict`
mode and the exported code paths contain no `any`.

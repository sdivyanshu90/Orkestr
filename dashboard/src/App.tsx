import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import {
  ApiError,
  cancelRun,
  decideApproval,
  getArtifacts,
  getEvents,
  getGraph,
  getMessages,
  getRun,
  health,
  listApprovals,
  listRuns,
} from './api';
import type {
  Approval,
  Artifact,
  GraphResponse,
  MessageRecord,
  RunDetailResponse,
  RunEvent,
  RunStatus,
  RunSummary,
  TaskState,
} from './types';
import ErrorBanner, { type BannerState } from './components/ErrorBanner';
import NewRunForm from './components/NewRunForm';
import RunList from './components/RunList';
import StatusPill, { runTone } from './components/StatusPill';
import DagView from './components/DagView';
import TimelineView from './components/TimelineView';
import TaskDetail from './components/TaskDetail';
import EventLog from './components/EventLog';
import ArtifactsPanel from './components/ArtifactsPanel';
import ApprovalsPanel from './components/ApprovalsPanel';
import MessagesPanel from './components/MessagesPanel';
import VerificationLegend from './components/VerificationLegend';
import { formatCost, formatNumber, formatTimestamp } from './lib/format';

const POLL_INTERVAL_MS = 2000;
const RUN_LIST_INTERVAL_MS = 5000;
const MAX_EVENTS = 5000;

const TERMINAL_RUN_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>([
  'succeeded',
  'failed',
  'cancelled',
  'completed_with_failures',
]);

type Tab = 'dag' | 'timeline' | 'task' | 'events' | 'artifacts' | 'approvals' | 'messages';

const TABS: { id: Tab; label: string }[] = [
  { id: 'dag', label: 'DAG' },
  { id: 'timeline', label: 'Timeline' },
  { id: 'task', label: 'Task' },
  { id: 'events', label: 'Events' },
  { id: 'artifacts', label: 'Artifacts' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'messages', label: 'Messages' },
];

type BackendState = 'unknown' | 'online' | 'offline';

function isTerminalRun(status: RunStatus | null | undefined): boolean {
  return status !== null && status !== undefined && TERMINAL_RUN_STATUSES.has(status);
}

function mergeEvents(existing: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  if (incoming.length === 0) return existing;
  const seen = new Set(existing.map((event) => event.seq));
  const merged = existing.slice();
  incoming.forEach((event) => {
    if (!seen.has(event.seq)) {
      seen.add(event.seq);
      merged.push(event);
    }
  });
  merged.sort((a, b) => a.seq - b.seq);
  return merged.length > MAX_EVENTS ? merged.slice(merged.length - MAX_EVENTS) : merged;
}

export default function App(): ReactElement {
  const [backendState, setBackendState] = useState<BackendState>('unknown');
  const [banner, setBanner] = useState<BannerState | null>(null);

  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);

  const [detail, setDetail] = useState<RunDetailResponse | null>(null);
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [lastSeq, setLastSeq] = useState(0);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [messages, setMessages] = useState<MessageRecord[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);

  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('dag');
  const [approvalPending, setApprovalPending] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [cancelReason, setCancelReason] = useState('');

  const lastSeqRef = useRef(0);
  const terminalRef = useRef(false);
  const selectedRunIdRef = useRef<string | null>(null);
  const offlineRef = useRef(false);

  selectedRunIdRef.current = selectedRunId;

  /**
   * Mark the backend as unreachable.  The banner is only raised on the
   * transition so a dismissed message does not come back on every poll.
   */
  const markOffline = useCallback((detail: string): void => {
    const wasOffline = offlineRef.current;
    offlineRef.current = true;
    setBackendState('offline');
    if (!wasOffline) {
      setBanner({
        kind: 'offline',
        text: `Backend unreachable (${detail}). Start it with "python -m agent_squad.api.server".`,
      });
    }
  }, []);

  /**
   * `/healthz` is the liveness signal.  A dev proxy answers with HTTP 500
   * when the upstream is gone, so any failure here means the service is not
   * serving -- not that the request was rejected.
   */
  const checkBackend = useCallback(async (): Promise<boolean> => {
    try {
      await health();
      if (offlineRef.current) {
        offlineRef.current = false;
        setBanner({ kind: 'info', text: 'Backend is reachable again.' });
      }
      setBackendState('online');
      return true;
    } catch (error) {
      const detail =
        error instanceof ApiError
          ? error.detail || error.message
          : error instanceof Error
            ? error.message
            : String(error);
      markOffline(detail);
      return false;
    }
  }, [markOffline]);

  /**
   * Report a failed request.  The health endpoint is consulted so a dead
   * backend is shown as "unreachable" rather than as an opaque status code.
   */
  const reportError = useCallback(
    async (error: unknown, context: string): Promise<void> => {
      if (error instanceof ApiError && error.unreachable) {
        markOffline(error.detail);
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      const alive = await checkBackend();
      if (!alive) return;
      setBanner({ kind: 'error', text: `${context}: ${message}` });
    },
    [checkBackend, markOffline],
  );

  /* ------------------------------------------------------------- run list */

  const refreshRuns = useCallback(async (): Promise<void> => {
    try {
      const response = await listRuns();
      setRuns(response.runs);
      setRunsLoading(false);
    } catch (error) {
      setRunsLoading(false);
      await reportError(error, 'listing runs');
    }
  }, [reportError]);

  useEffect(() => {
    void checkBackend();
    void refreshRuns();
  }, [checkBackend, refreshRuns]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      void checkBackend();
      void refreshRuns();
    }, RUN_LIST_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [checkBackend, refreshRuns]);

  /* --------------------------------------------------------- run polling */

  const refreshRun = useCallback(async (): Promise<void> => {
    const runId = selectedRunIdRef.current;
    if (!runId) return;
    try {
      const [runDetail, runGraph, runEvents, runArtifacts, runApprovals] = await Promise.all([
        getRun(runId),
        getGraph(runId),
        getEvents(runId, lastSeqRef.current, 500),
        getArtifacts(runId),
        listApprovals(runId),
      ]);
      lastSeqRef.current = runEvents.last_seq;
      setDetail(runDetail);
      setGraph(runGraph);
      setEvents((previous) => mergeEvents(previous, runEvents.events));
      setLastSeq(runEvents.last_seq);
      setArtifacts(runArtifacts.artifacts);
      setApprovals(runApprovals.approvals);
      if (offlineRef.current) {
        offlineRef.current = false;
        setBackendState('online');
        setBanner({ kind: 'info', text: 'Backend is reachable again.' });
      }
      setBackendState('online');
    } catch (error) {
      await reportError(error, `loading run ${runId}`);
    }
  }, [reportError]);

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      setGraph(null);
      setEvents([]);
      setLastSeq(0);
      setArtifacts([]);
      setApprovals([]);
      setMessages([]);
      setSelectedTaskId(null);
      setConfirmCancel(false);
      return;
    }

    lastSeqRef.current = 0;
    terminalRef.current = false;
    setDetail(null);
    setGraph(null);
    setEvents([]);
    setLastSeq(0);
    setArtifacts([]);
    setApprovals([]);
    setMessages([]);
    setSelectedTaskId(null);
    setConfirmCancel(false);

    void refreshRun();

    const timer = window.setInterval(() => {
      if (terminalRef.current) return;
      void refreshRun();
    }, POLL_INTERVAL_MS);

    return () => window.clearInterval(timer);
  }, [selectedRunId, refreshRun]);

  useEffect(() => {
    terminalRef.current = isTerminalRun(detail?.run.status);
  }, [detail]);

  /* ------------------------------------------------------------- messages */

  useEffect(() => {
    if (tab !== 'messages' || !selectedRunId) return;
    let cancelled = false;
    setMessagesLoading(true);
    void (async () => {
      try {
        const response = await getMessages(selectedRunId);
        if (!cancelled) setMessages(response.messages);
      } catch (error) {
        if (!cancelled) await reportError(error, 'loading messages');
      } finally {
        if (!cancelled) setMessagesLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tab, selectedRunId, reportError]);

  /* -------------------------------------------------------------- actions */

  const selectRun = useCallback((runId: string): void => {
    setSelectedRunId(runId);
    setTab('dag');
  }, []);

  const decide = useCallback(
    (approvalId: string, approved: boolean, decidedBy: string, reason: string): void => {
      setApprovalPending(approvalId);
      void (async () => {
        try {
          await decideApproval(approvalId, {
            approved,
            decided_by: decidedBy,
            reason,
          });
          setBanner({
            kind: 'info',
            text: `Approval ${approvalId} ${approved ? 'approved' : 'denied'}.`,
          });
          await refreshRun();
        } catch (error) {
          await reportError(error, `deciding approval ${approvalId}`);
        } finally {
          setApprovalPending(null);
        }
      })();
    },
    [refreshRun, reportError],
  );

  const doCancel = useCallback((): void => {
    if (!selectedRunId) return;
    setCancelling(true);
    void (async () => {
      try {
        await cancelRun(selectedRunId, {
          reason: cancelReason.trim() || 'cancelled via the dashboard',
        });
        setConfirmCancel(false);
        setBanner({ kind: 'info', text: `Run ${selectedRunId} cancelled.` });
        await refreshRun();
        await refreshRuns();
      } catch (error) {
        await reportError(error, `cancelling run ${selectedRunId}`);
      } finally {
        setCancelling(false);
      }
    })();
  }, [cancelRun, cancelReason, refreshRun, refreshRuns, reportError, selectedRunId]);

  /* ------------------------------------------------------------- derived */

  const taskStates = useMemo(() => {
    const map = new Map<string, TaskState>();
    (detail?.tasks ?? []).forEach((task) => map.set(task.id, task));
    return map;
  }, [detail]);

  const selectedNode = useMemo(() => {
    if (!selectedTaskId) return null;
    return graph?.nodes.find((node) => node.id === selectedTaskId) ?? null;
  }, [graph, selectedTaskId]);

  const selectedState = selectedTaskId ? (taskStates.get(selectedTaskId) ?? null) : null;

  const totals = useMemo(() => {
    const tasks = detail?.tasks ?? [];
    const tokens = tasks.reduce((sum, task) => sum + (task.tokens_used ?? 0), 0);
    const cost = tasks.reduce((sum, task) => sum + (task.cost_used_usd ?? 0), 0);
    return { tokens, cost, tasks: tasks.length };
  }, [detail]);

  const runStatus = detail?.run.status ?? null;
  const polling = selectedRunId !== null && !isTerminalRun(runStatus);

  const healthLabel =
    backendState === 'online'
      ? 'backend online'
      : backendState === 'offline'
        ? 'backend unreachable'
        : 'checking backend';

  const renderTab = (): ReactElement => {
    switch (tab) {
      case 'dag':
        return (
          <DagView
            graph={graph}
            selectedTaskId={selectedTaskId}
            onSelectTask={(taskId) => {
              setSelectedTaskId(taskId);
              setTab('task');
            }}
          />
        );
      case 'timeline':
        return (
          <TimelineView
            events={events}
            nodes={graph?.nodes ?? []}
            selectedTaskId={selectedTaskId}
            onSelectTask={(taskId) => {
              setSelectedTaskId(taskId);
              setTab('task');
            }}
          />
        );
      case 'task':
        return (
          <>
            <TaskDetail node={selectedNode} state={selectedState} />
            <VerificationLegend />
          </>
        );
      case 'events':
        return <EventLog events={events} lastSeq={lastSeq} polling={polling} />;
      case 'artifacts':
        return <ArtifactsPanel artifacts={artifacts} />;
      case 'approvals':
        return (
          <ApprovalsPanel
            approvals={approvals}
            pending={approvalPending}
            onDecide={(approvalId, approved, decidedBy, reason) =>
              decide(approvalId, approved, decidedBy, reason)
            }
          />
        );
      case 'messages':
        return <MessagesPanel messages={messages} loading={messagesLoading} />;
      default:
        return <div className="panel-empty" />;
    }
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-head">
          <div>
            <h1 className="app-title">agent-squad</h1>
            <p className="app-sub">execution dashboard</p>
          </div>
          <span className={`health health-${backendState}`}>{healthLabel}</span>
        </div>

        <NewRunForm
          onCreated={(runId) => {
            setBanner({ kind: 'info', text: `Run ${runId} started.` });
            void refreshRuns();
            selectRun(runId);
          }}
          onError={(message) => setBanner({ kind: 'error', text: `starting run: ${message}` })}
        />

        <div className="sidebar-section">
          <div className="sidebar-section-head">
            <span className="section-title">Runs</span>
            <button type="button" className="btn btn-ghost btn-tiny" onClick={() => void refreshRuns()}>
              refresh
            </button>
          </div>
          <RunList
            runs={runs}
            selectedRunId={selectedRunId}
            onSelect={selectRun}
            loading={runsLoading}
            backendState={backendState}
          />
        </div>

        <details className="sidebar-section sidebar-legend">
          <summary className="section-title">verification levels</summary>
          <VerificationLegend />
        </details>
      </aside>

      <main className="main">
        <ErrorBanner banner={banner} onDismiss={() => setBanner(null)} />

        {!selectedRunId ? (
          <div className="panel-empty panel-empty-main">
            {backendState === 'offline' ? (
              <>
                <p className="empty-title">Backend unreachable</p>
                <p className="muted small">
                  The dashboard cannot reach the agent-squad API. Start the service with{' '}
                  <code>python -m agent_squad.api.server</code> and reload this page. The
                  dashboard retries every few seconds.
                </p>
              </>
            ) : runs.length === 0 ? (
              <>
                <p className="empty-title">No runs yet</p>
                <p className="muted small">
                  The backend reports no stored runs. Start one from the sidebar, or run the
                  CLI so that it appears here.
                </p>
              </>
            ) : (
              <>
                <p className="empty-title">Select a run</p>
                <p className="muted small">
                  Pick a run from the list on the left to see its task DAG, timeline, events,
                  artifacts and approvals.
                </p>
              </>
            )}
          </div>
        ) : (
          <>
            <header className="run-head">
              <div className="run-head-main">
                <div className="run-head-titles">
                  <span className="mono run-head-id">{selectedRunId}</span>
                  {runStatus ? <StatusPill value={runStatus} tone={runTone(runStatus)} /> : null}
                  {detail?.run.strategy ? (
                    <span className="small muted">strategy {detail.run.strategy}</span>
                  ) : null}
                </div>
                <div className="run-head-stats">
                  <span className="small">tasks {totals.tasks}</span>
                  <span className="small">tokens {formatNumber(totals.tokens)}</span>
                  <span className="small">cost {formatCost(totals.cost)}</span>
                  <span className="small muted">
                    updated {formatTimestamp(detail?.run.updated_at ?? null)}
                  </span>
                  <span className={`poll-state ${polling ? 'poll-active' : 'poll-idle'}`}>
                    {polling
                      ? 'polling every 2s'
                      : isTerminalRun(runStatus)
                        ? 'run finished, polling stopped'
                        : 'idle'}
                  </span>
                </div>
              </div>

              <div className="run-head-actions">
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => void refreshRun()}
                  title="Refresh the selected run now"
                >
                  refresh
                </button>
                {runStatus && !isTerminalRun(runStatus) ? (
                  <button
                    type="button"
                    className="btn btn-danger"
                    onClick={() => setConfirmCancel((previous) => !previous)}
                  >
                    cancel run
                  </button>
                ) : null}
              </div>
            </header>

            {confirmCancel ? (
              <div className="cancel-panel">
                <span className="small">Cancel run {selectedRunId}?</span>
                <input
                  type="text"
                  value={cancelReason}
                  placeholder="reason (recorded on the cancellation event)"
                  onChange={(event) => setCancelReason(event.target.value)}
                />
                <button
                  type="button"
                  className="btn btn-danger"
                  disabled={cancelling}
                  onClick={() => void doCancel()}
                >
                  {cancelling ? 'cancelling...' : 'confirm cancel'}
                </button>
                <button type="button" className="btn btn-ghost" onClick={() => setConfirmCancel(false)}>
                  keep running
                </button>
              </div>
            ) : null}

            <nav className="tabs">
              {TABS.map((entry) => (
                <button
                  key={entry.id}
                  type="button"
                  className={`tab${tab === entry.id ? ' tab-active' : ''}`}
                  onClick={() => setTab(entry.id)}
                >
                  {entry.label}
                  {entry.id === 'approvals' && approvals.length > 0 ? (
                    <span className="tab-badge">{approvals.length}</span>
                  ) : null}
                  {entry.id === 'events' && events.length > 0 ? (
                    <span className="tab-badge tab-badge-quiet">{events.length}</span>
                  ) : null}
                </button>
              ))}
            </nav>

            <section className="panel">{renderTab()}</section>
          </>
        )}
      </main>
    </div>
  );
}

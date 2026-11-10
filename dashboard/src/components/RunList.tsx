import type { ReactElement } from 'react';
import type { RunSummary } from '../types';
import { formatTimestamp } from '../lib/format';
import StatusPill, { runTone } from './StatusPill';

interface RunListProps {
  runs: RunSummary[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
  loading: boolean;
}

export default function RunList({
  runs,
  selectedRunId,
  onSelect,
  loading,
}: RunListProps): ReactElement {
  if (loading && runs.length === 0) {
    return <p className="muted small">loading runs...</p>;
  }

  if (runs.length === 0) {
    return (
      <div className="empty">
        <p className="empty-title">No runs yet</p>
        <p className="muted small">
          The backend has no stored runs. Start one with the form above, or run the CLI
          (<code>agent-squad run</code>) so it appears here.
        </p>
      </div>
    );
  }

  return (
    <table className="run-table">
      <thead>
        <tr>
          <th>run</th>
          <th>status</th>
          <th>strategy</th>
          <th>updated</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const selected = run.run_id === selectedRunId;
          return (
            <tr
              key={run.run_id}
              className={selected ? 'selected' : undefined}
              onClick={() => onSelect(run.run_id)}
              tabIndex={0}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onSelect(run.run_id);
                }
              }}
            >
              <td className="mono run-id" title={run.run_id}>
                {run.run_id}
              </td>
              <td>
                <StatusPill value={run.status} tone={runTone(run.status)} />
              </td>
              <td className="muted small">{run.strategy ?? '-'}</td>
              <td className="muted small nowrap">{formatTimestamp(run.updated_at)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

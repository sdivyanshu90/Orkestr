import type { ReactElement } from 'react';
import type { GraphNode, TaskState } from '../types';
import { formatCost, formatDuration, formatNumber, formatTimestamp } from '../lib/format';
import {
  CRITERION_STATE_DESCRIPTIONS,
  CRITERION_STATE_LABELS,
  VERIFICATION_DESCRIPTIONS,
} from '../lib/verification';
import StatusPill, { taskTone } from './StatusPill';
import VerificationBadge from './VerificationBadge';

interface TaskDetailProps {
  node: GraphNode | null;
  state: TaskState | null;
}

export default function TaskDetail({ node, state }: TaskDetailProps): ReactElement {
  if (!node) {
    return (
      <div className="panel-empty">
        <p className="empty-title">No task selected</p>
        <p className="muted small">
          Select a node in the DAG, or a row in the timeline, to inspect its state,
          acceptance criteria and check records.
        </p>
      </div>
    );
  }

  const verification = node.verification;
  const states = verification?.criterion_states ?? {};
  const checks = verification?.checks ?? [];

  return (
    <div className="task-detail">
      <header className="task-detail-head">
        <div>
          <h2 className="task-detail-title">{node.title}</h2>
          <p className="mono muted small">{node.id}</p>
        </div>
        <div className="task-detail-head-right">
          <StatusPill value={node.status} tone={taskTone(node.status)} />
          <VerificationBadge level={verification?.level ?? null} />
        </div>
      </header>

      <section className="kv-grid">
        <div className="kv">
          <span className="kv-key">role</span>
          <span className="kv-value">{node.role}</span>
        </div>
        <div className="kv">
          <span className="kv-key">attempts</span>
          <span className="kv-value">
            {node.attempts}
            {state ? ` / ${state.max_attempts}` : ''}
          </span>
        </div>
        <div className="kv">
          <span className="kv-key">tokens</span>
          <span className="kv-value">{formatNumber(node.tokens_used)}</span>
        </div>
        <div className="kv">
          <span className="kv-key">cost</span>
          <span className="kv-value">{formatCost(node.cost_usd)}</span>
        </div>
        {state?.owner ? (
          <div className="kv">
            <span className="kv-key">owner</span>
            <span className="kv-value mono">{state.owner}</span>
          </div>
        ) : null}
        {state?.started_at ? (
          <div className="kv">
            <span className="kv-key">started</span>
            <span className="kv-value">{formatTimestamp(state.started_at)}</span>
          </div>
        ) : null}
        {state?.finished_at ? (
          <div className="kv">
            <span className="kv-key">finished</span>
            <span className="kv-value">{formatTimestamp(state.finished_at)}</span>
          </div>
        ) : null}
        {node.depends_on.length > 0 ? (
          <div className="kv kv-wide">
            <span className="kv-key">depends on</span>
            <span className="kv-value mono">{node.depends_on.join(', ')}</span>
          </div>
        ) : null}
      </section>

      {state?.error_message ? (
        <section className="task-error">
          <span className="task-error-class">{state.error_class ?? 'error'}</span>
          <span>{state.error_message}</span>
        </section>
      ) : null}

      <section className="detail-section">
        <h3 className="section-title">Acceptance criteria</h3>
        {node.criteria.length === 0 ? (
          <p className="muted small">
            This task declares no acceptance criteria, so there is nothing to verify.
          </p>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>criterion</th>
                <th>kind</th>
                <th>required</th>
                <th>verification state</th>
              </tr>
            </thead>
            <tbody>
              {node.criteria.map((criterion) => {
                const state_ = states[criterion.id] ?? 'unverified';
                return (
                  <tr key={criterion.id}>
                    <td>
                      <span className="mono small">{criterion.id}</span>
                      <div className="small">{criterion.description}</div>
                    </td>
                    <td className="small mono">{criterion.kind}</td>
                    <td className="small">{criterion.required ? 'yes' : 'no'}</td>
                    <td>
                      <span
                        className={`cstate cstate-${state_}`}
                        title={CRITERION_STATE_DESCRIPTIONS[state_]}
                      >
                        {CRITERION_STATE_LABELS[state_]}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>

      <section className="detail-section">
        <h3 className="section-title">Verification</h3>
        {!verification ? (
          <p className="muted small">
            No verification report has been recorded for this task yet.
          </p>
        ) : (
          <>
            <p className="small muted">
              Highest level reached: <strong>{verification.level}</strong>.{' '}
              {VERIFICATION_DESCRIPTIONS[verification.level]}
            </p>
            <ul className="inline-list small">
              <li>
                gate result:{' '}
                <strong className={verification.passed ? 'text-ok' : 'text-error'}>
                  {verification.passed ? 'passed' : 'not passed'}
                </strong>
              </li>
              <li>checks executed: {checks.length}</li>
              <li>unverified: {verification.unverified.length}</li>
              <li>advisory: {verification.advisory.length}</li>
              <li>failed: {verification.failed.length}</li>
              {verification.duration_s > 0 ? (
                <li>duration: {formatDuration(verification.duration_s)}</li>
              ) : null}
            </ul>
            {verification.error ? (
              <p className="small text-error">{verification.error}</p>
            ) : null}
          </>
        )}
      </section>

      {checks.length > 0 ? (
        <section className="detail-section">
          <h3 className="section-title">Check records</h3>
          <div className="check-list">
            {checks.map((check) => {
              const passed = check.exit_code === 0 && check.error === null;
              return (
                <article key={`${check.criterion_id}-${check.kind}-${check.started_at ?? ''}`} className="check">
                  <div className="check-head">
                    <span className={`cstate ${passed ? 'cstate-executed_passed' : 'cstate-executed_failed'}`}>
                      {passed ? 'passed' : 'failed'}
                    </span>
                    <span className="mono small">{check.criterion_id}</span>
                    <span className="mono small muted">{check.kind}</span>
                    {!check.deterministic ? (
                      <span className="cstate cstate-advisory">advisory</span>
                    ) : null}
                    {check.exit_code !== null ? (
                      <span className="mono small">exit {check.exit_code}</span>
                    ) : null}
                    {check.duration_s > 0 ? (
                      <span className="small muted">{formatDuration(check.duration_s)}</span>
                    ) : null}
                  </div>
                  {check.command.length > 0 ? (
                    <pre className="check-command mono">{check.command.join(' ')}</pre>
                  ) : null}
                  {check.error ? <p className="small text-error">{check.error}</p> : null}
                  {check.stdout_excerpt ? (
                    <pre className="check-output mono">{check.stdout_excerpt.trim()}</pre>
                  ) : null}
                </article>
              );
            })}
          </div>
        </section>
      ) : null}
    </div>
  );
}

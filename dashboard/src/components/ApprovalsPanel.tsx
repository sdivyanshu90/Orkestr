import { useState, type ReactElement } from 'react';
import type { Approval } from '../types';
import { formatTimestamp } from '../lib/format';

interface ApprovalsPanelProps {
  approvals: Approval[];
  onDecide: (approvalId: string, approved: boolean, decidedBy: string, reason: string) => void;
  pending: string | null;
}

export default function ApprovalsPanel({
  approvals,
  onDecide,
  pending,
}: ApprovalsPanelProps): ReactElement {
  const [decidedBy, setDecidedBy] = useState('api-user');
  const [reasons, setReasons] = useState<Record<string, string>>({});

  if (approvals.length === 0) {
    return (
      <div className="panel-empty">
        <p className="empty-title">No pending approvals</p>
        <p className="muted small">
          Nothing is waiting on a human decision for this run. Approvals appear when the
          policy engine routes a protected action to a person.
        </p>
      </div>
    );
  }

  return (
    <div className="approvals">
      <label className="field field-inline">
        <span className="field-label">decided by</span>
        <input
          type="text"
          value={decidedBy}
          onChange={(event) => setDecidedBy(event.target.value)}
        />
      </label>

      {approvals.map((approval) => {
        const busy = pending === approval.approval_id;
        return (
          <article key={approval.approval_id} className="approval">
            <header className="approval-head">
              <span className={`risk risk-${approval.risk}`}>{approval.risk} risk</span>
              <span className="mono approval-action">{approval.action}</span>
              <span className="mono small muted">{approval.approval_id}</span>
            </header>

            <p className="approval-rationale">{approval.rationale}</p>

            <div className="kv-grid">
              <div className="kv">
                <span className="kv-key">task</span>
                <span className="kv-value mono">{approval.task_id ?? '-'}</span>
              </div>
              <div className="kv">
                <span className="kv-key">run</span>
                <span className="kv-value mono">{approval.run_id}</span>
              </div>
              <div className="kv">
                <span className="kv-key">requested</span>
                <span className="kv-value">{formatTimestamp(approval.created_at)}</span>
              </div>
            </div>

            {approval.arguments.length > 0 ? (
              <div className="approval-args">
                <span className="field-label">arguments</span>
                <pre className="mono small">{approval.arguments.join('\n')}</pre>
              </div>
            ) : null}

            <label className="field">
              <span className="field-label">reason</span>
              <input
                type="text"
                value={reasons[approval.approval_id] ?? ''}
                placeholder="optional note recorded with the decision"
                onChange={(event) =>
                  setReasons((previous) => ({ ...previous, [approval.approval_id]: event.target.value }))
                }
              />
            </label>

            <div className="approval-actions">
              <button
                type="button"
                className="btn btn-approve"
                disabled={busy}
                onClick={() =>
                  onDecide(
                    approval.approval_id,
                    true,
                    decidedBy.trim() || 'api-user',
                    reasons[approval.approval_id] ?? '',
                  )
                }
              >
                {busy ? 'sending...' : 'Approve'}
              </button>
              <button
                type="button"
                className="btn btn-deny"
                disabled={busy}
                onClick={() =>
                  onDecide(
                    approval.approval_id,
                    false,
                    decidedBy.trim() || 'api-user',
                    reasons[approval.approval_id] ?? '',
                  )
                }
              >
                {busy ? 'sending...' : 'Deny'}
              </button>
            </div>
          </article>
        );
      })}
    </div>
  );
}

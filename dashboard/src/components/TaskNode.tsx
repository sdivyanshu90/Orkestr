import type { ReactElement } from 'react';
import { Handle, Position, type NodeProps } from 'reactflow';
import type { TaskNodeData } from '../types';
import { VERIFICATION_SHORT_LABELS } from '../lib/verification';
import { formatNumber } from '../lib/format';

/**
 * Custom DAG node.
 *
 * The node encodes a task at a glance: status colour on the left rail,
 * title, role, attempt count, token usage and the highest verification
 * level reached (if any evidence exists).
 */
export default function TaskNode({
  data,
  selected,
}: NodeProps<TaskNodeData>): ReactElement {
  const { status, title, role, attempts, tokens, level, criteriaCount, criteriaTotal } = data;

  return (
    <div className={`task-node task-node-${status}${selected ? ' is-selected' : ''}`}>
      <Handle type="target" position={Position.Left} className="task-handle" />
      <div className="task-node-rail" />
      <div className="task-node-body">
        <div className="task-node-top">
          <span className="task-node-status">{status.replace(/_/g, ' ')}</span>
          <span className="task-node-role">{role}</span>
        </div>
        <div className="task-node-title" title={title}>
          {title}
        </div>
        <div className="task-node-metrics">
          <span title="attempts">attempts {attempts}</span>
          <span title="tokens used">{formatNumber(tokens)} tok</span>
          {criteriaTotal > 0 ? (
            <span title="acceptance criteria">
              crit {criteriaCount}/{criteriaTotal}
            </span>
          ) : null}
        </div>
        {level ? (
          <div className="task-node-verification">
            <span className={`vlevel vlevel-${level}`}>
              <strong>{VERIFICATION_SHORT_LABELS[level]}</strong>
            </span>
          </div>
        ) : null}
      </div>
      <Handle type="source" position={Position.Right} className="task-handle" />
    </div>
  );
}

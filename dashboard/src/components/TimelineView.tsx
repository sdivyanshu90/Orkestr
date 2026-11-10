import { useMemo, type ReactElement } from 'react';
import type { GraphNode, RunEvent } from '../types';
import { buildGantt, positionOf, type GanttModel } from '../lib/gantt';
import { formatDuration, formatTick } from '../lib/format';

interface TimelineViewProps {
  events: RunEvent[];
  nodes: GraphNode[];
  selectedTaskId: string | null;
  onSelectTask: (taskId: string) => void;
}

const TICKS = 6;

function tickLabels(model: GanttModel): number[] {
  const span = model.max - model.min;
  const labels: number[] = [];
  for (let index = 0; index < TICKS; index += 1) {
    labels.push(model.min + (span * index) / (TICKS - 1));
  }
  return labels;
}

export default function TimelineView({
  events,
  nodes,
  selectedTaskId,
  onSelectTask,
}: TimelineViewProps): ReactElement {
  const taskIds = useMemo(() => nodes.map((node) => node.id), [nodes]);
  const model = useMemo(() => buildGantt(events, taskIds), [events, taskIds]);
  const labels = useMemo(() => (model.hasBars ? tickLabels(model) : []), [model]);
  const titles = useMemo(() => {
    const map = new Map<string, GraphNode>();
    nodes.forEach((node) => map.set(node.id, node));
    return map;
  }, [nodes]);

  if (nodes.length === 0) {
    return (
      <div className="panel-empty">
        <p className="empty-title">No tasks to lay out</p>
        <p className="muted small">This run has no task graph yet.</p>
      </div>
    );
  }

  if (!model.hasBars) {
    return (
      <div className="panel-empty">
        <p className="empty-title">No agent activity recorded</p>
        <p className="muted small">
          The timeline is derived from <code>agent.started</code> / <code>agent.finished</code>{' '}
          events. This run has not produced any yet.
        </p>
      </div>
    );
  }

  return (
    <div className="timeline">
      <div className="timeline-axis">
        <div className="timeline-axis-spacer" />
        <div className="timeline-track">
          {labels.map((stamp, index) => {
            const isFirst = index === 0;
            const isLast = index === TICKS - 1;
            const align = isFirst ? 'translateX(0)' : isLast ? 'translateX(-100%)' : 'translateX(-50%)';
            return (
              <span
                key={stamp}
                className="timeline-tick"
                style={{ left: `${(index / (TICKS - 1)) * 100}%`, transform: align }}
              >
                {formatTick(stamp, model.max - model.min)}
              </span>
            );
          })}
        </div>
      </div>

      {model.rows.map((row) => {
        const node = titles.get(row.taskId);
        const selected = row.taskId === selectedTaskId;
        return (
          <div
            key={row.taskId}
            className={`timeline-row${selected ? ' is-selected' : ''}`}
            onClick={() => onSelectTask(row.taskId)}
            role="button"
            tabIndex={0}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                onSelectTask(row.taskId);
              }
            }}
          >
            <div className="timeline-label">
              <span className="timeline-title" title={node?.title ?? row.taskId}>
                {node?.title ?? row.taskId}
              </span>
              <span className="timeline-meta muted small">
                {row.taskId}
                {node ? ` / ${node.status.replace(/_/g, ' ')}` : ''}
              </span>
            </div>
            <div className="timeline-track">
              {row.bars.map((bar) => {
                const left = positionOf(model, bar.start);
                const end = bar.end ?? bar.start;
                const right = positionOf(model, end);
                const width = Math.max(0.6, right - left);
                const duration =
                  bar.end === null ? null : Math.max(0, (bar.end - bar.start) / 1000);
                return (
                  <div
                    key={bar.key}
                    className={`gantt-bar gantt-${bar.outcome}`}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    title={`${row.taskId} attempt ${bar.attempt ?? '-'} - ${
                      duration === null ? 'in progress' : formatDuration(duration)
                    }`}
                  >
                    {bar.attempt !== null ? <span className="gantt-attempt">a{bar.attempt}</span> : null}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}

      <div className="timeline-legend">
        <span className="gantt-key">
          <i className="gantt-swatch gantt-ok" /> finished ok
        </span>
        <span className="gantt-key">
          <i className="gantt-swatch gantt-error" /> finished with error
        </span>
        <span className="gantt-key">
          <i className="gantt-swatch gantt-running" /> still running
        </span>
        <span className="gantt-key muted small">
          bars come from agent.started / agent.finished events
        </span>
      </div>
    </div>
  );
}

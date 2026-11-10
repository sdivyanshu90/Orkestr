/**
 * Derives a per-task timeline from the run's event log.
 *
 * A bar starts at an `agent.started` event and ends at the matching
 * `agent.finished` event for the same task and attempt.  Both events carry
 * `created_at`, which is the timestamp used for the bar.
 */

import type { RunEvent } from '../types';

export type GanttOutcome = 'ok' | 'error' | 'running';

export interface GanttBar {
  key: string;
  taskId: string;
  attempt: number | null;
  start: number;
  end: number | null;
  outcome: GanttOutcome;
}

export interface GanttRow {
  taskId: string;
  bars: GanttBar[];
}

export interface GanttModel {
  rows: GanttRow[];
  min: number;
  max: number;
  hasBars: boolean;
}

function parseTime(value: string): number | null {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed;
}

export function buildGantt(
  events: readonly RunEvent[],
  taskIds: readonly string[],
): GanttModel {
  const open = new Map<string, GanttBar>();
  const byTask = new Map<string, GanttBar[]>();

  const push = (bar: GanttBar): void => {
    const list = byTask.get(bar.taskId);
    if (list) list.push(bar);
    else byTask.set(bar.taskId, [bar]);
  };

  events.forEach((event) => {
    if (!event.task_id) return;
    if (event.type !== 'agent.started' && event.type !== 'agent.finished') return;

    const stamp = parseTime(event.created_at);
    if (stamp === null) return;

    const key = `${event.task_id}#${event.attempt ?? 0}`;

    if (event.type === 'agent.started') {
      const bar: GanttBar = {
        key,
        taskId: event.task_id,
        attempt: event.attempt,
        start: stamp,
        end: null,
        outcome: 'running',
      };
      open.set(key, bar);
      push(bar);
      return;
    }

    const existing = open.get(key);
    if (existing) {
      existing.end = stamp;
      const ok = event.payload['ok'];
      if (ok === true) existing.outcome = 'ok';
      else if (ok === false) existing.outcome = 'error';
      open.delete(key);
    }
  });

  const rows: GanttRow[] = taskIds.map((taskId) => ({
    taskId,
    bars: byTask.get(taskId) ?? [],
  }));

  // Include tasks that only appear in the event log (e.g. before a replan
  // produced the current spec list) so no execution is silently dropped.
  byTask.forEach((bars, taskId) => {
    if (!rows.some((row) => row.taskId === taskId)) {
      rows.push({ taskId, bars });
    }
  });

  let min = Number.POSITIVE_INFINITY;
  let max = Number.NEGATIVE_INFINITY;
  rows.forEach((row) => {
    row.bars.forEach((bar) => {
      if (bar.start < min) min = bar.start;
      const end = bar.end ?? bar.start;
      if (end > max) max = end;
    });
  });

  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    return { rows, min: 0, max: 0, hasBars: false };
  }
  if (min === max) {
    max = min + 1000;
  }

  return { rows, min, max, hasBars: true };
}

/** Percentage offset of a timestamp inside the timeline window. */
export function positionOf(model: GanttModel, stamp: number): number {
  const span = model.max - model.min;
  if (span <= 0) return 0;
  const ratio = (stamp - model.min) / span;
  return Math.min(100, Math.max(0, ratio * 100));
}

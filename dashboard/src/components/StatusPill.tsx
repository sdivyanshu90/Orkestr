import type { ReactElement } from 'react';
import type { RunStatus, TaskStatus } from '../types';

export type Tone = 'ok' | 'error' | 'warn' | 'info' | 'neutral';

const TASK_TONES: Record<TaskStatus, Tone> = {
  pending: 'neutral',
  ready: 'info',
  running: 'info',
  retry_scheduled: 'warn',
  awaiting_approval: 'warn',
  succeeded: 'ok',
  failed: 'error',
  timed_out: 'error',
  blocked: 'error',
  cancelled: 'neutral',
};

const RUN_TONES: Record<RunStatus, Tone> = {
  created: 'neutral',
  planning: 'info',
  running: 'info',
  awaiting_approval: 'warn',
  paused: 'warn',
  succeeded: 'ok',
  failed: 'error',
  cancelled: 'neutral',
  completed_with_failures: 'warn',
};

export function taskTone(status: TaskStatus): Tone {
  return TASK_TONES[status] ?? 'neutral';
}

export function runTone(status: RunStatus): Tone {
  return RUN_TONES[status] ?? 'neutral';
}

interface StatusPillProps {
  value: string;
  tone: Tone;
  title?: string;
}

export default function StatusPill({ value, tone, title }: StatusPillProps): ReactElement {
  return (
    <span className={`pill pill-${tone}`} title={title ?? value}>
      {value.replace(/_/g, ' ')}
    </span>
  );
}

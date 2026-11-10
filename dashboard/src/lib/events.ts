/**
 * Severity classification for the event log.
 *
 * The mapping is derived from the event types the orchestrator actually
 * emits (`agent_squad.store.events.EventType`) plus the payload of status
 * transitions, which carry the target status.
 */

import type { RunEvent } from '../types';

export type EventSeverity = 'error' | 'warn' | 'ok' | 'info';

const ERROR_TYPES = new Set<string>([
  'run.failed',
  'verification.failed',
  'integration.conflict',
  'policy.denied',
  'tool.denied',
  'model.error',
  'task.blocked',
]);

const WARN_TYPES = new Set<string>([
  'recovery.started',
  'integration.attempted',
  'run.budget_exhausted',
  'task.retry_scheduled',
  'control.cancellation_requested',
  'run.cancelled',
]);

const OK_TYPES = new Set<string>([
  'run.completed',
  'verification.passed',
  'integration.verified',
  'artifact.published',
  'patch.captured',
  'checkpoint.created',
  'recovery.completed',
]);

const FAILED_TARGETS = new Set<string>(['failed', 'timed_out', 'blocked']);
const WARN_TARGETS = new Set<string>(['awaiting_approval', 'paused', 'retry_scheduled']);
const OK_TARGETS = new Set<string>(['succeeded', 'cancelled']);

function payloadString(event: RunEvent, key: string): string | null {
  const value = event.payload[key];
  return typeof value === 'string' ? value : null;
}

export function eventSeverity(event: RunEvent): EventSeverity {
  if (event.type === 'task.status_changed' || event.type === 'run.status_changed') {
    const to = payloadString(event, 'to');
    if (to && FAILED_TARGETS.has(to)) return 'error';
    if (to && WARN_TARGETS.has(to)) return 'warn';
    if (to && OK_TARGETS.has(to)) return 'ok';
  }
  if (event.type === 'agent.finished') {
    const ok = event.payload['ok'];
    if (ok === false) return 'error';
    if (ok === true) return 'ok';
  }
  if (event.type === 'tool.invoked') {
    const ok = event.payload['ok'];
    if (ok === false) return 'error';
  }
  if (ERROR_TYPES.has(event.type)) return 'error';
  if (WARN_TYPES.has(event.type)) return 'warn';
  if (OK_TYPES.has(event.type)) return 'ok';
  return 'info';
}

/** One-line summary of an event payload for the dense log table. */
export function summarizePayload(event: RunEvent): string {
  const payload = event.payload;
  const parts: string[] = [];
  Object.entries(payload).forEach(([key, value]) => {
    if (value === null || value === undefined) return;
    if (typeof value === 'object') return;
    parts.push(`${key}=${String(value)}`);
  });
  if (parts.length === 0) return '';
  return parts.join(' ');
}

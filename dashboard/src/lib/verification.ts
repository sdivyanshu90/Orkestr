/**
 * The verification ladder, exactly as defined by
 * `agent_squad.models.run.VerificationLevel`.
 *
 * The levels are strictly ordered and must never be conflated.  In
 * particular `checks_passed` means "the specified checks ran and produced
 * these results" -- it never means "the change is correct".
 */

import type { CriterionState, VerificationLevel } from '../types';

export const VERIFICATION_LEVELS: readonly VerificationLevel[] = [
  'none',
  'worker_reported',
  'patch_produced',
  'validation_executed',
  'checks_passed',
  'integration_validated',
  'human_approved',
];

export const VERIFICATION_DESCRIPTIONS: Readonly<Record<VerificationLevel, string>> = {
  none: 'No evidence has been recorded for this task yet.',
  worker_reported: 'The worker said it was done. This is a claim, not a result.',
  patch_produced: 'A diff exists and its content hash is known.',
  validation_executed: 'The verification gate ran the specified acceptance checks.',
  checks_passed:
    'The specified checks ran and produced these results. This is NOT a statement that the change is correct.',
  integration_validated: 'The combined set of patches was re-verified together.',
  human_approved: 'A person signed off on a protected action.',
};

/** Short label used in dense badges. */
export const VERIFICATION_SHORT_LABELS: Readonly<Record<VerificationLevel, string>> = {
  none: 'none',
  worker_reported: 'worker reported',
  patch_produced: 'patch produced',
  validation_executed: 'validation executed',
  checks_passed: 'checks passed',
  integration_validated: 'integration validated',
  human_approved: 'human approved',
};

/** Rank of a level in the ladder; higher means further along the chain. */
export function levelRank(level: VerificationLevel | null | undefined): number {
  if (!level) return -1;
  const index = VERIFICATION_LEVELS.indexOf(level);
  return index < 0 ? -1 : index;
}

/** True when `level` is a level the backend can actually emit. */
export function isVerificationLevel(value: string): value is VerificationLevel {
  return (VERIFICATION_LEVELS as readonly string[]).includes(value);
}

export const CRITERION_STATE_LABELS: Readonly<Record<CriterionState, string>> = {
  unverified: 'unverified',
  advisory: 'advisory',
  executed_failed: 'executed, failed',
  executed_passed: 'executed, passed',
};

export const CRITERION_STATE_DESCRIPTIONS: Readonly<Record<CriterionState, string>> = {
  unverified: 'No check was executed for this criterion.',
  advisory: 'Only a model judgement exists (reviewer opinion or manual note).',
  executed_failed: 'A deterministic check ran and failed.',
  executed_passed: 'A deterministic check ran and passed.',
};

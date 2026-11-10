import type { ReactElement } from 'react';
import type { VerificationLevel } from '../types';
import {
  VERIFICATION_DESCRIPTIONS,
  VERIFICATION_LEVELS,
  VERIFICATION_SHORT_LABELS,
  levelRank,
} from '../lib/verification';

interface VerificationBadgeProps {
  level: VerificationLevel | null;
  /** When true the badge is prefixed with "highest level reached". */
  labelled?: boolean;
}

/**
 * Renders the highest verification level reached for a unit of work.
 *
 * The wording is deliberately explicit: `checks_passed` means the specified
 * checks ran and produced these results, not that the change is correct.
 */
export default function VerificationBadge({
  level,
  labelled = true,
}: VerificationBadgeProps): ReactElement {
  const effective = level ?? 'none';
  const rank = levelRank(effective);
  const rankText = rank >= 0 ? `level ${rank + 1}/${VERIFICATION_LEVELS.length}` : 'unknown';

  return (
    <span
      className={`vlevel vlevel-${effective}`}
      title={`${VERIFICATION_SHORT_LABELS[effective]} (${rankText}) - ${VERIFICATION_DESCRIPTIONS[effective]}`}
    >
      {labelled ? 'highest level reached: ' : ''}
      <strong>{VERIFICATION_SHORT_LABELS[effective]}</strong>
    </span>
  );
}

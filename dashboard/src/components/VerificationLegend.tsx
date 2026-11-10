import type { ReactElement } from 'react';
import {
  VERIFICATION_DESCRIPTIONS,
  VERIFICATION_LEVELS,
  VERIFICATION_SHORT_LABELS,
} from '../lib/verification';

/**
 * The verification ladder, from "no evidence" to "a person signed off".
 *
 * This legend exists because the levels are easy to over-read.  In
 * particular `checks_passed` is called out explicitly: it means the
 * specified checks ran and produced these results, NOT that the change is
 * correct.
 */
export default function VerificationLegend(): ReactElement {
  return (
    <div className="legend">
      <div className="legend-head">
        <span className="legend-title">Verification levels</span>
        <span className="legend-sub">highest level reached, strictly ordered</span>
      </div>
      <ol className="legend-list">
        {VERIFICATION_LEVELS.map((level, index) => (
          <li key={level} className="legend-item">
            <span className={`vlevel vlevel-${level}`}>
              <strong>{VERIFICATION_SHORT_LABELS[level]}</strong>
            </span>
            <span className="legend-rank">{index + 1}</span>
            <span className="legend-desc">{VERIFICATION_DESCRIPTIONS[level]}</span>
          </li>
        ))}
      </ol>
      <p className="legend-note">
        <strong>checks_passed</strong> means: the specified checks ran and produced these
        results. It does <em>not</em> mean the change is correct.
      </p>
    </div>
  );
}

import type { ReactElement } from 'react';
import type { Artifact } from '../types';
import { formatBytes, formatTimestamp, shortHash } from '../lib/format';

interface ArtifactsPanelProps {
  artifacts: Artifact[];
}

export default function ArtifactsPanel({ artifacts }: ArtifactsPanelProps): ReactElement {
  if (artifacts.length === 0) {
    return (
      <div className="panel-empty">
        <p className="empty-title">No artifacts</p>
        <p className="muted small">
          Nothing has been published for this run. Artifacts appear once a worker produces a
          patch, log, report or other evidence.
        </p>
      </div>
    );
  }

  return (
    <div className="artifact-list">
      {artifacts.map((artifact) => (
        <article key={artifact.id} className="artifact">
          <header className="artifact-head">
            <span className={`kind kind-${artifact.kind}`}>{artifact.kind}</span>
            <span className="mono small artifact-id" title={artifact.id}>
              {artifact.id}
            </span>
            <span className="small muted">{formatBytes(artifact.size)}</span>
            <span className="small muted mono">{artifact.media_type}</span>
          </header>

          <div className="artifact-uri mono small" title={artifact.uri}>
            {artifact.uri}
          </div>

          <div className="kv-grid">
            <div className="kv">
              <span className="kv-key">sha256</span>
              <span className="kv-value mono" title={artifact.sha256}>
                {shortHash(artifact.sha256)}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">producer</span>
              <span className="kv-value">{artifact.provenance.producer}</span>
            </div>
            <div className="kv">
              <span className="kv-key">task</span>
              <span className="kv-value mono">{artifact.provenance.task_id ?? '-'}</span>
            </div>
            <div className="kv">
              <span className="kv-key">attempt</span>
              <span className="kv-value">{artifact.provenance.attempt}</span>
            </div>
            <div className="kv">
              <span className="kv-key">base revision</span>
              <span className="kv-value mono">
                {artifact.provenance.base_revision ?? '-'}
              </span>
            </div>
            <div className="kv">
              <span className="kv-key">created</span>
              <span className="kv-value">{formatTimestamp(artifact.provenance.created_at)}</span>
            </div>
            {artifact.provenance.model ? (
              <div className="kv">
                <span className="kv-key">model</span>
                <span className="kv-value mono">{artifact.provenance.model}</span>
              </div>
            ) : null}
            {artifact.provenance.provider ? (
              <div className="kv">
                <span className="kv-key">provider</span>
                <span className="kv-value mono">{artifact.provenance.provider}</span>
              </div>
            ) : null}
          </div>

          {Object.keys(artifact.metadata).length > 0 ? (
            <details className="artifact-meta">
              <summary className="small">metadata</summary>
              <pre className="mono small">{JSON.stringify(artifact.metadata, null, 2)}</pre>
            </details>
          ) : null}
        </article>
      ))}
    </div>
  );
}

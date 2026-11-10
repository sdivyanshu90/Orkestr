import type { ReactElement } from 'react';
import type { MessageRecord } from '../types';
import { formatTimestamp } from '../lib/format';

interface MessagesPanelProps {
  messages: MessageRecord[];
  loading: boolean;
}

export default function MessagesPanel({ messages, loading }: MessagesPanelProps): ReactElement {
  if (loading && messages.length === 0) {
    return <p className="muted small">loading messages...</p>;
  }

  if (messages.length === 0) {
    return (
      <div className="panel-empty">
        <p className="empty-title">No messages</p>
        <p className="muted small">
          No inter-agent messages have been recorded for this run.
        </p>
      </div>
    );
  }

  return (
    <div className="message-list">
      {messages.map((message) => (
        <article key={message.id} className="message">
          <header className="message-head">
            <span className="kind">{message.kind}</span>
            <span className="mono small">
              {message.sender} to {message.recipient}
            </span>
            {message.task_id ? <span className="mono small muted">{message.task_id}</span> : null}
            <span className="small muted">{formatTimestamp(message.created_at)}</span>
          </header>
          {message.note ? <p className="message-note small">{message.note}</p> : null}
          {Object.keys(message.payload).length > 0 ? (
            <details className="artifact-meta">
              <summary className="small">payload</summary>
              <pre className="mono small">{JSON.stringify(message.payload, null, 2)}</pre>
            </details>
          ) : null}
        </article>
      ))}
    </div>
  );
}

import { useMemo, useState, type ReactElement } from 'react';
import type { RunEvent } from '../types';
import { eventSeverity, summarizePayload } from '../lib/events';
import { formatTimestamp, truncate } from '../lib/format';

interface EventLogProps {
  events: RunEvent[];
  lastSeq: number;
  polling: boolean;
}

export default function EventLog({ events, lastSeq, polling }: EventLogProps): ReactElement {
  const [typeFilter, setTypeFilter] = useState<string>('all');
  const [query, setQuery] = useState('');

  const types = useMemo(() => {
    const unique = new Set<string>();
    events.forEach((event) => unique.add(event.type));
    return Array.from(unique).sort();
  }, [events]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return events.filter((event) => {
      if (typeFilter !== 'all' && event.type !== typeFilter) return false;
      if (!needle) return true;
      if (event.task_id && event.task_id.toLowerCase().includes(needle)) return true;
      if (event.actor && event.actor.toLowerCase().includes(needle)) return true;
      return event.type.toLowerCase().includes(needle);
    });
  }, [events, typeFilter, query]);

  return (
    <div className="event-log">
      <div className="event-toolbar">
        <label className="field field-inline">
          <span className="field-label">type</span>
          <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>
            <option value="all">all ({events.length})</option>
            {types.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
        <label className="field field-inline">
          <span className="field-label">filter</span>
          <input
            type="text"
            value={query}
            placeholder="task id, actor or event type"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <span className="muted small event-count">
          {filtered.length} shown / {events.length} loaded / last_seq {lastSeq}
          {polling ? ' / tailing' : ' / idle'}
        </span>
      </div>

      {events.length === 0 ? (
        <div className="panel-empty">
          <p className="empty-title">No events yet</p>
          <p className="muted small">The event log for this run is empty.</p>
        </div>
      ) : (
        <div className="event-scroll">
          <table className="data-table event-table">
            <thead>
              <tr>
                <th>seq</th>
                <th>time</th>
                <th>type</th>
                <th>task</th>
                <th>att</th>
                <th>actor</th>
                <th>payload</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((event) => {
                const severity = eventSeverity(event);
                return (
                  <tr key={event.seq} className={`event-row event-${severity}`}>
                    <td className="mono small num">{event.seq}</td>
                    <td className="small nowrap">{formatTimestamp(event.created_at)}</td>
                    <td className="mono small event-type">{event.type}</td>
                    <td className="mono small truncate-cell" title={event.task_id ?? ''}>
                      {event.task_id ?? '-'}
                    </td>
                    <td className="mono small num">{event.attempt ?? '-'}</td>
                    <td className="small truncate-cell" title={event.actor ?? ''}>
                      {event.actor ?? '-'}
                    </td>
                    <td className="small muted truncate-cell" title={summarizePayload(event)}>
                      {truncate(summarizePayload(event), 90)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

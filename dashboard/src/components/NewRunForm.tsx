import { useState, type FormEvent, type ReactElement } from 'react';
import { createRun } from '../api';
import type { StrategyName } from '../types';

const STRATEGIES: { value: StrategyName | 'auto'; label: string }[] = [
  { value: 'auto', label: 'auto (let the selector decide)' },
  { value: 'single', label: 'single' },
  { value: 'static_two', label: 'static_two' },
  { value: 'static_four', label: 'static_four' },
  { value: 'adaptive', label: 'adaptive' },
  { value: 'dag', label: 'dag' },
];

interface NewRunFormProps {
  onCreated: (runId: string) => void;
  onError: (message: string) => void;
}

export default function NewRunForm({ onCreated, onError }: NewRunFormProps): ReactElement {
  const [objective, setObjective] = useState('');
  const [description, setDescription] = useState('');
  const [repoPath, setRepoPath] = useState('');
  const [strategy, setStrategy] = useState<StrategyName | 'auto'>('auto');
  const [maxWorkers, setMaxWorkers] = useState(4);
  const [provider, setProvider] = useState('mock');
  const [model, setModel] = useState('mock-deterministic-v1');
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      const response = await createRun({
        objective: objective.trim(),
        description: description.trim(),
        repo_path: repoPath.trim(),
        strategy: strategy === 'auto' ? null : strategy,
        max_workers: Number.isFinite(maxWorkers) && maxWorkers > 0 ? maxWorkers : null,
        provider: provider.trim() || 'mock',
        model: model.trim() || 'mock-deterministic-v1',
      });
      setObjective('');
      setDescription('');
      onCreated(response.run_id);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="new-run" onSubmit={(event) => void submit(event)}>
      <div className="new-run-head">
        <span className="section-title">New run</span>
      </div>

      <label className="field">
        <span className="field-label">objective</span>
        <input
          type="text"
          value={objective}
          placeholder="e.g. add retry handling to the HTTP client"
          onChange={(event) => setObjective(event.target.value)}
          required
        />
      </label>

      <label className="field">
        <span className="field-label">description</span>
        <textarea
          value={description}
          rows={2}
          placeholder="optional context for the planner"
          onChange={(event) => setDescription(event.target.value)}
        />
      </label>

      <label className="field">
        <span className="field-label">repo path</span>
        <input
          type="text"
          value={repoPath}
          placeholder="/absolute/path/to/repository"
          onChange={(event) => setRepoPath(event.target.value)}
          required
        />
      </label>

      <div className="field-row">
        <label className="field">
          <span className="field-label">strategy</span>
          <select
            value={strategy}
            onChange={(event) => setStrategy(event.target.value as StrategyName | 'auto')}
          >
            {STRATEGIES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <label className="field field-narrow">
          <span className="field-label">max workers</span>
          <input
            type="number"
            min={1}
            max={64}
            value={maxWorkers}
            onChange={(event) => setMaxWorkers(Number(event.target.value))}
          />
        </label>
      </div>

      <details className="advanced">
        <summary>provider</summary>
        <div className="field-row">
          <label className="field">
            <span className="field-label">provider</span>
            <input type="text" value={provider} onChange={(event) => setProvider(event.target.value)} />
          </label>
          <label className="field">
            <span className="field-label">model</span>
            <input type="text" value={model} onChange={(event) => setModel(event.target.value)} />
          </label>
        </div>
      </details>

      <button type="submit" className="btn btn-primary" disabled={submitting}>
        {submitting ? 'starting...' : 'Start run'}
      </button>
    </form>
  );
}

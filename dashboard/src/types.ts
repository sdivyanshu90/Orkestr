/**
 * Response types for the agent-squad FastAPI service.
 *
 * Every shape here mirrors what `src/agent_squad/api/server.py` actually
 * returns.  Where the backend stores free-form JSON (`payload`, `metadata`,
 * `config`) the value is typed as `Record<string, unknown>` so nothing has
 * to be cast at the call site.
 */

/* ------------------------------------------------------------------ enums */

/** `RunStatus` in `agent_squad.models.run`. */
export type RunStatus =
  | 'created'
  | 'planning'
  | 'running'
  | 'awaiting_approval'
  | 'paused'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'completed_with_failures';

/** `TaskStatus` in `agent_squad.models.task`. */
export type TaskStatus =
  | 'pending'
  | 'ready'
  | 'running'
  | 'retry_scheduled'
  | 'awaiting_approval'
  | 'succeeded'
  | 'failed'
  | 'timed_out'
  | 'blocked'
  | 'cancelled';

/** `VerificationLevel` in `agent_squad.models.run`. */
export type VerificationLevel =
  | 'none'
  | 'worker_reported'
  | 'patch_produced'
  | 'validation_executed'
  | 'checks_passed'
  | 'integration_validated'
  | 'human_approved';

/** `CriterionState` in `agent_squad.models.evidence`. */
export type CriterionState =
  | 'unverified'
  | 'advisory'
  | 'executed_failed'
  | 'executed_passed';

/** `ArtifactKind` in `agent_squad.models.artifact`. */
export type ArtifactKind =
  | 'patch'
  | 'file'
  | 'log'
  | 'test_report'
  | 'review'
  | 'report'
  | 'evidence'
  | 'context'
  | 'manifest'
  | 'bundle';

/** `Role` in `agent_squad.models.task`. */
export type TaskRole =
  | 'supervisor'
  | 'implementer'
  | 'test_engineer'
  | 'reviewer'
  | 'integration';

/** `StrategyName` in `agent_squad.models.run`. */
export type StrategyName =
  | 'single'
  | 'static_two'
  | 'static_four'
  | 'adaptive'
  | 'dag';

/* ------------------------------------------------------------------- runs */

export interface RunSummary {
  run_id: string;
  status: RunStatus;
  strategy: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunsResponse {
  runs: RunSummary[];
}

export interface RunRecord {
  run_id: string;
  status: RunStatus;
  strategy: string | null;
  created_at: string;
  updated_at: string;
  config: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

/* ------------------------------------------------------------ verification */

export interface CheckRecord {
  criterion_id: string;
  kind: string;
  command: string[];
  exit_code: number | null;
  stdout_excerpt: string;
  stderr_excerpt: string;
  duration_s: number;
  started_at: string | null;
  finished_at: string | null;
  log_artifact_id: string | null;
  deterministic: boolean;
  error: string | null;
}

/** The verification report stored on a task's state. */
export interface VerificationReport {
  task_id: string;
  passed: boolean;
  level: VerificationLevel;
  checks: CheckRecord[];
  criterion_states: Record<string, CriterionState>;
  unverified: string[];
  advisory: string[];
  failed: string[];
  error: string | null;
  duration_s: number;
  workspace: string | null;
}

/* ------------------------------------------------------------------ tasks */

export interface TaskState {
  id: string;
  run_id: string;
  status: TaskStatus;
  owner: string | null;
  attempt: number;
  max_attempts: number;
  error_class: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  deadline_at: string | null;
  tokens_used: number;
  cost_used_usd: number;
  artifacts: string[];
  verification: VerificationReport | null;
  updated_at: string;
  lease_expires_at: string | null;
}

export interface AcceptanceCriterionSummary {
  id: string;
  description: string;
  kind: string;
  required: boolean;
}

export interface GraphNode {
  id: string;
  title: string;
  role: string;
  status: TaskStatus;
  depends_on: string[];
  attempts: number;
  tokens_used: number;
  cost_usd: number;
  verification: VerificationReport | null;
  criteria: AcceptanceCriterionSummary[];
}

export interface GraphEdge {
  source: string;
  target: string;
}

export interface GraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface Checkpoint {
  checkpoint_id: string;
  run_id: string;
  seq: number;
  label: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface RunDetailResponse {
  run: RunRecord;
  tasks: TaskState[];
  pending_approvals: Approval[];
  checkpoints: Checkpoint[];
}

/* ----------------------------------------------------------------- events */

export interface RunEvent {
  seq: number;
  event_id: string;
  run_id: string;
  task_id: string | null;
  attempt: number | null;
  type: string;
  actor: string | null;
  payload: Record<string, unknown>;
  created_at: string;
  recorded_at: number;
}

export interface EventsResponse {
  events: RunEvent[];
  last_seq: number;
}

/* -------------------------------------------------------------- artifacts */

export interface ArtifactProvenance {
  producer: string;
  run_id: string;
  task_id: string | null;
  attempt: number;
  created_at: string;
  model: string | null;
  provider: string | null;
  base_revision: string | null;
  parent_artifact_ids: string[];
  tool_calls: string[];
}

export interface Artifact {
  id: string;
  kind: ArtifactKind;
  uri: string;
  sha256: string;
  size: number;
  media_type: string;
  provenance: ArtifactProvenance;
  metadata: Record<string, unknown>;
}

export interface ArtifactsResponse {
  artifacts: Artifact[];
}

/* --------------------------------------------------------------- messages */

export interface MessageRecord {
  id: string;
  kind: string;
  sender: string;
  recipient: string;
  task_id: string | null;
  correlation_id: string | null;
  payload: Record<string, unknown>;
  note: string;
  created_at: string;
}

export interface MessagesResponse {
  messages: MessageRecord[];
}

/* -------------------------------------------------------------- approvals */

export interface Approval {
  approval_id: string;
  run_id: string;
  task_id: string | null;
  action: string;
  rationale: string;
  arguments: string[];
  risk: string;
  status: string;
  created_at: string;
}

export interface ApprovalsResponse {
  approvals: Approval[];
}

export interface ApprovalDecisionResponse {
  approval_id: string;
  run_id: string;
  action: string;
  status: string;
  decided_by: string;
  reason: string;
}

/* ----------------------------------------------------------- write bodies */

export interface CreateRunRequest {
  objective: string;
  description?: string;
  repo_path: string;
  spec?: Record<string, unknown> | null;
  max_workers?: number | null;
  strategy?: string | null;
  provider?: string;
  model?: string;
}

export interface CreateRunResponse {
  run_id: string;
  status: RunStatus;
}

export interface CancelRunRequest {
  reason: string;
}

export interface CancelRunResponse {
  run_id: string;
  status: RunStatus;
}

export interface ApprovalDecisionRequest {
  approved: boolean;
  decided_by: string;
  reason: string;
}

export interface HealthResponse {
  status: string;
}

/* ------------------------------------------------------------ react flow */

/** Payload carried by the custom DAG node renderer. */
export interface TaskNodeData extends Record<string, unknown> {
  taskId: string;
  title: string;
  role: string;
  status: TaskStatus;
  attempts: number;
  tokens: number;
  cost: number;
  level: VerificationLevel | null;
  criteriaCount: number;
  criteriaTotal: number;
  dependsOn: string[];
  errorClass: string | null;
}

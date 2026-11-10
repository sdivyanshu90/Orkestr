/**
 * Typed fetch wrappers for every agent-squad API endpoint.
 *
 * The wrappers are intentionally thin: they translate transport failures into
 * a single `ApiError` type so the UI can distinguish "the backend is not
 * running" from "the backend rejected this request".
 */

import type {
  ApprovalDecisionRequest,
  ApprovalDecisionResponse,
  ApprovalsResponse,
  ArtifactsResponse,
  CancelRunRequest,
  CancelRunResponse,
  CreateRunRequest,
  CreateRunResponse,
  EventsResponse,
  GraphResponse,
  HealthResponse,
  MessagesResponse,
  RunDetailResponse,
  RunsResponse,
} from './types';

export class ApiError extends Error {
  /** HTTP status, or `null` when the request never reached the backend. */
  readonly status: number | null;
  /** True when the backend could not be reached at all. */
  readonly unreachable: boolean;
  /** Human readable detail extracted from the response body, if any. */
  readonly detail: string;

  constructor(
    message: string,
    options: { status?: number | null; unreachable?: boolean; detail?: string } = {},
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = options.status ?? null;
    this.unreachable = options.unreachable ?? false;
    this.detail = options.detail ?? message;
  }
}

function describeBody(body: unknown): string | null {
  if (body === null || body === undefined) return null;
  if (typeof body === 'string') return body;
  if (typeof body !== 'object') return String(body);
  const record = body as Record<string, unknown>;
  const detail = record['detail'];
  if (typeof detail === 'string') return detail;
  if (detail !== undefined) return JSON.stringify(detail);
  const message = record['message'];
  if (typeof message === 'string') return message;
  return null;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (init.body !== undefined && init.body !== null) {
    headers['Content-Type'] = 'application/json';
  }
  if (init.headers !== undefined) {
    Object.entries(init.headers).forEach(([key, value]) => {
      if (typeof value === 'string') headers[key] = value;
    });
  }

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch (cause) {
    const detail = cause instanceof Error ? cause.message : String(cause);
    throw new ApiError('backend unreachable', { unreachable: true, detail });
  }

  const raw = await response.text();
  let parsed: unknown = null;
  if (raw.length > 0) {
    try {
      parsed = JSON.parse(raw) as unknown;
    } catch {
      parsed = raw;
    }
  }

  if (!response.ok) {
    const detail = describeBody(parsed) ?? response.statusText ?? 'request failed';
    throw new ApiError(`${response.status} ${detail}`, {
      status: response.status,
      detail,
    });
  }

  return parsed as T;
}

function encode(value: string): string {
  return encodeURIComponent(value);
}

/* ------------------------------------------------------------- endpoints */

/** `GET /healthz` */
export function health(): Promise<HealthResponse> {
  return request<HealthResponse>('/healthz');
}

/** `GET /api/runs` */
export function listRuns(): Promise<RunsResponse> {
  return request<RunsResponse>('/api/runs');
}

/** `POST /api/runs` */
export function createRun(body: CreateRunRequest): Promise<CreateRunResponse> {
  return request<CreateRunResponse>('/api/runs', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/** `GET /api/runs/{run_id}` */
export function getRun(runId: string): Promise<RunDetailResponse> {
  return request<RunDetailResponse>(`/api/runs/${encode(runId)}`);
}

/** `GET /api/runs/{run_id}/events` */
export function getEvents(
  runId: string,
  afterSeq = 0,
  limit = 500,
): Promise<EventsResponse> {
  const query = new URLSearchParams({
    after_seq: String(afterSeq),
    limit: String(limit),
  });
  return request<EventsResponse>(`/api/runs/${encode(runId)}/events?${query.toString()}`);
}

/** `GET /api/runs/{run_id}/graph` */
export function getGraph(runId: string): Promise<GraphResponse> {
  return request<GraphResponse>(`/api/runs/${encode(runId)}/graph`);
}

/** `GET /api/runs/{run_id}/artifacts` */
export function getArtifacts(runId: string): Promise<ArtifactsResponse> {
  return request<ArtifactsResponse>(`/api/runs/${encode(runId)}/artifacts`);
}

/** `GET /api/runs/{run_id}/messages` */
export function getMessages(runId: string): Promise<MessagesResponse> {
  return request<MessagesResponse>(`/api/runs/${encode(runId)}/messages`);
}

/** `GET /api/approvals?run_id=` */
export function listApprovals(runId?: string): Promise<ApprovalsResponse> {
  const query = runId ? `?run_id=${encode(runId)}` : '';
  return request<ApprovalsResponse>(`/api/approvals${query}`);
}

/** `POST /api/approvals/{approval_id}` */
export function decideApproval(
  approvalId: string,
  body: ApprovalDecisionRequest,
): Promise<ApprovalDecisionResponse> {
  return request<ApprovalDecisionResponse>(`/api/approvals/${encode(approvalId)}`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

/** `POST /api/runs/{run_id}/cancel` */
export function cancelRun(runId: string, body: CancelRunRequest): Promise<CancelRunResponse> {
  return request<CancelRunResponse>(`/api/runs/${encode(runId)}/cancel`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

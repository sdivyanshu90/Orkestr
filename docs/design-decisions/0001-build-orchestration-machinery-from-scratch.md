# 0001. Build the orchestration machinery from scratch

- Status: Accepted
- Date: 2026-11-10

## Context

The system needs a task planner, a dependency-aware concurrent scheduler, a
per-attempt isolation mechanism, an evidence-producing verification boundary,
a durable event log, and a capability/policy layer. Every mainstream
multi-agent framework (LangGraph, CrewAI, AutoGen, Google's ADK, the OpenAI
Agents SDK) provides some subset of this, but each makes the *same* set of
architectural choices about the parts this project cares about most.

The specific requirements that pull against every one of those frameworks:

1. **The worker must not be trusted to decide it is finished.** Acceptance
   criteria must be re-executed by the orchestrator, in the worker's actual
   workspace, against the worker's actual output. A framework whose unit of
   success is "the agent said it was done" cannot express this without being
   extended past its own abstraction.
2. **The task lifecycle must be an explicit, checked state machine.** A
   transition from `RUNNING` to `SUCCEEDED` has to be rejected if it is
   illegal, and a write from a superseded attempt has to be rejected as
   stale. This is a data-model requirement, not a workflow-graph requirement.
3. **Every decision must be replayable and auditable.** The system is
   intended to support experiments on parallelisation strategies, which
   requires that the record of *why* a decision was taken outlives the
   process that took it.
4. **The isolation mechanism is git worktrees, and the control plane must
   know it.** Base-revision selection per task, dependency-ordered patch
   application, and tree-level staleness detection are all git-specific.
5. **The dependency budget is three packages.** `pydantic`, `httpx` and
   `rich`. A framework would add a graph runtime, an HTTP client, a config
   system, a tokenizer, a telemetry stack and a set of transitive
   dependencies, most of which would be used for a fraction of their
   surface.

What the frameworks genuinely do well — LLM call plumbing, tool schemas,
streaming, provider-specific adapters, prompt templating — is either already
covered by the provider abstraction in `providers/base.py` (a four-message
vocabulary and a `ToolCall` shape) or is explicitly out of scope for a
runtime whose research question is about *coordination and verification*, not
about prompt engineering.

The project is also a research vehicle: the metrics module
(`observe/metrics.py`) defines *verified completion rate*, *tokens per
verified task*, *coordination overhead*, and *integration conflicts* as
first-class quantities. Producing those numbers honestly requires owning
the control plane rather than inferring it from a framework's telemetry.

## Decision

Implement the orchestration machinery in-tree, with no agent-framework
dependency. The runtime dependencies are `pydantic>=2.6`, `httpx>=0.27` and
`rich>=13.0`; OpenTelemetry is optional and only used for trace export.

The runtime is decomposed so that each concern has exactly one owner:

| Concern | Owner |
|---|---|
| Composition and run lifecycle | `orchestrator.Orchestrator` |
| Task structure and validation | `dag.graph.TaskGraph`, `models/task.py` |
| What runs next | `scheduler.engine.Scheduler` |
| One attempt of one task | `agents.runner.WorkerRunner` |
| The bounded model/tool loop | `agents.base.AgentLoop` |
| Independent verification | `verification.gate.VerificationGate` |
| Durable state | `store.events.EventLog`, `store.state.StateStore` |
| Authority | `security.capabilities`, `security.policy` |

The provider abstraction is deliberately kept small — `ChatMessage`,
`ToolCall`, `ToolSpec`, `Usage`, `ModelResponse` — so that adding a vendor
means writing one adapter, not learning a framework's message model. Two
adapters with genuinely different wire protocols exist
(`providers/openai_compat.py`, `providers/anthropic.py`) precisely so the
abstraction cannot accidentally become shaped around one vendor's quirks.

The framework's functionality is re-implemented, not copied: the equivalents
of "graph node" and "edge" are `TaskSpec` and `depends_on`; the equivalent
of "checkpoint" is `EventType.CHECKPOINT_CREATED`; the equivalent of a
"shared memory" object is the append-only event log plus its projections.

## Alternatives considered

- **LangGraph** — its central abstraction is a state graph with
  checkpointed nodes, which is a good match for the task DAG. But its
  checkpoint mechanism is a library-owned persistence layer, whereas this
  system needs the *event log itself* to be the source of truth, with
  projections rebuildable from it and with per-task attempt fencing
  layered on top. Reproducing that inside LangGraph would mean owning the
  persistence layer anyway, while inheriting the framework's node/graph
  model. Its state is also a mutable channel dictionary rather than a
  transition-checked state machine, which is precisely the property this
  project needs to enforce.
- **CrewAI** — organised around agents with fixed roles and sequential or
  hierarchical task delegation. The role model does not map onto the
  requirements: the reviewer here is read-only *by construction* (it never
  receives the write tool), which is a tool-registry property rather than a
  prompt or role-description property. CrewAI's execution model also does not
  express the dependency-ordered patch integration this system needs.
- **AutoGen** — a conversational multi-agent framework. The agents here
  communicate through a typed, validated message bus with version fencing,
  not through free-form conversation turns; a large fraction of AutoGen's
  value (conversation patterns, group chat managers) is orthogonal to the
  verification and evidence requirements.
- **Google ADK** — provides workflow primitives (sequential, parallel,
  loop) and session state. Its workflow models assume the steps are
  well-behaved functions; the steps here are non-deterministic model agents
  whose outputs must be independently checked, and whose failures must be
  classified into retryable and non-retryable classes before a policy is
  applied.
- **OpenAI Agents SDK** — an attractive fit for the tool-calling loop and
  the handoff model, but it makes the OpenAI Responses API the centre of the
  design. This runtime already targets a provider-neutral interface with two
  materially different wire protocols behind it, and the mock provider must
  be able to substitute for a real endpoint in offline runs. A
  vendor-centred abstraction would make the deterministic mock path a
  second-class citizen, which would defeat the purpose of the mock.
- **Celery / RQ / a task queue** — considered for the scheduler role
  specifically, and rejected as part of the broader decision (see [0003](0003-append-only-event-log-sqlite-wal.md),
  which covers the durability model). The scheduling problem here is
  dependency-gated, bounded-concurrency and single-host; a broker adds
  operational surface without removing any constraint that actually binds.
- **Writing the framework but also depending on one for the model loop**
  (for example, using an agent SDK purely for its tool-calling turn
  management) — rejected because the agent loop is where the budget
  enforcement, the untrusted-content fencing (`wrap_untrusted` /
  `redact_secrets`) and the per-role tool registry live. Those are the
  properties this system is actually about, so the loop has to be owned.

## Consequences

**What this buys:**

- **The verification boundary is structural, not bolted on.** Because the
  scheduler, the state store and the verifier are all in-tree, "the worker
  cannot mark its own task complete" is enforced by construction:
  `Scheduler._execute` calls `Scheduler._verify` after a successful worker
  result and the transition to `SUCCEEDED` happens only after that report
  passes.
- **The lifecycle is checkable.** `LEGAL_TRANSITIONS` and `assert_transition`
  are ordinary data and code, so the legal state space can be reasoned
  about, tested exhaustively, and rendered as a diagram.
- **The durable record is ours.** The event log's contents are dictated by
  the research question (verified completion rate, coordination overhead,
  integration conflicts) rather than by a framework's telemetry schema.
- **The mock provider is a first-class provider.** `providers/mock.py`
  implements the same `ModelProvider` protocol as the HTTP adapters, so
  offline end-to-end runs are a supported path, not a test hack.
- **No transitive dependency risk.** Three direct dependencies, all of them
  small and stable.

**What this costs:**

- **We own the parts we would have gotten for free.** Streaming responses,
  token counting, prompt templating, retries at the HTTP layer beyond the
  status-code classification in `openai_compat.py`, and conversation
  management all have to be written or deliberately omitted.
- **The framework ecosystem's integrations are unavailable.** Anything
  published as a LangGraph node, a CrewAI tool or an ADK tool has to be
  reimplemented against `tools/base.py`'s `Tool` protocol.
- **More code to maintain in-tree.** The scheduler, the store and the
  security layer are all ours, including their bugs.
- **The trade is not obviously correct for everyone.** A project that needed
  chat-style multi-agent collaboration and nothing else would be better
  served by AutoGen or the OpenAI Agents SDK. The from-scratch choice is
  justified by the specific requirements above, not by a general claim that
  frameworks are worse.
- **Risk of reinventing a wheel.** The scheduler implements only what this
  system needs: a dependency-gated ready queue with a bounded worker pool,
  per-task deadlines, a retry policy and cancellation propagation. It is not
  a distributed executor and does not attempt to be.

# Orkestr documentation

[Repository overview](../README.md) · [Current status](../PROJECT_STATUS.md)

This documentation describes the implementation at the documentation baseline
`f8404f0`, with historical experiments identified separately. Source and CLI help
remain authoritative for executable interfaces. A documented mechanism is not
an independently qualified guarantee.

## Choose a reading path

| Reader | Recommended order |
|---|---|
| New user | [Project guide](project-guide.md) → [getting started](getting-started.md) → [concepts](concepts.md) |
| Operator | [Configuration](configuration.md) → [operations](operations.md) → [reference](reference.md) → [reliability](reliability.md) |
| Contributor | [Architecture](architecture.md) → [implementation walkthrough](implementation-walkthrough.md) → [deep dive](deep-dive.md) → [contributing](contributing.md) |
| Security reviewer | [Security model](security-threat-model.md) → [limitations](limitations.md) → [reliability](reliability.md) → [scoring policy](scoring-criteria.md) |
| Evaluator | [Evaluation guide](evaluation.md) → [reproduction](reproduction.md) → measured reports → [assessment record](assessment-record.yaml) |

## Guides and references

- [Project guide](project-guide.md): the problem, the execution model, a worked
  example, design tradeoffs, and what the project has actually demonstrated.
- [Getting started](getting-started.md): an offline example with a temporary
  repository, explicit paths and interpretation of the JSON result.
- [Concepts](concepts.md): contracts, task DAGs, attempts, worktrees, evidence,
  approvals, replay and qualification.
- [Configuration](configuration.md): effective configuration, provider protocols,
  budgets, deadline behavior, containment and optional service dependencies.
- [Operations](operations.md): inspect, approve, cancel, recover, retain and clean
  runs without confusing recorded state with external side effects.
- [Reference](reference.md): CLI flags, local API routes, configuration ownership
  and on-disk artifacts.
- [Architecture](architecture.md), [deep dive](deep-dive.md), and
  [implementation walkthrough](implementation-walkthrough.md): complementary
  views of components, algorithms and concrete source paths.
- [Reliability](reliability.md), [security model](security-threat-model.md),
  [limitations](limitations.md), and [failure analysis](failure-analysis.md):
  failure behavior, authority, residual risk and lessons from observed defects.
- [Contributing](contributing.md): development checks, evidence discipline and
  how to change a trust boundary.

## Experiments: read the scope before the numbers

| Material | Meaning |
|---|---|
| [Benchmark methodology](benchmark-methodology.md) / [results](benchmark-results.md) | Deterministic orchestration fixtures and links to later measurements |
| [Local workflow methodology](local-benchmark-methodology.md) / [results](local-benchmark-results.md) | Historical real-model workflow compatibility under constrained local serving |
| [Direct inference results](local-inference-results.md) | Five tiny fixed tasks per model/device configuration; historical timing caveat |
| [Evaluation guide](evaluation.md) | What differs across these experiments, the generated corpus and later clean timing sample |
| [Reproduction](reproduction.md) | Commands for reproducing evidence on a supported, clean environment |

Raw results live in [benchmarks/results](../benchmarks/results/). Do not silently
mix runs, re-label worker successes as independently accepted solutions, or
replace published records with new runs made under different conditions.

## Plans, assessment and history

- [Roadmap](roadmap.md) defines the work and dependency order.
- [Strict scoring criteria](scoring-criteria.md) defines the formal pass/fail
  requirements. The policy is not relaxed by documentation or test-count growth.
- [Assessment decisions](assessment-decisions.json) and
  [assessment record](assessment-record.yaml) state what is missing, inconclusive
  or independently qualified for their pinned release. They are not a live badge
  that automatically follows HEAD.
- [Improvement audit](improvement-audit.md) tracks implementation and evidence
  gaps. [Design decisions](design-decisions/README.md) preserve the rationale for
  major architectural choices.

## Documentation conventions

“Implemented” means an execution path exists in source. “Tested” must identify
which tests and environment. “Measured” refers to a particular experiment.
“Qualified” requires the scoring policy's independent, release-bound evidence.
A model name containing “distill” is not equivalent to the teacher/vendor model.
A command example with `YOUR_MODEL` or `/absolute/path` requires substitution.

The project name is **Orkestr**, the CLI/distribution is **agent-squad**, and the
Python package is **agent_squad**. This distinction matters when installing,
importing modules, inspecting source and interpreting old design documents.

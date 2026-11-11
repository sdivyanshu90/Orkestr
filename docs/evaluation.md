# Evidence and evaluation guide

[Documentation home](README.md) · [Reproduction](reproduction.md) · [Scoring](scoring-criteria.md)

## Separate the experiments

| Experiment | Inputs / oracle | What it can establish | What it cannot establish |
|---|---|---|---|
| Deterministic orchestration benchmark | Known mock tool behavior and synthetic tasks | Scheduling, integration and accounting behavior for those fixtures | Model reasoning or real-repository coding reliability |
| Historical local workflow sweep | Small GGUF models, synthetic requests, then-current runtime gates | Compatibility with a particular agent/tool protocol and budget | Independent coding accuracy or clean comparative inference speed |
| Fixed direct probes | Five tiny prompts with fixed scoring per model/device | Bounded instruction/protocol behavior and recorded response timing | General model ranking or repository repair performance |
| Generated contract corpus | Constructed base/candidate trees and expected verdicts | Regression coverage of declared verifier mechanisms | Independent real-world failure probabilities without sampling review |
| Later clean timing sample | Three models, ownership-checked serving, repeated prompts | Evidence that the ownership/teardown measurement path works | The full held-out qualification study |
| Formal qualification | Release-bound independent evidence across all mandatory criteria | The precise scope qualified by the scoring policy | Universal correctness or a live badge for unrelated commits |

## Historical results must remain historical

The [deterministic results](benchmark-results.md) contain the original 96-run
mock experiment. The [local workflow report](local-benchmark-results.md) retains
62 completed trials and two cancellations. Its worker-gate outcomes predate the
newer request contract semantics. Eleven of sixteen recorded passes had no worker
tool calls; these are not sixteen independently established code solutions.

The [direct probes](local-inference-results.md) contain 40 GPU-configuration
requests and five CPU requests. Kimi/MiniMax labels identify third-party
Qwen-based distillations. The original timing sample retained an idle residual
server until cleanup, so its timings are observations under that resource state.
Do not turn the GPU/CPU ratio into a clean hardware claim.

The later [clean timing artifact](../benchmarks/results/local-clean-timing.json)
records three model configurations, two repetitions and verified ownership.
Its own purpose statement limits the claim. It demonstrates a mechanism rather
than qualifying all models, all workloads or coordination benefits.

## Interpret the verifier corpus carefully

The [published corpus artifact](../benchmarks/results/corpus-20261111.json)
records 600 adversarial cases, 160 known-correct cases, expected/actual outcomes,
family information and confidence calculations. Its reported zero mismatches
are useful author-produced regression evidence.

A numeric bound depends on its independence/sampling assumptions. Different
PRNG streams, filenames and isolated directories do not eliminate shared
mechanism blind spots. Read the category-level and family-level bounds, excluded
cases and design argument. Do not silently present the case-level interval as
an independently validated failure rate on unfamiliar repositories.

The default corpus test uses a stratified sample. A full corpus run is explicit:

```bash
AGENT_SQUAD_CORPUS_FULL=1 python -m pytest tests/evaluation/test_corpus.py -q
```

That command can write a report. Preserve the published snapshot before running
an experiment that writes to its default artifact location, or use a clean
checkout/output procedure. A default fast test pass is not a rerun of all 760
published cases.

## A reproducible measurement protocol

Record the target commit, task/repository IDs, owner criteria, verifier identity,
model revision/hash, template, seed, output/context caps, provider concurrency,
worker count, hardware, effective offload, cache condition and dependency lock.
Record request queues, model load, prompt, decode, tools, checks and teardown
separately. State whether each usage value is reported, estimated or unavailable.

Before timing, verify process ownership and a known resource baseline. After
teardown, verify owned PIDs and listening ports are gone. A requested layer
count or a successful server startup is insufficient evidence of actual fit.
Keep invalid/contaminated samples and their reasons, but do not use them to
qualify clean latency claims. Raw text/JSON is evidence; a hand-written table is
only a presentation of it.

Compare policies on matched tasks/models and total budgets. Predeclare subsets,
order randomization, cancellation handling and failure treatment. Report
uncertainty at the repository/task level, not by treating repeated seeds as
independent repositories. Deadline exhaustion remains a failed budgeted attempt;
user-aborted work is censored and requires explicit bounds/handling.

## Formal score versus engineering assessment

The strict policy has 20 binary criteria. All 18 mandatory criteria must PASS
for 9.5. A conversational engineering assessment is not that qualification.
The [decision input](assessment-decisions.json) and
[generated assessment](assessment-record.yaml) preserve missing and inconclusive
states. An author saying “complete” describes implementation status, not an
independently reviewed PASS.

The current checked-in record identifies its own older release commit. Review
it against the exact candidate being discussed; rerun evidence affected by
changes. A validator recomputing zero awarded points is not a statement that the
code has zero engineering value. It is a statement about qualifying evidence.

## Remaining real-repository proof

The policy requires a preregistered held-out corpus across at least six real
repositories, independent acceptance oracles, multiple configurations/trials,
a strong single-agent baseline, paired quality/speed/cost evidence and external
review/use. These are separate from the synthetic tests already in the tree.

Do not change confidence thresholds or discard difficult cases after observing
results. The [roadmap](roadmap.md) organizes the work; the
[scoring policy](scoring-criteria.md) defines the actual qualification rules.

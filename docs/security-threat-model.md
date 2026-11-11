# Security and authority model

[Documentation home](README.md) · [Configuration](configuration.md) · [Limitations](limitations.md)

This is the threat model for a local, single-user repository-agent runtime.
It describes implemented controls, assumptions and unqualified risks. It is not
an independent penetration-test report or permission to expose the API publicly.

## Assets, actors and trust boundaries

| Asset / boundary | Why it matters |
|---|---|
| Owner request and acceptance criteria | A planner must not redefine a failing task into a passing request |
| Host filesystem, credentials and Git configuration | Worker code can otherwise inherit the user's authority |
| Evaluator material and expected outputs | A worker controlling its own oracle can manufacture acceptance |
| Run database, artifacts and approvals | These determine state, evidence and authorization |
| Provider credentials and payloads | Host-side model calls can expose data independently of worker networking |
| Child processes and downloaded models | Orphans and broad cleanup can consume resources or damage unrelated work |
| API/dashboard actions | A locally trusted interface is not a multi-user identity boundary |

Repository content, model responses, tool outputs and generated code are untrusted
inputs. The host user and configured operator are trusted within the declared
local deployment. A privileged host user can edit the database or code; content
hashes are not protection against that actor.

## Development versus hardened execution

**Development** mode uses policy checks, a private run HOME, environment handling,
worktrees and resource limits. It does not promise strong host filesystem
containment. A private HOME is useful, but arbitrary code can still try absolute
host paths unless an actual boundary denies them.

**Hardened** mode requires an available supported containment backend. The
bubblewrap path constructs user/network/PID-related namespaces, a restricted
filesystem image, controlled writable binds, private HOME/caches and denied
network access. Its probe observes boundary behavior and the profile refuses
unsupported promises. A binary being installed is not sufficient if the host
disallows its namespace operations.

Read the actual bind list and process command path for the supported environment.
A writable worktree, exposed Git metadata, system image or extra bind can widen
authority. The project does not claim a reviewed guarantee against kernel,
container-runtime or all namespace escapes. Independent qualification remains
required by S1–S3 in the [scoring policy](scoring-criteria.md).

## Tool policy and arbitrary command execution

Tools consult capabilities, scope and protected-action rules. File operations and
supported command patterns can be refused before execution. Command mediation
also neutralizes relevant Git hook/config authority on covered paths.

A parser or allow-list cannot generally prove the behavior of arbitrary shell or
Python code. A script can compute a path or effect that a syntactic scanner does
not recognize. Treat command scanning as a control to test, not a replacement
for OS containment. Development mode must not be described as safe for arbitrary
hostile repositories because an example command was refused.

Git hooks, aliases, external diff drivers, credential helpers, environment
variables and package-manager configuration can execute code. Review actual
Git invocations, local/global configuration priority and dependency-install paths.
Turning off one hook setting is not a general Git security proof.

## Approval is exact authority, not general consent

The broker records protected-action requests and returns grants bound to action,
arguments, workspace and request context, with expiry semantics. A changed plan,
command, path or contract must not silently reuse authorization for a different
effect. Test mutation, replay, expiry and cross-run reuse through real tools.

The local API's `decided_by` label is not authenticated identity. Anyone who has
local access to the writable data/control surface may have operator authority.
Public exposure and multiple untrusted users require a separate authentication,
authorization, tenancy and audit design.

## Verification is another trust boundary

Owner criteria are captured before planning and preserved by semantic identity.
The final gate uses complete check results, not exit code alone. Worker-written
tests and summaries do not automatically become independent acceptance evidence.

Protected-material paths, task write restrictions, command controls and final
hash comparisons cover different cases. A hash checked after execution cannot
prove that a worker never read an evaluator, temporarily modified it and restored
it, or influenced dependencies/configuration outside the hashed subject. Hidden
oracles need separately owned storage and authority, not only a filename in a
prompt. Review every worker execution path and verifier environment.

Candidate and contract identities invalidate stale evidence when inputs change.
They do not prove that the original criterion is complete or correct. A malicious
or simply weak owner specification remains a weak oracle.

## Provider and data exposure

Model adapters execute in the orchestrator process. They may contact a configured
remote endpoint even while worker commands have network disabled. A provider
base URL therefore belongs in the data/credential threat model. Keep keys in the
configured environment mechanism, inspect effective configuration, and do not
commit credentials to request files or examples.

Recorded prompts, exchanges, patches, paths and command output can contain
proprietary data. Redaction is a useful signal/control, not proof that every
secret format is removed. Apply storage permissions, retention and review before
publishing benchmark artifacts or exposing run-data directories.

## Ownership and cleanup authority

Durable leases/fences protect concurrent run mutation; registries identify owned
processes and artifacts. Construction, reconciliation, takeover and shutdown
must be reviewed together. Do not infer safe concurrent startup from an ownership
helper's unit test. Cleanup must preserve pre-existing processes/models and
verify that a recorded identity was not reused by another process.

External side effects remain outside a local exactly-once state guarantee.
Approval and reconciliation must handle ambiguity rather than repeat effects
blindly after a crash.

## Security evidence and reporting

The repository has authority, containment, protected-material and lifecycle tests.
Some require host capabilities that may be unavailable. An expected refusal is
useful evidence of fail-closed behavior; a skipped successful-containment test is
not evidence that a host can run hardened workloads.

Record environment, backend, exact command path, expected denied access and
observed result. Test host canaries, network endpoints, protected evaluators,
Git configuration, process descendants and seeded secret formats. An independent
review must reproduce critical cases and resolve high/critical findings before
formal qualification. See [reproduction](reproduction.md) and the release-bound
[assessment record](assessment-record.yaml); no current documentation page is
that independent audit.

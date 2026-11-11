# Contributing and maintaining evidence

[Documentation home](README.md) · [Walkthrough](implementation-walkthrough.md) · [Scoring](scoring-criteria.md)

## Development setup

Use the [getting-started](getting-started.md) environment and the checked-in
hash lock for release reproduction. For ordinary editable development,
`python -m pip install -e '.[dev]'` installs the declared tools. Optional API
and dashboard dependencies require their own setup; there is no `api` extra.

```bash
python -m ruff check src tests scripts
python -m mypy
python scripts/check_docs.py
python -m pytest
```

Mypy roots are declared in `pyproject.toml` and include the orchestrator and
related critical paths. A zero-error result describes that configuration, not
a proof that every behavior is correct. Pytest uses importlib mode because test
modules in different directories share names. If you override `addopts`, keep
`--import-mode=importlib` or collection can fail for duplicate module names.

The full suite includes slow lifecycle and deadline tests. For a bounded initial
check, select relevant files or `python -m pytest -m 'not slow'`. Report that
selection explicitly. A timed-out, skipped or filtered run is not a full-suite
pass. Collecting tests discovers cases; it does not execute them.

## Change the runtime with a counterexample

For a correctness or authority change, identify the failing scenario before
editing the implementation. Add the narrow regression and an integration test
through the real run entry point. A helper can be correct while its callers
never use it. Trace both normal completion and exceptional teardown.

The most important seams are owner request → planner, model response → tool,
worker result → verification, patches → final candidate, process → durable
registry, owner lease → state write, and recorded exchange → replay. Tests should
exercise those boundaries rather than mirror implementation statements.

Classify compatibility impact: configuration schema, task/contract semantics,
SQLite migrations, artifact schema, provider payload, CLI/API JSON or dashboard
expectations. Add migration/recovery evidence before changing persisted state.
Avoid silently changing a result's meaning while keeping the same field name.

## Documentation and experiments

Keep operational instructions synchronized with executable help and package
metadata. Never advertise an optional extra that is not declared, a CLI flag
that is not parsed, or a dashboard field that is not actually rendered.

Historical measured data is immutable evidence. Generate a new artifact for a
new candidate/configuration; do not overwrite an old experiment and preserve its
headline as if nothing changed. State which metrics include planning, retries,
missing usage, cancellation and model/server load. Raw logs may contain captured
whitespace or escaped tokens; do not normalize them to satisfy style checks.

A generated corpus is not a real-repository benchmark. Random strings and
separate workspaces do not, by themselves, prove independence of failure modes.
Show family-level results and let independent review challenge the sampling
assumptions. Timing requires resource and process-ownership evidence.

## Assessment discipline

`docs/scoring-criteria.md` is the normative policy. The assessment record is
pinned to its declared release and does not automatically follow HEAD. Changing
code or evidence can invalidate recorded hashes. Use the read-only validator:

```bash
python scripts/build_assessment.py --check
```

Without `--check`, the tool writes an assessment record; that is a publication
step, not a substitute for independent review. Never set PASS merely because a
feature was implemented or tests increased. Preserve missing/inconclusive
statuses and the evidence needed to resolve them. Documentation edits do not
independently qualify the implementation they describe.

## Review checklist

- Reproduce the concrete trigger and explain the resulting behavior.
- Verify the final candidate and the original contract, not only task status.
- Exercise cancellation, timeout, provider error and cleanup for affected paths.
- Keep secrets, unowned processes and user model caches outside test authority.
- Record the exact checks, their environment and any incomplete verification.
- Update the documentation page that owns the changed concept; link to it rather
  than duplicating a conflicting explanation in multiple places.

See [reproduction](reproduction.md) for the release procedure and
[roadmap](roadmap.md) for priority. Public deployment and distributed/multi-user
features need separate authority and security evidence before they are claimed.

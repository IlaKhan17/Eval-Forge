# Parity fixtures

`(example results, gate set) → verdict` cases, evaluated **twice** — once by the library the CLI
calls, once by the API through its own aggregation and gate path — and asserted equal.

This is the fixture set behind the claim the whole product rests on: **the exit code your CI acts on
and the verdict your dashboard shows are the same verdict.** They are computed in different
processes, from different data representations (in-memory objects vs rows rehydrated from Postgres),
by code reached through different call stacks. Nothing but a test keeps them identical, and the way
they drift is not a wrong answer — it is one side quietly gaining a special case.

`apps/api/tests/test_parity.py` runs them. Each case is one JSON file:

```jsonc
{
  "name": "hidden-slice-regression",
  "why": "Why this case exists — the failure mode it would catch.",
  "gate_set": { "name": "...", "rules": [ ... ] },
  "candidate": [ { "example_id": "e1", "scores": [ { "metric": "accuracy", "value": 1.0 } ] } ],
  "baseline":  [ ... ]        // optional
}
```

`candidate` and `baseline` are lists of `ExampleResult` (`proofstep_types.results`), so a case can
exercise errored scores, slices, and non-scalar payloads — not just final numbers. That matters:
aggregation is where the interesting divergences live, because "exclude errored scores from the mean
but count them" is a rule that exists in two places the moment anyone reimplements it in SQL.

## Adding a case

Add a file. The suite discovers `*.json` in this directory, and there is a test asserting the
directory is non-empty — an empty fixture set that silently passes would be worse than no suite.

Prefer cases where the two sides *could* disagree: errors, empty slices, absent baselines, missing
metrics, dataset mismatch. A case where every rule trivially passes proves the plumbing works and
nothing else.

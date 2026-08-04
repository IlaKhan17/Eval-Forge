# Online evaluation, review, and promotion

Offline evaluation answers *"is this change worse than main?"*. Online evaluation answers
*"is what we shipped actually behaving?"* — and then hands the failures to a human who can
turn one into a regression test.

That last step is the point. A failure nobody converts into a test is a failure that
recurs, and an evaluation platform that only produces charts is a dashboard.

## The loop

```
trace ingested
  → online rule evaluates it            (trajectory: every trace, free)
  → failure lands in a review queue     (with the reason it failed)
  → reviewer claims it                  (SKIP LOCKED, leased)
  → reviewer annotates the correction   (ground truth, never model-written)
  → promote into a draft dataset        (never a locked one)
  → next CI run covers the case
```

```bash
make dev          # postgres, redis, minio
make bootstrap    # migrations, project, API key
make api          # the API
make worker       # the background jobs
make online-eval  # or: process one batch now, without waiting for the cadence
```

## Cost is what shapes the design

Offline runs are bounded by the dataset. Online runs are bounded by traffic, which is to
say unbounded. Three rules follow from that.

**Deterministic checks run on every trace.** Trajectory policies, schema checks, and secret
scans are free and instant. Sampling them would save nothing and lose coverage of exactly
the safety properties most worth having everywhere. A test asserts a deterministic batch
costs exactly zero, because if that ever stops being true, the whole claim stops being
affordable.

**Sampling is deterministic in the trace id, not random.** `random()` means a re-run
evaluates a different subset, so replaying a backlog spends money again and no two runs are
comparable. Hashing the trace id makes membership a *property of the trace*: always in or
always out, replays free, and the decision recomputable later to answer "why was this one
skipped?".

Raising the rate only ever *adds* traces — monotonicity falls out of comparing against a
fixed bucket, and it matters operationally: turning the rate up must not drop traces that
were already being evaluated.

**Each rule samples independently.** Two rules at 1 % must not select the *same* 1 %. With a
shared salt they would, which sounds harmless and is not: 99 % of traffic would be invisible
to every judge in the project, and the sampled cohort would be fixed forever. Set
`sample_group` to opt into shared sampling — the right choice when several judges must score
the same traces so their scores are comparable per trace.

**Failures escalate past the sample, under a cap.** A failed trace is worth more than a
random one, so it is evaluated even when the sample missed it. Uncapped that is a cost bomb:
an incident produces an error spike, which produces a judge-call spike, and the surprise
bill arrives on the day you can least afford the distraction. `max_escalations_per_batch`
bounds it, and escalation is checked *after* sampling so budget is only spent on traces the
sample actually missed.

## Every decision is recorded, including the skips

A skipped trace gets a row with its reason. Without that, "this trace has no score" is
ambiguous between *not sampled*, *escalation budget exhausted*, *the rule errored*, and *the
worker never got here* — and those have four different responses.

```bash
GET /v1/online-rules/{id}/coverage
{"by_decision": {"deterministic": 4}, "unprocessed": 3}
```

`unprocessed` is the number that makes this auditable. "97 % of traces were not sampled" and
"97 % of traces were never processed" produce the same pass rate, and only one of them means
the worker is behind.

## Five verdicts, not two

| Verdict | Meaning |
|---|---|
| `pass` | checked, compliant |
| `fail` | checked, violated — **queued for review** |
| `inconclusive` | the trace could not answer the question |
| `error` | the evaluation itself broke |
| `skipped` | not evaluated; `decision_reason` says why |

**`inconclusive` is the one worth explaining.** A trace whose spans were dropped cannot
support a claim about what did *not* happen, so a `required_action` rule over it is unknown.
Calling that a violation would fill the review queue with "your exporter dropped spans"
items until people stopped reading it; calling it a pass would hide a coverage gap behind a
green number.

The complement is equally deliberate: a `forbidden_before` rule **still fires** on an
incomplete trace. A send with no approval among the spans that *were* recorded is a real
violation — the missing spans cannot un-send the email. Both halves are pinned by tests.

**`error` is never a failing trace.** A malformed policy or a provider outage would otherwise
present as a quality regression and queue innocent traces for human attention.

## Idempotency

`(project_id, trace_id, rule_id)` is unique, and writes use `ON CONFLICT DO NOTHING`. A
worker that dies mid-batch and restarts must not double-count, because an online metric that
drifts upward on every replay is worse than no metric.

The pending-work query is a `NOT EXISTS` against recorded decisions, **not** a timestamp
high-water mark. Ingestion is not ordered by `started_at` — a client can upload hours late —
so a cursor would skip every late arrival permanently. A test pins exactly that case.

Two rules failing on one trace queue **one** review item, not two.

## Review queues

Claiming uses `SELECT … FOR UPDATE SKIP LOCKED`. The alternatives fail in specific ways: a
plain `SELECT` then `UPDATE` hands two reviewers the same item, and `FOR UPDATE` without
`SKIP LOCKED` makes the second reviewer wait on a row lock — which looks like the UI
hanging. Tested with genuinely concurrent transactions, including more reviewers than items.

Order is priority first, then **oldest** — so a backlog drains instead of growing a tail
nobody ever reaches.

**Claims carry a lease.** A reviewer who claims an item and closes their laptop must not hold
it forever. Expired leases are reclaimed opportunistically by the next `claim_next`, so an
abandoned item is available immediately rather than waiting for a sweeper; a periodic job
covers queues nobody is currently working.

`GET /v1/review-queues/health` reports depth **and the age of the oldest pending item**. Age
matters more: 500 items raised this morning is a busy day, while 5 items where the oldest is
three weeks old means nobody is reading the queue — and a review queue nobody reads has
stopped being a control.

## Annotations are ground truth

Never written by a model. This is the table judge calibration is measured against, and
labelling it with an LLM makes the exercise circular.

An annotation must say *something* — a label, rating, comment, correction, or preference —
enforced by a check constraint. "I looked and had no opinion" must not enter the
ground-truth table, where it would count as a label.

`annotate` is its own API-key scope, separate from `write`. The authority to create ground
truth is not the authority to register an evaluator or run an experiment: a CI key should
not be able to inject labels, and an annotation tool's key should not be able to rewrite
gates. Machine credentials leave `annotator_id` null rather than inventing a user, because
the ground-truth table must not claim a person made a judgement they never made.

## Promotion

`POST /v1/datasets/promote-from-trace` turns a production failure into a dataset example.
Three rules make it safe.

**It targets a draft, never a locked version.** A locked version's content hash is what lets
an experiment prove it saw identical data; appending to one would silently invalidate every
historical comparison against it. A draft is created if none exists.

**The expected result comes from a human** — passed directly, or taken from an annotation's
`correction`. Promoting with the model's own output as the expected answer would enshrine
the defect as the specification, which is the failure mode that makes a golden dataset
actively harmful. Promotion without one is refused with a 422 that says so.

**It is idempotent and records provenance.** Promoting twice returns `already_present`
rather than creating a duplicate, because duplicates skew every metric computed over the
dataset. `source_trace_id` survives into the example, so one that later looks wrong can be
traced back to the interaction that produced it.

## Retention

Two mechanisms for two reasons.

**Whole partitions are dropped** for traces and spans. `DROP TABLE` on a partition is O(1)
and reclaims disk immediately. A `DELETE` over the same rows writes a tombstone per row,
bloats the table, and leaves the space to be recovered by a `VACUUM FULL` that takes an
exclusive lock — which is how a retention job becomes an outage.

**A partition is dropped only when its entire range has expired.** Comparing the partition's
*start* against the cutoff instead would drop the current month on the first day of
retention and delete data the project asked to keep. The boundary rounds in the safe
direction, and the arithmetic is unit-tested including the December rollover and the
`DEFAULT` partition, whose rows have no known range and so are never dropped.

Partition drops use the **longest** retention window across all projects, because a partition
holds every project's traces for that month. Dropping one on behalf of the shortest window
would destroy another tenant's retainable data.

**Payloads have a shorter window than traces**, on purpose: the prompt bodies are the
sensitive part, while the span skeleton stays useful for latency and cost analysis long
after the text should be gone.

## The worker

Four jobs, all in `evalforge_api.worker`, each callable directly with a session so an
operator or a test can run one without Redis:

| Job | Cadence | Why |
|---|---|---|
| `online_eval` | every minute | a violation should reach a queue while the incident is live |
| `release_leases` | every 5 min | covers queues nobody is working |
| `rollup` | every 15 min | summarises verdicts per rule |
| `retention` | 03:17 daily | holds DDL locks; keep it off peak and off the hour |

`max_jobs = 1`: these are database-bound, and running four concurrently mostly produces lock
contention with ingestion rather than throughput. Each job gets its own session, so one job's
failure cannot roll back another's work. Failures are logged and **re-raised**, because arq's
retry handling is the right owner of a failed job and swallowing the exception would make a
permanently broken job look like a healthy one that does nothing.

The rollup reports `skipped` alongside `pass` and `fail`, and a pass rate of `None` — not
`0.0` — when nothing was evaluated. A zero over zero measurements looks like a total
collapse on a dashboard.

## Not built yet

Named so the absence is a decision:

- **Judge and deterministic online rules.** The sampling, budgeting, escalation, and
  recording all work and are tested; `_evaluate` raises `NotImplementedError` for those two
  kinds rather than silently recording a pass. They need the evaluator registry and a
  provider client in the worker.
- **A review UI.** The API is complete; the dashboard has no queue view (see
  [DASHBOARD.md](DASHBOARD.md) for what else is deferred there).
- **Stored rollups.** Computed on read for now, because a stored rollup needs an
  invalidation story and getting that wrong produces confidently stale numbers. The returned
  shape is what the materialised table will hold.
- **Orphaned payload-object deletion.** The count is reported; the objects are not removed,
  because the object store is the authority on what exists and deleting the rows first would
  lose the only record of what to delete there.
- **Annotation of spans and experiment results.** The schema supports all four target types;
  only `trace` has a promotion path.

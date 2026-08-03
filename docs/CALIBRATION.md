# Judge calibration

An LLM judge is a measuring instrument. An uncalibrated instrument produces numbers, not
measurements — and gating a merge on one means blocking engineers on a figure nobody has
checked.

Calibration is the check: run the judge over examples a human has labelled, and measure
how well it agrees.

## The short version

```bash
# What it would do, and what it would cost. No model calls.
evalforge calibrate evals/suites/reply-tone.yaml -e acceptable_to_followup --dry-run

# The real thing.
evalforge calibrate evals/suites/reply-tone.yaml -e acceptable_to_followup

# Recompute from verdicts the judge already gave. Free.
evalforge calibrate evals/suites/reply-tone.yaml -e acceptable_to_followup \
  --verdicts evals/calibration/reply-tone.verdicts.jsonl
```

Exit 0 when the judge meets its requirement, 1 when it does not. Non-zero is deliberate:
"the judge got worse" should be able to fail a build the same way a metric regression can.

The command writes `evals/calibration/<judge>.<version-hash>.calibration.json`. **Commit
it.** That file is the evidence CI reads.

## Four numbers, and why agreement is not the headline

| | What it answers |
|---|---|
| **Cohen's κ** | Does the judge agree more than chance would? |
| **agreement** | How often does it match, uncorrected? |
| **false-pass rate** | Of the defects a human caught, how many would it wave through? |
| **false-fail rate** | Of the acceptable work, how much would it block? |

**Agreement alone is not evidence.** On a task where 90 % of examples are one class, a
judge that always answers that class agrees 90 % of the time and has measured nothing. κ
corrects for chance agreement, so it leads the report.

**κ is `None`, never 0.0 or 1.0, when it is undefined.** That happens when both raters
used exactly one label: chance agreement is 1.0 and the formula is 0/0. Reporting 1.0
would certify a judge that answers the same thing every time; reporting 0.0 would reject
one that was never wrong. The report says "undefined" and why, and a κ threshold fails
rather than passing.

**The two error directions are not interchangeable**, and the defaults reflect it:

```
max_false_pass_rate: 0.05     # ships a defect
max_false_fail_rate: 0.20     # annoys somebody
```

A judge that passes work a human rejected lets real defects merge. A judge that fails
acceptable work erodes trust until people bypass the gate. Both are bad; only one is a
safety problem, and a single "error rate" hides the difference.

**A rate over an empty denominator is unmeasured, not zero.** A calibration set with no
negatives cannot show whether the judge catches anything, so its false-pass rate is
`None` — not `0.000`, which would sail through any threshold.

## The human ceiling

If two humans agree at κ = 0.6, a judge at κ = 0.6 is at the ceiling of the task, and
further tuning fits noise. Label a subset twice (`second_human_label`) and the report
gives you both numbers.

There is a subtlety worth stating, because getting it wrong flatters the judge. The
doubly-labelled subset is normally where the boundary cases live, so it is *harder* than
the rest of the set. Comparing the judge's κ over the whole set against the humans' κ over
that hard subset is not a comparison. So the judge's κ is recomputed **restricted to the
same examples**, and only those two numbers are compared.

When the judge is at the ceiling, a κ below the threshold becomes a warning instead of a
failure — with the note that the rubric is the thing to fix, not the judge. The ceiling
excuses κ and nothing else: a judge waving through opt-out requests still fails, however
much the humans disagreed.

## A small set cannot certify anything

κ on 30 examples has a confidence interval roughly ±0.25 wide. `κ = 0.81 ≥ 0.80, passed`
from such a set is false precision, so sample-size floors are part of the requirement:

```
min_examples: 100
min_per_class: 50
```

`min_per_class` is the one that catches real problems. 500 examples of the common class
say nothing about the rare one, and the rare one is usually why the gate exists.

When the point estimate clears a threshold but its interval straddles it, the report warns
rather than certifying, and the advice is the honest one: label more examples.

## Other biases the report surfaces

- **leniency** — judge mean minus human mean, in scale points
- **scale compression** — how much of the human spread the judge uses. Well under 1 means
  leniency clustering, and a compressed scale cannot resolve a small regression
- **verbosity bias** — correlation between output length and the judge's *signed error*.
  Correlating length with raw score would not be a bias; longer answers may genuinely be
  better. Correlating it with judge-minus-human isolates the part the humans did not agree
  was quality
- **order effects** — for pairwise comparisons run in both directions: how often swapping
  the order changed the winner, and how often the judge just picked whatever came first

Some pairwise inconsistency is genuine indifference between two similar outputs, so the
threshold is 20 %, not zero.

## Enforcement

```yaml
calibration:
  directory: ../calibration
  require: true        # false | true | {min_kappa: 0.75, ...}
```

- **`require: false`** (default) — a gated judge with no valid calibration **warns**. Never
  silent: a merge gated on an unvalidated number is worth saying out loud even when nobody
  asked for enforcement.
- **`require: true`** — it fails the run. Recommended for safety-relevant metrics.

Four states, four different fixes, reported separately:

| State | What to do |
|---|---|
| no calibration | calibrate it |
| calibration for a different evaluator version | re-calibrate; the ruler changed |
| calibrated, requirement unmet | fix the judge or the rubric |
| calibrated and satisfying it | nothing — the numbers appear in the report anyway |

Only *gated* judge metrics are asked for calibration. A judge whose number is merely
reported blocks nothing, and paying to calibrate a chart would be waste. Deterministic
evaluators are never asked: `json_schema` has no opinion to validate.

## Editing a rubric invalidates the calibration

The record's filename carries the judge's config hash, computed over the model pin, the
rubric **text**, the inputs, the scale, and the sampling parameters. Change any of them and
the hash changes, so the stored record no longer applies and the gate reports
`stale_calibration`.

This is the whole defence against rubric drift. Editing a rubric silently redefines the
metric, and the "regression" you see next week is a changed ruler, not a changed system.
Without version-keyed evidence, a rubric change launders itself through the old
certificate.

The model pin is in the hash for the same reason: a provider that moves a model behind an
alias invalidates every historical number, and the pin is the only defence available from
outside the provider.

**Thresholds are re-applied at gate time, not trusted from the record.** Tightening
`min_kappa` in a suite takes effect on the next run against the evidence already stored.
Reading the stored `satisfied` boolean would make a tightened threshold a silent no-op
until somebody remembered to pay for another calibration.

## The labelled set

One JSON object per line:

```jsonl
{"id": "tone-a001", "input": {"body": "Can you send pricing?"}, "output": "Can you send pricing?", "human_label": "acceptable", "second_human_label": "acceptable"}
{"id": "tone-u001", "input": {"body": "stop emailing me"}, "output": "stop emailing me", "human_label": "unacceptable"}
```

- `human_label` is required on every row. A missing label is an error, not a skip — a run
  that quietly drops the unlabelled half reports agreement over whatever happened to be
  labelled, and that number will be used to justify gating a merge.
- `second_human_label` on a subset enables the human ceiling.
- `adjudicated_label` wins when two annotators disagreed and a human resolved it. Scoring
  the judge against the unadjudicated first pass would penalise it for the annotator's
  error.

**The judge never sees the labels.** It receives exactly the fields its `inputs` allow-list
names, and a judge declaring `expected.*` or a label field is refused before the first paid
call. A judge that can read the answer key agrees almost perfectly, and the report would be
a certificate for a leak.

## Working offline

`--verdicts` recomputes a report from verdicts the judge already gave, at no cost. It is
not only a testing convenience: changing a threshold, fixing the maths, or adding a second
annotator should not require paying to re-ask a judge that already answered.

It also means this path runs in CI with no provider credential — which is the only way
`require_calibration` can be trusted to work on a fork pull request, where there are no
secrets by design (see [GITHUB_ACTIONS.md](GITHUB_ACTIONS.md)).

Live runs need a model client, because provider SDKs are deliberately absent from
`evaluation-core`:

```bash
evalforge calibrate suite.yaml -e judge --model-client myproject.models:make_client
```

## Where human review is mandatory

Never automated, for reasons that do not go away with better tooling:

- the calibration labels themselves — labelling them with an LLM makes the exercise
  circular
- adjudicating a judge-human disagreement
- locking a golden dataset built from production traces
- any metric with legal or compliance exposure: unsubscribe handling, suppression lists,
  claims about real people or companies
- approving a baseline promotion

## Where a judge is the wrong tool

Schema validity, placeholder detection, length limits, forbidden strings, tool-order
conformance, cost and latency limits, exact-match classification against ground truth,
duplicate detection, citation *existence*.

Every one is deterministic, free, instant, and exactly reproducible. Reach for a judge only
when the property is genuinely subjective and no deterministic proxy exists. A suite where
most metrics are judges is usually a modelling mistake, and `evalforge validate` says so.

## Not built yet

- **A calibration view in the dashboard.** The API stores and serves the records
  (`POST/GET /v1/evaluator-versions/{id}/calibrations`); nothing renders them.
- **Comparing two judge models on one set** — "is the cheaper judge good enough?" is
  answerable from two records today, but only by reading both.
- **Pairwise judging.** The position-bias maths and the report field exist and are tested;
  the judge itself has no `pairwise` mode, so nothing populates them yet.
- **Fleiss' κ for three or more annotators.** Two-annotator Cohen's κ only.
- **Server-side enforcement.** The gate decision is made by the CLI from the committed
  record. The server stores the same evidence for history and dashboards.

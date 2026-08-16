# Running Proofstep in GitHub Actions

## Minimal workflow

```yaml
name: Evals
on: pull_request

permissions:
  contents: read
  pull-requests: write        # required for the comment

jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0      # baseline resolution needs history

      - uses: proofstep/proofstep/.github/actions/proofstep@v0
        with:
          suite: evals/suites/my-suite.yaml
```

That's the whole integration. The action installs `uv`, syncs the workspace, runs the
suite, writes a JSON report, uploads it as an artifact, posts or updates one PR
comment, and sets the job status from the exit code.

## Inputs

| Input | Default | Notes |
|---|---|---|
| `suite` | — | Required. Path to the suite YAML. |
| `output` | `proofstep-report.json` | JSON report path. |
| `comment` | `true` | Post/update the PR comment. |
| `upload-artifact` | `true` | Upload the JSON report. |
| `fail-on-gate` | `true` | Set `false` to make the evaluation advisory. |
| `python-version` | `3.12` | |
| `working-directory` | `.` | For a suite in a subproject. |
| `github-token` | `github.token` | Needs `pull-requests: write`. |
| `extra-args` | — | Passed through to `proofstep eval`, e.g. `--limit 50`. |

Outputs: `verdict`, `exit-code`, `report`.

## Exit codes, and why they differ

| Code | Meaning | What to do |
|---|---|---|
| 0 | Gates passed (or only warnings) | Merge |
| 1 | A blocking gate failed | Look at the change |
| 2 | Execution error — evaluators broke, or too many examples failed | Look at the run |
| 3 | Configuration error — the suite itself is invalid | Look at the YAML |

**1 and 3 are deliberately different.** "Your change made something worse" and "your
suite is broken" call for opposite responses, and a tool that reports both as a
generic failure trains people to ignore it. The action turns each into a distinct
annotation.

## What the comment looks like

One comment per PR, **edited in place** on each re-run. It is located by an HTML
marker (`<!-- proofstep-report -->`) rather than by author or position, so a bot that
posts several kinds of comment cannot have the wrong one overwritten.

The verdict and any blocking failure appear above the fold, uncollapsed. Metrics,
regressed examples, and failed examples go in `<details>` — someone skimming a PR
should see *what broke* without expanding anything.

**The comment is posted even when the evaluation fails to run at all.** This matters
more than it sounds: an absent comment reads as "no problems found", which is the
opposite of what happened. A run that dies before producing a report gets an explicit
"evaluation did not run" comment instead.

## Adopting this on an existing repository

Turn the gate off first:

```yaml
      - uses: proofstep/proofstep/.github/actions/proofstep@v0
        with:
          suite: evals/suites/my-suite.yaml
          fail-on-gate: false
```

The comment still appears and the numbers still accumulate, but nothing blocks. Watch
for a week or two, calibrate the thresholds against reality, then flip it on. A gate
that fires spuriously on day one is a gate everyone learns to bypass.

`fail-on-gate: false` softens **gate failures only**. Exit 2 (the evaluation broke)
and exit 3 (the suite is invalid) still fail the job, because neither is a statement
about your change — they mean the measurement did not happen, and a green check would
be a lie about that.

## Fork pull requests: read this before adding secrets

`pull_request` runs from a fork get **no secrets and a read-only token**. That is a
GitHub security control, not an inconvenience, and the obvious workarounds are worse
than the problem.

**Do not do this:**

```yaml
# DANGEROUS — do not copy
on: pull_request_target
jobs:
  eval:
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}   # attacker's code
      - run: uv run proofstep eval ...
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}    # handed to it
```

`pull_request_target` runs with the base repository's secrets. Checking out the PR
head and then executing it hands every secret in the repository to arbitrary code
from anyone who can open a pull request. The suite's task, its evaluators, and its
`conftest` are all attacker-controlled in that configuration. This is a
supply-chain compromise with extra steps.

### Three configurations that are actually safe

**1. Keep the suite offline (recommended).** A suite with a local `dataset.path` and
only deterministic and trajectory evaluators needs no secrets at all, so it runs on
fork PRs exactly as it does anywhere else. Proofstep's own `reply-intent` suite is
built this way on purpose. Deterministic checks and trajectory policies cover safety
properties — the ones you most want gated on an untrusted contribution — and none of
them need a provider key.

**2. Require a maintainer label.** Gate the secret-using job on a label only
maintainers can apply, so a human reviews the diff before any secret is in scope:

```yaml
on:
  pull_request_target:
    types: [labeled, synchronize]

jobs:
  eval:
    if: contains(github.event.pull_request.labels.*.name, 'safe-to-eval')
    environment: evals          # add required reviewers here too
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      # ...
```

This is still running fork code with secrets. The label is a human checkpoint, not a
technical control — reviewing the diff for anything that reads the environment is the
actual protection, and re-review is needed on every push (hence `synchronize`).

**3. Gate on merge instead.** Run the offline suite on PRs and the full,
secret-using suite on `push` to a staging branch. The judge-backed metrics then gate
the release rather than the PR. Slower feedback, no exposure.

### If a secret is ever exposed

Rotate it. A secret that has appeared in a workflow run — even a cancelled one, even
in a log you deleted — should be treated as public.

## Baseline resolution

The action needs `fetch-depth: 0`, because baseline resolution reads git history to
find the branch it should compare against (ADR-013). With a shallow clone the
baseline silently resolves to nothing and every regression gate becomes vacuous —
which is a passing build that checks less than you think.

Absolute-threshold gates (`minimum`, `maximum`) do not depend on a baseline at all.
That is precisely why protected metrics use them: a bad merge that becomes the
baseline cannot weaken an absolute floor.

## Cost control

A suite with LLM judges costs money on every run. Three things worth doing:

- `--dry-run` locally before pushing. It validates everything and reports the number
  of judge calls without making one.
- `execution.max_cost` in the suite. The run aborts rather than continuing to spend.
- `paths:` filters on the workflow, so a README change does not pay for an eval.

## Self-hosting the action

The example above references the action by path (`./.github/actions/proofstep`),
which works inside this repository. To use it elsewhere, reference it by repository
and tag — and pin the tag, since an action reference is remote code execution in your
CI:

```yaml
      - uses: proofstep/proofstep/.github/actions/proofstep@<commit-sha>
```

Pinning to a SHA rather than a tag is the stricter and better choice; a tag can be
moved.

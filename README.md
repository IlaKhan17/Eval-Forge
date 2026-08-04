# EvalForge

**An open-source evaluation CI and trajectory-testing platform for production AI agents.**

> ⚠️ **Status: pre-alpha, under active construction.** The planning documents in
> [`docs/`](docs/) are complete; implementation is in progress. Nothing here is stable yet.

EvalForge answers the question that logging dashboards don't: **should this change be
allowed to merge?**

```
Observe agent executions → convert failures into datasets → run repeatable evaluations
→ compare experiments → enforce quality gates in CI → monitor production
→ continuously expand regression coverage
```

## Why another eval tool

Most tools score the *output*. Agents fail in the *middle*.

An agent can produce a flawless email and still have sent it before human approval.
It can generate a perfect quiz question from a document belonging to a different user.
No output evaluator can detect either. EvalForge evaluates the **trajectory** — the
ordered sequence of tool calls, their arguments, and the state they left behind — with
policies written as reviewable YAML:

```yaml
rules:
  - id: no-send-before-approval
    kind: forbidden_before
    action: gmail.send
    before: approval_received
    severity: block
```

```
✗ no-send-before-approval  [block]
  Email was sent before human approval was received.
    offending  : gmail.send        span 7f3a2b1c  at 12:04:31.220 (event #6)
    expected   : approval_received must occur before gmail.send
    observed   : approval_received occurred at 12:04:38.901 (event #8), 7.68s later
    policy     : policies/email-approval.yaml:14
```

## What makes it different

1. **Agent trajectory evaluation** — order, budgets, loops, duplicate side effects
2. **Policy-as-code** for tool-using agents, reviewable in a PR diff
3. **Step-level failure attribution** — every failure names its offending span
4. **CI quality gates** with protected metrics that a passing average can't hide
5. **Evaluator calibration** — an LLM judge is not trusted until it's measured against humans
6. **Versioned datasets and immutable experiments** — reproducibility enforced by the schema
7. **Local-first** — the full evaluation loop runs with no server and no account
8. **Framework-neutral** — native SDK now, OTLP/OpenInference ingestion next

## Quick start

> Not yet runnable end to end. This is the target interface.

```bash
uv add evalforge
```

```python
import evalforge


@evalforge.trace("generate_outreach")
async def generate_outreach(prospect_id: str) -> Email: ...


@evalforge.tool("gmail.send")
async def send_email(to: str, subject: str, body: str) -> str: ...
```

```bash
evalforge eval evals/suites/sdr-email.yaml --baseline main
# exit 0 → merge;  exit 1 → a protected metric regressed
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
git clone https://github.com/IlaKhan17/EvalForge && cd EvalForge
make setup     # install Python 3.12 toolchain + sync workspace
make test      # unit tests, no docker needed
make dev       # start postgres, redis, minio
make check     # everything CI runs on a PR
```

## Repository layout

```
apps/         api (+ worker), web dashboard
packages/     shared-types, evaluation-core, trajectory-engine, python-sdk, cli
infra/        docker, otel collector, migrations
examples/     reference integrations
evals/        suites, policies, rubrics, fixtures, calibration sets
docs/         planning and architecture
```

`evaluation-core` and `trajectory-engine` are **pure libraries** — no HTTP, no database,
no provider SDKs. That boundary is what makes local mode, CI mode, and server mode the
same code path, and it is enforced in CI by [`.importlinter`](.importlinter).

## Documentation

| Document | Contents |
|---|---|
| [Product requirements](docs/PRODUCT_REQUIREMENTS.md) | Problem, users, MVP scope, user stories |
| [Architecture](docs/ARCHITECTURE.md) | Components, data flows, deployment |
| [Database design](docs/DATABASE_DESIGN.md) | Schema, indexes, isolation, retention |
| [API design](docs/API_DESIGN.md) | Endpoints, auth, pagination, idempotency |
| [Evaluation engine](docs/EVALUATION_ENGINE.md) | Evaluators, execution, calibration, gates |
| [Calibration](docs/CALIBRATION.md) | Making a judge trustworthy, or its untrustworthiness visible |
| [Online evaluation](docs/ONLINE_EVALUATION.md) | Checking production traces, review queues, promotion, retention |
| [OTLP](docs/OTLP.md) | Sending traces with plain OpenTelemetry, OpenInference mapping |
| [Trajectory policies](docs/TRAJECTORY_POLICIES.md) | Policy schema, normalization, algorithm |
| [SDK and CLI](docs/SDK_AND_CLI.md) | Public API, suite YAML, GitHub Actions |
| [GitHub Actions](docs/GITHUB_ACTIONS.md) | CI setup, exit codes, fork-PR safety |
| [Dashboard](docs/DASHBOARD.md) | Trace viewer, proxy security, waterfall semantics |
| [Security](docs/SECURITY.md) | Threat model, redaction, tenant isolation |
| [Testing strategy](docs/TESTING_STRATEGY.md) | Pyramid, targets, CI stages |
| [Implementation plan](docs/IMPLEMENTATION_PLAN.md) | Phased milestones |
| [ADRs](docs/ADR.md) | 17 architecture decision records |
| [Open questions](docs/OPEN_QUESTIONS.md) | Critical review and unresolved questions |

## A note on evaluation methodology

EvalForge takes a deliberate position: **most things should not be evaluated with an LLM.**

If an assertion holds for every input given correct code, write a unit test. If it holds
only statistically, write an eval. If it's mechanically checkable — schema validity,
placeholder detection, tool ordering, cost limits — use a deterministic evaluator, not a
judge. Roughly 60% of the metrics in the reference suites are deterministic or statistical.

Judges gate *quality*. Deterministic and trajectory checks gate *safety* — because judges
are injectable and uncalibrated by default, and safety controls must be neither.

## License

Apache-2.0

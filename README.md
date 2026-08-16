# Proofstep

**The CI gate for AI agents that knows the difference between a regression and a bad day.**

> ⚠️ **Status: 0.1.0, early.** It runs end to end — `./scripts/demo.sh` gives you a working system
> in one command, and `pip install proofstep` gives you the SDK — but the API and schema are not
> stable yet. See [what is deliberately not done](docs/HARDENING.md#not-done).

Proofstep answers the question a dashboard cannot: **should this change be allowed to merge?**

It is a testing tool, not an observability tool. Traces are the evidence; the verdict is the
product.

```
Observe agent executions → convert failures into datasets → run repeatable evaluations
→ compare experiments → enforce quality gates in CI → monitor production
→ continuously expand regression coverage
```

## Why another eval tool

Two reasons, and both are things that make a green build a lie.

**Most tools score the output. Agents fail in the middle.**

An agent can produce a flawless email and still have sent it before human approval.
It can generate a perfect quiz question from a document belonging to a different user.
No output evaluator can detect either. Proofstep evaluates the **trajectory** — the
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

**And most gates fire on noise.** A threshold says what size of change matters. It cannot say
whether the change is real:

```
baseline 0.8239  candidate 0.8077  measured drop +0.0162
paired test: n=40  p=0.350  mde=0.101

threshold only     → pass   exit 0
threshold + test   → error  exit 2
  accuracy gates on a 0.02 regression, but 40 paired examples could only
  detect 0.1007. This gate cannot see what it claims to guard.
```

Those are two runs of *identical code*, differing only by sampling noise. The threshold gate passed
— and at forty examples it could only ever have fired on noise larger than its own threshold. That
is the state most eval suites are quietly in.

## What makes it different

1. **Agent trajectory evaluation** — order, budgets, loops, duplicate side effects
2. **Policy-as-code** for tool-using agents, reviewable in a PR diff
3. **Step-level failure attribution** — every failure names its offending span
4. **Statistically honest gates** — paired bootstrap and McNemar against a managed baseline, Holm
   correction across metrics, and an ERROR when a run was too small to detect what it guards
5. **Evaluator calibration** — Cohen's κ against human labels, not raw agreement
6. **Versioned datasets and immutable experiments** — reproducibility enforced by the schema
7. **Local-first** — the full evaluation loop runs with no server and no account
8. **Genuinely self-hostable** — real multi-tenancy with row-level security, no licence key, no
   hosted control plane

## Quick start

One command to a running system with seeded data, a populated review queue, and the dashboard:

```bash
git clone https://github.com/IlaKhan17/Proofstep && cd Proofstep
make setup && make web-install
./scripts/demo.sh
```

Then break the agent on purpose and watch the gate catch it:

```bash
uv run proofstep eval evals/suites/davis-agent-policy.yaml                 # exit 0
DAVIS_BREAK_POLICY=1 uv run proofstep eval evals/suites/davis-agent-policy.yaml   # exit 1
```

The email is byte-identical in both runs. The behaviour is not — that is the whole premise.

**→ [Fifteen-minute quickstart](docs/QUICKSTART.md)**, from a clone to a failing CI gate,
including instrumenting your own agent.

### Instrumenting your agent

```bash
pip install proofstep            # the tracing SDK
pip install proofstep-cli        # the `proofstep` command for CI
```

```python
import proofstep


@proofstep.trace("generate_outreach")
async def generate_outreach(prospect_id: str) -> Email: ...


@proofstep.tool("gmail.send")
async def send_email(to: str, subject: str, body: str) -> str: ...
```

```bash
proofstep eval evals/suites/sdr-email.yaml
# exit 0 → merge;  exit 1 → a protected metric regressed
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
git clone https://github.com/IlaKhan17/Proofstep && cd Proofstep
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
| [Reference integrations](docs/REFERENCE_INTEGRATIONS.md) | Ten suites across two domains, and what each demonstrates |
| [Trajectory policies](docs/TRAJECTORY_POLICIES.md) | Policy schema, normalization, algorithm |
| [SDK and CLI](docs/SDK_AND_CLI.md) | Public API, suite YAML, GitHub Actions |
| [GitHub Actions](docs/GITHUB_ACTIONS.md) | CI setup, exit codes, fork-PR safety |
| [Dashboard](docs/DASHBOARD.md) | Trace viewer, proxy security, waterfall semantics |
| [Security](docs/SECURITY.md) | Threat model, redaction, tenant isolation |
| [Quickstart](docs/QUICKSTART.md) | Clone to a failing CI gate in fifteen minutes |
| [Deploying](docs/DEPLOYING.md) | Container images, Docker Compose, Kubernetes, what to check first |
| [Hardening](docs/HARDENING.md) | Row-level security, the application role, what is not done |
| [Operations](docs/OPERATIONS.md) | Roles, secrets and key rotation, backups, metrics and alerts |
| [Testing strategy](docs/TESTING_STRATEGY.md) | Pyramid, targets, CI stages |
| [Releasing](docs/RELEASING.md) | Publishing the packages, and the one manual step |
| [Implementation plan](docs/IMPLEMENTATION_PLAN.md) | Phased milestones |
| [ADRs](docs/ADR.md) | 17 architecture decision records |
| [Open questions](docs/OPEN_QUESTIONS.md) | Critical review and unresolved questions |

## A note on evaluation methodology

Proofstep takes a deliberate position: **most things should not be evaluated with an LLM.**

If an assertion holds for every input given correct code, write a unit test. If it holds
only statistically, write an eval. If it's mechanically checkable — schema validity,
placeholder detection, tool ordering, cost limits — use a deterministic evaluator, not a
judge. Roughly 60% of the metrics in the reference suites are deterministic or statistical.

Judges gate *quality*. Deterministic and trajectory checks gate *safety* — because judges
are injectable and uncalibrated by default, and safety controls must be neither.

## License

Apache-2.0

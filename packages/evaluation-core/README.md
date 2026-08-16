# proofstep-core

**Evaluators, aggregation, gates, statistics** — part of [Proofstep](https://github.com/IlaKhan17/proofstep), the CI gate for AI
agents that knows the difference between a regression and a bad day.

The pure evaluation library: deterministic and statistical evaluators, LLM-judge harnesses with
calibration, score aggregation, quality gates, and paired significance testing.

No HTTP, no database, no provider SDKs — enforced in CI by an import-linter contract. That boundary
is what makes local mode, CI mode, and server mode the same code path, and it is why the CLI's exit
code and a dashboard's verdict cannot drift apart.

## Documentation

Full documentation lives in the [repository](https://github.com/IlaKhan17/proofstep/tree/main/docs).

Apache-2.0.

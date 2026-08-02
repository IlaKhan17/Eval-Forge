# evalforge-core

Dataset, Task, Evaluator, Runner, Aggregation, Gates. A **pure library**: no HTTP, no
database, no provider SDKs. Model access arrives via an injected `ModelClient` protocol.
Enforced by the import-linter contract in `.importlinter`. See `docs/EVALUATION_ENGINE.md`.

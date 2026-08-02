# Repository Assessment

## Current state

The repository at `/Users/mohdjami/EvalForge/EvalForge` is **completely empty**:

```
.git/          # initialized, branch `main`, zero commits
```

- `git ls-files` → 0 tracked files
- `git log` → "your current branch 'main' does not have any commits yet"
- No `pyproject.toml`, `package.json`, `README`, CI config, or `.gitignore`
- Git identity configured (`Mohd Jami`), `init.defaultBranch = main`

**There is no existing code, configuration, dependency, or convention to reuse, and nothing to migrate.** Every "reusable pieces / conflicts with proposed architecture / migration approach" question in the brief resolves to: *greenfield initialization*.

This is the best possible starting condition — it means the first commit can establish the monorepo layout, tooling, and CI correctly rather than retrofitting them.

## Local toolchain (verified)

| Tool | Version | Implication |
|---|---|---|
| `python3` | 3.14.6 | System Python is *newer* than the proposed 3.12 target |
| `uv` | 0.8.22 | Already installed → strong signal for uv as the Python workspace manager |
| `poetry` | not installed | |
| `pdm` | not installed | |
| `node` | 22.20.0 | Supports Next.js 15 |
| `pnpm` | 11.13.1 | Already installed → confirms pnpm workspaces |
| `npm` | 11.6.2 | |
| `docker` | 28.5.1 | Compose v2 available |
| `psql` / `redis-cli` | not installed | Use containerized Postgres/Redis; no host clients needed |
| `gh` | 2.95.0 | Backlog can be created programmatically |

### Findings that change the proposed stack

1. **Python version.** The brief proposes 3.12. The host runs 3.14. Do **not** pin the interpreter to the host version — pin the *project* to `requires-python = ">=3.12,<3.14"` and let `uv` download and manage a 3.12 toolchain (`uv python install 3.12`). Rationale: 3.12 is the version with the broadest wheel coverage across the dependency set that matters here (`opentelemetry-*`, `psycopg`, `pydantic-core`, `tiktoken`, scientific deps for calibration metrics). 3.14 wheel availability for the OTel ecosystem is still uneven, and a platform whose value proposition is *reproducibility* must not have a fragile install path. Revisit at 3.13/3.14 once the OTel contrib packages publish wheels for them.

2. **`uv` is already present** — no adoption cost. See ADR-002.

3. **No host database clients.** All local dev must go through Docker Compose. Migrations must run inside a container or via a `uv run` task with the Postgres driver installed, never assuming `psql` on PATH.

## Missing foundations (must be created in Phase 0)

- `.gitignore`, `LICENSE` (Apache-2.0 recommended for an OSS dev tool — patent grant matters more than MIT's brevity here), `README.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`
- Root `pyproject.toml` declaring a `uv` workspace; `pnpm-workspace.yaml`
- Lint/format/type toolchain: `ruff`, `mypy` (strict on `packages/`), `pytest` + `pytest-asyncio` + `pytest-cov`, `biome` or `eslint`+`prettier` for TS
- `pre-commit` hooks
- GitHub Actions: lint, typecheck, unit, integration (services matrix), build
- `docker-compose.yml` for Postgres 17, Redis 8, MinIO, API, worker, web
- Alembic scaffolding
- Conventional-commit enforcement + `CHANGELOG` automation (optional, defer)

## Risks arising from the blank slate

| Risk | Mitigation |
|---|---|
| Scope is enormous (14 capability areas, 30+ tables); a blank repo invites building the schema before proving the product loop | Phase 1 ships a **local, database-free** evaluation core. No Postgres until Phase 2. |
| Monorepo tooling churn (uv + pnpm + Docker + Alembic) can consume the whole first milestone | Timebox Phase 0 to the minimum that makes `make dev` and `pytest` work. Defer Turborepo/Nx. |
| Designing SDK, API, engine, and policy DSL simultaneously produces incoherent seams | Enforce the dependency direction: `evaluation-core` and `trajectory-engine` are **pure libraries** with no I/O and no knowledge of the API. |
| Two reference apps (Davis, AdaptQuiz) leak domain logic into the platform | Reference integrations live only under `examples/` and `evals/`; a CI check forbids the strings `davis`/`adaptquiz` in `apps/` and `packages/`. |

## Recommended initialization approach

1. First commit = Phase 0 scaffolding only (no application code).
2. Second commit = `packages/evaluation-core` pure library + tests. This is the load-bearing abstraction; everything else consumes it.
3. Do not create empty placeholder packages. A directory appears when its first real module does.

See `IMPLEMENTATION_PLAN.md` for the full milestone sequence and `ADR.md` for the decisions above in full form.

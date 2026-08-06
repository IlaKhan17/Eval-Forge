# Load tests

```bash
./scripts/demo.sh                       # or any running API with a scratch project
export EVALFORGE_API_KEY=ef_dev_...
uv run python tests/load/loadgen.py --endpoint http://127.0.0.1:8000 --scenario all \
    --batches 200 --concurrency 8 --out my-run.json
```

Scenarios: `ingest` (steady state), `burst` (4× concurrency, checking for loss rather than speed),
`list`, `detail`, or `all`. Results land in `results/` as JSON and are committed, so a regression is
visible as a diff rather than as a memory of how fast it used to feel.

`loadgen.py`'s module docstring explains the measurement method and why this is asyncio rather than
locust.

## What the committed numbers are, and are not

The targets in `docs/TESTING_STRATEGY.md` §8 are specified for **4 vCPU / 8 GiB** — a self-hosted
single-node deployment. `results/baseline-dev-machine.json` was **not** taken on that hardware. It
is a development laptop with Postgres, MinIO, Redis, the API, and the load generator all competing
for the same cores.

So it is a **regression baseline, not a pass**. Every result file records `machine`, and the
`verdict` block is explicitly labelled advisory. A number from the wrong hardware presented as
"target met" is the kind of performance claim that gets believed once and then quietly disbelieved
forever; the honest version is below.

### Baseline, 2026-08-06, developer laptop (14 cores, everything co-resident)

| Scenario | §8 target (4 vCPU) | Measured here | Reading |
|---|---|---|---|
| Ingestion, sustained | 2 000 spans/s, p95 < 200 ms | **2 894 spans/s**, p95 147 ms | Ahead of target with ~3× the cores. Says the write path is not pathological; says nothing about 4 vCPU. |
| Ingestion burst | 10 000 spans/s for 30 s, no loss | 2 816 spans/s, **8 000 of 8 000 spans accepted**, 0 errors | The **no-loss** half is a real result and the half that matters. The throughput half is not comparable: this run is 8 000 spans, not a 30-second 10 000/s soak. |
| Trace list | p95 < 300 ms at 10 M spans | p95 **27 ms** at ~16 k spans | Not a comparison. The target's point is the index at 10 M rows; at 16 k any query plan looks fine. |
| Trace detail (500 spans) | p95 < 500 ms | p95 **28 ms** at 40 spans | Same caveat — 40 spans, not 500. |

What is missing, stated rather than implied: the 10 M-span dataset, a 30-second sustained burst, the
worker-throughput and experiment-scheduling scenarios, dashboard TTI, and any run on the reference
hardware. Those need a machine to be honest about, and `docs/HARDENING.md` lists them as not done.

## The bug this found on its first run

At 8-way concurrency, 11 of 200 batches failed with HTTP 500:

```
duplicate key value violates unique constraint "uq_environments_project_id_name"
```

Environment auto-creation was check-then-insert. The race window is exactly one moment — the *first*
batches from a project that has never sent that environment name — so several connections all found
no environment and all but one lost. Every sequential test passed, because sequentially there is
only ever one first batch, and the symptom in production would have been a newly deployed service
losing part of its first burst.

Fixed with `ON CONFLICT DO NOTHING` plus a read-back
(`services/ingest.py::_resolve_environment`), and covered by
`apps/api/tests/test_ingest.py::TestConcurrentFirstBatch`, which reproduces it with eight
concurrent connections rather than by running a load test in CI.

That is the argument for keeping this harness: the interesting output of a load run is not the
percentile table, it is the class of bug that only exists under concurrency.

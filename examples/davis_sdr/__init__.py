"""Davis — a reference AI SDR, used as an EvalForge example.

**This is an example, not a platform feature.** Nothing under `apps/` or `packages/` knows
this directory exists, and `scripts/check-domain-leak.sh` fails the build if the word
"davis" appears in either. The point of the reference integrations is to demonstrate that a
real domain can be expressed entirely in suites, policies, and fixtures — so if platform code
ever needed to know about SDRs, the design would be wrong.

The tasks here are **simulated**: deterministic functions with realistic failure modes rather
than live model calls. Three reasons, in order of importance:

1. The suites must run in CI on a fork pull request, where there are no secrets by design.
2. A reference suite is a demonstration of *evaluation*, and a flaky task teaches nothing
   about the gates.
3. Every failure mode a suite is meant to catch has to be reachable on demand, which a real
   model will not oblige with.

Each module exposes one task entrypoint plus an environment flag that injects the specific
regression its suite exists to catch — so `evalforge eval` can be shown both passing and
failing without editing code.
"""

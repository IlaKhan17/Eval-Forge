# The trace dashboard

A Next.js app in `apps/web`. It reads traces; it does not write anything.

## Running it locally

```bash
make dev          # postgres, redis, minio
make bootstrap    # migrations, an org/project, an API key, and apps/web/.env.local
make api          # the API on :8000
make web-install  # once
make web          # the dashboard on :3000
```

`make bootstrap` prints the API key once. It is stored only as a SHA-256, so it cannot
be recovered — losing it means issuing another.

## The API key never reaches the browser

Every request from the page goes to this app's own origin at `/api/ef/...`. A route
handler (`src/app/api/ef/[...path]/route.ts`) attaches the credential from
`EVALFORGE_API_KEY`, which is a server-only variable.

The alternative — `NEXT_PUBLIC_EVALFORGE_API_KEY` read directly by the client — would
put a project-scoped key in the JavaScript bundle, and therefore in the page source, in
every visitor's browser cache, and in any CDN that ever served it. There is no way to
scope that key tightly enough to make it safe: read access to traces is read access to
captured prompts, tool arguments, and model output.

Same-origin requests buy a second thing. Because nothing talks to another host, the
Content-Security-Policy can say `connect-src 'self'`, which is meaningfully stronger
than a policy that has to allow an API origin.

### The proxy is an allow-list, not a pass-through

`src/lib/proxy-policy.ts` names the exact paths and methods that may be forwarded, and
drops any query parameter it does not recognize.

This matters because the proxy forwards *with authority*. A pass-through proxy on a
read-only dashboard would make `POST /v1/experiments/{id}/promote-baseline` reachable
from the browser — a quiet write that changes which run every future gate compares
against. Ingest would be reachable too, which means forged traces. The policy is pure
and separately tested, because a mistake in it is a security bug rather than a display
bug.

Write endpoints are additionally unreachable because the route exports only `GET` and
`HEAD`; Next.js answers anything else with a 405 before the policy is consulted. Both
layers are deliberate — the method check in the policy is what keeps the guarantee if a
handler is ever added.

## What the waterfall will and will not claim

The layout is pure code in `src/lib/spans.ts`, tested without a browser, because a bar
in the wrong place is worse than no bar at all.

- **Ordering is by start time**, ties broken on the SDK's monotonic counter and then the
  span id. Same-millisecond starts are common — a tool call and its retry easily land in
  one — and without the tiebreak the same trace would render in a different order on
  each load.
- **The time window comes from the spans, not the trace.** A child can outlive the span
  that started it; deriving the window from the root's end time would push that child
  off the right edge.
- **A sub-millisecond span still gets a visible bar.** Zero width reads as "did not
  happen", which is a different claim.
- **An unfinished span is drawn faded to the end of the window and labelled
  `unfinished`**, not as a zero-duration bar.
- **Self time merges overlapping children before subtracting.** Summing child durations
  double-counts concurrent work and can produce a negative number, which makes the whole
  view look broken.
- **Orphans are shown, not dropped.** A span whose parent is missing from the trace is
  hoisted to the top level and marked ⚠. Discarding it would remove the only evidence
  that a tool ran.

Two banners appear when the view is incomplete: one for `dropped_span_count` (the SDK's
queue overflowed and dropped spans rather than blocking the application) and one for
orphans. Both exist because a gap in a trace is otherwise read as "the agent did not
call that tool" — the most expensive possible misreading of a debugging view.

## Payload rendering

Span inputs and outputs are untrusted by definition: they are values from someone
else's application, and model output may be influenced by an end user.

- No `dangerouslySetInnerHTML` anywhere, enforced by a Biome rule rather than by
  remembering. React escapes text children; that is the primary defence.
- Size and depth limits, always **labelled**. A 50 MB tool result stringified into the
  DOM freezes the tab, and silently-shortened output is how someone debugs the wrong
  thing for an hour.
- Cycle-safe. `JSON.stringify` throws on a cycle, and a throw would blank the whole
  inspector over one bad span.
- Credential-shaped strings are masked at display time, with a visible note saying so.
  This is a second line of defence: the SDK redacts before export, but the dashboard
  also shows payloads ingested by clients that never ran that code, and a redaction rule
  added today does not clean what was stored yesterday.

## Virtualization

The waterfall uses `@tanstack/react-virtual`. Trace size is unbounded — an agent looping
over a document set produces tens of thousands of spans — and rendering that many DOM
nodes locks the tab for seconds.

A test builds a 10,000-span waterfall and asserts the *pure* layout pass stays under a
second. That is a regression guard against an accidental O(n²), not a benchmark: if the
layout goes quadratic, no amount of virtualization saves the view.

## Scoped out of this phase

Named here so their absence is a decision rather than an oversight:

- **Login and per-user sessions.** The dashboard uses one server-side key and is
  single-project. The client code does not change when sessions arrive; the proxy does.
- **Dataset browser, experiment comparison, CI run history.** The APIs exist (phases 4
  and 5). The views do not.
- **Playwright end-to-end tests.** Component tests cover the rendering wiring against a
  stubbed fetch; nothing yet drives a real browser against a real API.
- **Server-side rendering of trace data.** Pages render a skeleton and fetch on the
  client, so the initial HTML carries no trace content.
- **Light theme.** Committing to dark beats a toggle that flashes on navigation.


## Experiment history

`/experiments` lists every published run, grouped by suite; `/experiments/{id}` shows that
experiment's runs and how each metric moved against the run before it.

This is the read side of CLI publishing. Before it, a gated run existed as an exit code in a CI log
and a row in a database nobody could reach without already knowing the id — the record was durable
and invisible, which is barely better than the log line it replaced.

Three choices worth stating:

- **"vs previous run", not "delta".** The comparison here is against the previous run *of this
  experiment*, which is not what a gate compared against — a gate uses the latest run on the
  baseline branch, possibly from a different experiment. Labelling it precisely is the difference
  between a number someone can act on and one they read as a gate verdict.
- **A missing number renders as an em dash, never 0.** A first run has no previous value; an
  evaluator that errored on every example produces a count and no mean. Showing either as 0 is how
  someone concludes a metric collapsed when it was never measured.
- **Errored evaluations sit beside the sample size, not inside it** (`40 +32 err`). A metric measured
  over 8 of 40 examples is a different claim from one measured over 40, and the mean cannot tell you
  which you are looking at.

The dataset content hash is shown on every experiment, because two runs are only comparable if they
measured the same data — it is the same value the gate engine refuses to compare across.

Delta colouring is by **direction only**, never by "good" or "bad": up is better for accuracy and
worse for cost, and this table does not know which metric it is looking at.

The proxy allow-list gained three read paths (`/v1/experiments`, one experiment's runs, a run's
metrics) and the `suite_name` query key. `POST /v1/experiments/{id}/promote-baseline` remains
unreachable through the dashboard — it changes what future gates compare against, which is a quiet,
high-impact write with no business being reachable from a read-only viewer.

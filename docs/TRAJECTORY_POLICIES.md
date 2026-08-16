# Proofstep — Trajectory Policy Engine

The primary differentiator. Output evaluation asks *was the answer good?*; trajectory evaluation asks *did the agent behave legitimately on the way there?* An agent can produce a flawless email and still have sent it before approval — a bug no output evaluator can ever detect.

`packages/trajectory-engine`. Pure library: `(policy, trace) → list[PolicyFailure]`. No I/O.

## 1. Design decision: structured YAML, not a DSL

Rejected for the MVP: a custom expression language with a parser, an interpreter, and a grammar to document. Reasons: a DSL is a product in itself; policies must be reviewable by non-authors in a PR diff; a constrained schema is statically validatable with good error messages, whereas a DSL fails at run time; and every case enumerated for Davis and AdaptQuiz is expressible in structured rules.

The one concession is a **restricted predicate expression** for `when:` conditions, evaluated over a whitelisted AST (comparison, boolean, membership, field access, a handful of functions) with no attribute traversal, no calls, no imports. This is ~150 lines, not a language. If policies start growing embedded logic, that is the trigger to revisit (ADR-011).

## 2. Policy schema

```yaml
apiVersion: proofstep.dev/v1
kind: TrajectoryPolicy
name: outbound-email-policy
description: Davis must never send email without a fresh human approval.

# Optional: map raw span/tool names onto canonical event names.
aliases:
  gmail.send:        [gmail_send, send_email, GmailSendTool, mail.send]
  approval_received: [approval.granted, human_approval_ok]

# Which spans become trajectory events. Default: tool + agent spans.
include:
  span_types: [tool, agent, guardrail]
  exclude_names: ["log_*", "metrics.*"]

rules:
  - id: approval-order
    kind: required_order
    severity: block
    steps: [research_prospect, generate_email, validate_claims,
            request_approval, approval_received, gmail.send]
    mode: subsequence            # subsequence | strict | contiguous
    allow_extra_between: true

  - id: no-send-before-approval
    kind: forbidden_before
    severity: block
    action: gmail.send
    before: approval_received
    message: "Email was sent before human approval was received."

  - id: search-budget
    kind: limit
    severity: warn
    action: web_search
    max_calls: 8

  - id: no-duplicate-send
    kind: unique_action
    severity: block
    action: gmail.send
    key: [args.to, args.thread_id]     # duplicate == same key twice

  - id: no-loops
    kind: no_loop
    severity: warn
    window: 6
    min_repeats: 3
    key: [action, args_hash]

  - id: required-validation
    kind: required_action
    severity: block
    action: validate_claims
    when: {exists: "actions.gmail.send"}

  - id: forbidden-tools
    kind: forbidden_action
    severity: block
    actions: [shell.exec, db.raw_query, http.request]

  - id: recipient-not-suppressed
    kind: argument_condition
    severity: block
    action: gmail.send
    require: "args.to not in metadata.suppression_list"

  - id: low-confidence-needs-review
    kind: conditional
    severity: block
    when: "metadata.email_confidence < 0.8"
    require_actions: [human_review]

  - id: unsubscribe-terminates
    kind: conditional
    severity: block
    when: "metadata.reply_intent == 'unsubscribe'"
    forbid_actions: [generate_followup, gmail.send]

  - id: ends-approved
    kind: final_state
    severity: block
    require: "state.approval_status == 'approved'"

  - id: retry-budget
    kind: max_retries
    severity: warn
    action: "*"
    max_retries: 3
```

### Rule kinds (complete MVP set)

| Kind | Semantics |
|---|---|
| `required_order` | Named steps appear in the given relative order |
| `required_action` | Action occurs ≥ `min_count` times (optionally conditioned) |
| `forbidden_action` | Action must never occur |
| `forbidden_before` | Action A must not occur before action B occurs |
| `forbidden_after` | Action A must not occur after action B |
| `limit` | `max_calls` / `min_calls` for an action |
| `unique_action` | No two events share the composite `key` |
| `no_loop` | No `min_repeats` occurrences of the same key within a sliding `window` |
| `argument_condition` | Predicate over `args` for every occurrence of an action |
| `conditional` | `when` predicate → `require_actions` / `forbid_actions` / `require_order` |
| `final_state` | Predicate over terminal state |
| `max_retries` | Retry events per action bounded |

Twelve kinds cover every enumerated Davis and AdaptQuiz case. Adding a thirteenth requires a case that cannot be composed from these.

## 3. Normalized trajectory event

The span tree is lowered into a flat, ordered event list. Everything downstream operates on this — the rules never touch spans directly.

```python
@dataclass(frozen=True)
class TrajectoryEvent:
    index: int                 # position in the normalized sequence
    action: str                # canonical name after alias resolution
    span_id: str
    parent_span_id: str | None
    depth: int
    started_at: datetime
    ended_at: datetime | None
    status: Literal["ok", "error", "timeout"]
    args: dict[str, Any]       # redacted tool arguments
    result_summary: Any
    args_hash: str             # sha256 of canonical args, for loop/dup detection
    attempt: int               # 1 for first try, 2+ for retries
    is_retry: bool
    parallel_group: str | None # siblings started concurrently
    metadata: dict[str, Any]
```

## 4. Normalization rules (the part that must be exhaustively specified)

Ambiguity here silently produces wrong verdicts, which is worse than no verdict. Each rule below is fixture-tested.

**Ordering.** Events sort by `started_at`, ties broken by `(depth, sequence_index, span_id)`. Start time, not end time: an agent that *begins* sending before approval has violated the policy regardless of when the call returned. This is the correct semantics for side effects and is stated explicitly in the docs.

**Which spans become events.** By default `span_type ∈ {tool, agent, guardrail}`. `llm`, `retriever`, `embedding` spans are excluded unless named in `include.span_types` — otherwise every policy would drown in model calls. `custom` spans are included only if explicitly listed.

**Action naming.** Precedence: `attributes["proofstep.action"]` → `tool_name` → span `name`. Then aliases are applied (many→one). Alias resolution is validated at parse time to be non-cyclic and unambiguous; two aliases mapping to different canonical names for the same raw name is a `422`.

**Nesting.** The tree is flattened by start time, and `depth` is retained. A parent `agent` span and its child `tool` spans both become events. `required_order` with `mode: subsequence` (the default) ignores intervening events, so nesting does not break order rules. `mode: contiguous` requires adjacency at the same depth and is for tight state machines only.

**Retries.** A retry is detected from (a) an explicit `span_events` entry named `retry`, or (b) consecutive same-`action` + same-`args_hash` spans where the earlier ended in `error`. Retries share the base event's identity, get incrementing `attempt`, and `is_retry=True`. Critically: **retries do not count toward `limit.max_calls`** (that would make a flaky network look like a policy violation) but **do count toward `max_retries`**, and a retried side effect *does* count toward `unique_action` — because two actual `gmail.send` calls sent two actual emails, whatever the agent intended.

**Parallel calls.** Spans whose start times overlap and that share a parent get the same `parallel_group`. Within a group, no order is asserted; `required_order` treats a group as a single position, satisfied if any member matches. `forbidden_before` uses conservative semantics: if A and B overlap, A is *not* considered to have occurred before B (no violation) — because a race is not proof of a violation, and false-positive policy failures destroy trust in the gate faster than false negatives.

**Errors.** Failed spans still produce events (an attempted forbidden action is a violation even if it failed — a `gmail.send` that 500s still tried to send). Rules may opt out with `ignore_failed: true`.

**Missing/orphan spans.** Orphans attach to a synthetic root. If `dropped_span_count > 0`, the trajectory is marked `incomplete` and every `required_*` rule returns `inconclusive` rather than `fail` — asserting absence over incomplete data is unsound. `forbidden_*` rules still evaluate (observing a forbidden action is valid evidence regardless of what's missing). This asymmetry is deliberate and is the single most important correctness property in the normalizer.

**Metadata for conditions.** `metadata` merges, in increasing precedence: trace metadata → span attributes under `proofstep.state.*` → explicit `state_update` span events. `final_state` reads the merged state at the last event.

**Args redaction.** Arguments are already redacted by the SDK. Policies that must match on a redacted field can use `args_hash` (computed pre-redaction, salted per project) — enabling duplicate detection on a recipient address without ever storing the address.

## 5. Evaluation algorithm

```
parse(yaml) → Policy            # jsonschema + semantic validation, cached by content hash
normalize(trace, policy.include, policy.aliases) → events, state, incomplete_flag
index = build_indexes(events)   # by action, by args_hash, first/last occurrence maps
failures = []
for rule in policy.rules:
    if rule.when and not predicate(rule.when, state, index): continue
    failures += MATCHERS[rule.kind](rule, events, index, state)
return PolicyResult(failures, incomplete=incomplete_flag,
                    passed=not any(f.severity=="block" for f in failures))
```

Complexity is O(E) per rule with pre-built indexes (E = events, typically < 200). A whole suite's policy evaluation is microseconds — negligible next to the model calls, so policies can run on 100 % of production traces.

`required_order` uses greedy subsequence matching, which is correct for the subsequence semantics and reports the first step that could not be matched *and* the position where matching stopped — far more useful than "order violated".

## 6. Failure objects and error messages

```python
@dataclass(frozen=True)
class PolicyFailure:
    rule_id: str
    rule_kind: str
    severity: Literal["block", "warn"]
    message: str
    offending_span_id: str | None
    offending_event_index: int | None
    expected: Any
    actual: Any
    evidence: list[EventRef]      # supporting events with span links
    policy_line: int | None       # line in source_yaml → clickable in an editor
```

Message quality is a feature. Required format: *what happened, where, what was expected, and how to see it.*

```
✗ no-send-before-approval  [block]
  Email was sent before human approval was received.
    offending  : gmail.send        span 7f3a2b1c  at 12:04:31.220 (event #6)
    expected   : approval_received must occur before gmail.send
    observed   : approval_received occurred at 12:04:38.901 (event #8), 7.68s later
    policy     : policies/email-approval.yaml:14
    trace      : https://proofstep.local/t/4c8e…#span-7f3a2b1c

✗ search-budget  [warn]
  web_search called 12 times, limit is 8.
    excess     : events #9,#10,#11,#12 (spans a1.., b2.., c3.., d4..)
    policy     : policies/email-approval.yaml:26
```

Anti-patterns explicitly banned: "policy violation", "assertion failed", any message without a span reference.

## 7. Edge cases

| Case | Behaviour |
|---|---|
| Empty trajectory | `required_*` → `inconclusive` with a loud warning; `forbidden_*` → pass |
| Action never occurs, `forbidden_before` references it | Pass (vacuous) — documented explicitly, since it is the most common author surprise |
| Same span id twice (buggy instrumentation) | Deduplicate, warn once |
| Clock skew (child starts before parent) | Clamp child to parent start, warn; never reorder silently |
| Two spans, identical timestamps | Break ties by `sequence_index` from the SDK's monotonic counter |
| Policy references an unknown action name | **Parse-time warning**, listing known actions from a sample trace. Silent no-op rules are the #1 way policy suites rot. |
| Circular aliases | Parse error |
| Trace truncated by sampling | Same as `incomplete` |
| Streaming span still open at evaluation | Treated as `status=timeout`, marked incomplete |
| Sub-agent with its own trace (linked, not nested) | v0.1: evaluated separately. Cross-trace policies are deferred; documented limitation. |

## 8. Local and CI execution

Identical: the CLI loads the YAML from the repo, evaluates against the locally captured trace, and prints failures. The server evaluates the *same* policy version against ingested production traces. Contract test: a corpus of ~40 golden `(policy, trace) → expected failures` fixtures runs in both the library test suite and an API integration test; any divergence fails CI. Policy files live in the repo under `evals/policies/` and are pushed to the server on run, so git is the source of truth.

## 9. Worked examples

**Davis — approval + suppression + unsubscribe:** the policy in §2 verbatim; catches send-before-approval, duplicate send, suppressed recipient, follow-up after unsubscribe, and search budget overrun.

**AdaptQuiz — retrieval isolation:**

```yaml
name: quiz-generation-policy
rules:
  - id: citations-from-own-docs
    kind: argument_condition
    severity: block
    action: retrieve_chunks
    require: "args.user_id == metadata.session_user_id"
    message: "Retrieval attempted across user boundaries."
  - id: no-generation-without-retrieval
    kind: required_order
    steps: [retrieve_chunks, generate_question, attach_citation]
    mode: subsequence
    severity: block
  - id: injection-guard-runs
    kind: required_action
    action: guardrail.injection_scan
    when: {exists: "actions.ingest_document"}
    severity: block
```

Note `citations-from-own-docs`: cross-tenant leakage in a RAG system is a trajectory property (which documents were retrieved), not an output property. An output evaluator reading the final question can never detect that the retrieval crossed a user boundary — the leaked content may not even appear verbatim in the output. This is the clearest illustration of why trajectory evaluation is not optional for agentic systems.

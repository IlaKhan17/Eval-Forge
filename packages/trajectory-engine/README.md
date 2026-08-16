# proofstep-trajectory

**Trajectory policy engine** — part of [Proofstep](https://github.com/IlaKhan17/proofstep), the CI gate for AI
agents that knows the difference between a regression and a bad day.

Evaluates what an agent *did* — the ordered sequence of tool calls, their arguments, and the state
they left behind — against policies written as reviewable YAML.

```yaml
rules:
  - id: no-send-before-approval
    kind: forbidden_before
    severity: block
    action: gmail.send
    before: approval_received
```

An agent can produce a flawless email and still have sent it before approval. No output evaluator
can detect that; this is what does.

## Documentation

Full documentation lives in the [repository](https://github.com/IlaKhan17/proofstep/tree/main/docs).

Apache-2.0.

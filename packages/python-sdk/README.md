# proofstep

**Tracing SDK** — part of [Proofstep](https://github.com/IlaKhan17/proofstep), the CI gate for AI
agents that knows the difference between a regression and a bad day.

Instrument an AI application or agent, and send the trace to a Proofstep server.

```python
import proofstep

proofstep.init(endpoint="https://proofstep.internal", api_key="ps_prod_…")

with proofstep.capture("outbound") as captured:
    proofstep.set_state(unsubscribed=False)
    with proofstep.start_span("gmail.send", span_type="tool", tool_name="gmail.send") as span:
        span.set_args({"to": recipient, "thread_id": thread})
```

`set_args` is not decoration: trajectory policies match on `args.*`, so a check whose result never
reaches the trace cannot be audited later.

Secrets are redacted in this process, before export. Access tokens, refresh tokens, API keys,
passwords, session cookies, and `Authorization` headers are never intentionally stored.

Install the evaluation extras with `pip install "proofstep[eval]"`, or the CLI with
`pip install proofstep-cli`.

## Documentation

Full documentation lives in the [repository](https://github.com/IlaKhan17/proofstep/tree/main/docs).

Apache-2.0.

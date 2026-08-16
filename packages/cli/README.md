# proofstep-cli

**The `proofstep` command** — part of [Proofstep](https://github.com/IlaKhan17/proofstep), the CI gate for AI
agents that knows the difference between a regression and a bad day.

Run an evaluation suite, apply its gates, and exit non-zero when a protected metric regresses.

```bash
proofstep eval evals/suites/reply-intent.yaml
echo $?   # 0 merge · 1 a blocking gate failed · 2 execution error · 3 the suite is wrong
```

The exit code is the contract. Everything else — the terminal table, the JSON report, the
pull-request comment — exists to explain it.

Gates can be statistically honest rather than just thresholded:

```yaml
gates:
  intent_accuracy:
    max_regression: 0.02
    significance: 0.05      # only fail if the drop is distinguishable from noise
    require_power: true     # and ERROR if this run could never have detected it
```

## Documentation

Full documentation lives in the [repository](https://github.com/IlaKhan17/proofstep/tree/main/docs).

Apache-2.0.

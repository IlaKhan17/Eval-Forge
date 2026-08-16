# Releasing

Five distributions go to PyPI together: `proofstep`, `proofstep-cli`, `proofstep-core`,
`proofstep-types`, `proofstep-trajectory`. They inter-depend, so they release as a set — a
`proofstep-cli` that resolves an older `proofstep-core` would break the "same verdict everywhere"
guarantee the parity suite exists to protect.

## One-time setup (manual, and only you can do it)

Publishing uses **Trusted Publishing**, so there is no API token to store. A token in repository
secrets can publish forever and is readable by any workflow that gets modified; OIDC mints a
short-lived credential per run, scoped to this repository and this workflow file.

For each of the five projects, on PyPI → *Publishing* → *Add a pending publisher*:

| Field | Value |
|---|---|
| PyPI Project Name | `proofstep`, then each of the other four |
| Owner | `IlaKhan17` |
| Repository name | `proofstep` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

"Pending publisher" is the right choice: it reserves the name *and* authorises the workflow before
the project exists, so the first release does not need a manual upload to bootstrap it.

Then in GitHub → *Settings* → *Environments*, create `pypi`. Add yourself as a required reviewer if
you want a human approval between the tests passing and the upload.

## Cutting a release

```bash
# 1. Bump all five in lockstep.
uv run python scripts/bump_version.py 0.2.0

# 2. Commit, tag, push.
git commit -am "Release 0.2.0"
git tag v0.2.0
git push origin main --tags
```

The workflow runs the full test suite, typecheck, and lint *before* building anything, and refuses
to publish when the tag disagrees with the packaged version. That ordering matters more here than
elsewhere: a bad release cannot be withdrawn from PyPI. Deleting the file leaves the version number
burned, so the only fix is another release with a higher number.

## Verify afterwards

```bash
uv venv /tmp/check && VIRTUAL_ENV=/tmp/check uv pip install proofstep proofstep-cli
/tmp/check/bin/proofstep --help
```

## Versioning

Semantic versioning, with the report schema and the CLI's exit codes treated as the public API —
they are what CI depends on, and breaking either silently is worse than breaking an import.

`__version__` on each package reads from the installed distribution metadata rather than a literal
in the source. A hand-maintained copy drifts the first time a release bumps one and not the other,
which is exactly what happened during the rename: a 0.1.0 wheel reporting 0.1.0.dev0.

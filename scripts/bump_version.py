#!/usr/bin/env python
"""Bump every published package to the same version.

    uv run python scripts/bump_version.py 0.2.0

All five distributions release together because they inter-depend: a `proofstep-cli` resolving an
older `proofstep-core` would break the guarantee that the CLI's exit code and the server's verdict
come from identical code. Bumping them by hand is how one gets forgotten.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PACKAGES = (
    "packages/shared-types",
    "packages/evaluation-core",
    "packages/trajectory-engine",
    "packages/python-sdk",
    "packages/cli",
)

#: PEP 440, loosely — enough to catch a typo like `v0.2.0` or `0.2`, not to re-implement the spec.
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[abc]\d+|\.dev\d+|rc\d+)?$")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    version = sys.argv[1]
    if not VERSION_PATTERN.match(version):
        print(f"{version!r} does not look like a version. Expected e.g. 0.2.0", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parent.parent
    for package in PACKAGES:
        path = root / package / "pyproject.toml"
        text = path.read_text()
        updated, count = re.subn(
            r'^version = "[^"]+"', f'version = "{version}"', text, count=1, flags=re.M
        )
        if count != 1:
            print(f"could not find a version line in {path}", file=sys.stderr)
            return 1
        path.write_text(updated)
        print(f"{package} → {version}")

    print(
        f"\nNow:\n  git commit -am 'Release {version}'\n"
        f"  git tag v{version}\n  git push origin main --tags"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

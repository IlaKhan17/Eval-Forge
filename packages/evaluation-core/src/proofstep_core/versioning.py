"""Evaluator identity: the config hash that mints a version.

An evaluator's version is a hash of everything that could change a score. That is what
makes "re-calibrate whenever the rubric, judge model, or judge params change" automatic
rather than a thing someone has to remember: change any of them and the hash changes, so
the old calibration no longer applies to the new evaluator.

This lives in the pure core, not in the API, because **both sides must compute it
identically**. The server stores a calibration against a hash and the CLI decides locally
whether that calibration is stale; two implementations of the same hash would drift, and
the failure would be silent acceptance of a calibration for a judge that no longer
exists. The API delegates here (`proofstep_api.services.datasets.config_hash`).

The judge model is deliberately part of the hash. A provider that silently upgrades the
model behind an alias invalidates every historical number, and pinning the version string
inside the evaluator's identity is the only defence available from outside the provider.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Bumped only when the hashing scheme itself changes, which invalidates every stored
#: version. Included in the digest so an old hash can never collide with a new one.
SCHEME = "v1"


def canonical_config(config: dict[str, Any]) -> str:
    """Deterministic serialization of an evaluator's configuration.

    Sorted keys and no whitespace, so two logically identical configs hash the same
    however they were written — a suite reformatted by an editor must not mint a new
    evaluator version and invalidate its calibration.

    Keys whose value is `None` are dropped, because an unset option and an option
    explicitly set to its default-of-nothing are the same evaluator. Without this, adding
    `seed: null` to a YAML file would silently invalidate a calibration.
    """
    pruned = {key: value for key, value in config.items() if value is not None}
    return json.dumps(
        {"scheme": SCHEME, "config": pruned},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def config_digest(config: dict[str, Any]) -> bytes:
    """SHA-256 of the canonical config."""
    return hashlib.sha256(canonical_config(config).encode("utf-8")).digest()


def config_hash(config: dict[str, Any]) -> str:
    """Hex digest, truncated to 16 characters for display and storage keys.

    Sixteen hex characters is 64 bits — ample against accidental collision among the
    handful of evaluator versions a project will ever have, and short enough to appear in
    a CI message without wrapping.
    """
    return config_digest(config).hex()[:16]

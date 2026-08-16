"""Redaction, re-exported from `proofstep_core`.

The pipeline used to live here, in the client SDK. That was the wrong home the moment the server
started using it too: redaction has to produce *identical* output on both sides — a field the SDK
strips and the server keeps is a secret that leaks — and the only way to guarantee that is one
implementation, not two that agree today. But a server importing its own client SDK is backwards,
and it pulled `httpx` into the API image to get a module that needs nothing but `re` and `math`.

So it now lives in `proofstep_core`, which both sides already depend on and which is a pure library
by contract (see `.importlinter`). This module stays as a re-export because
`from proofstep.redaction import RedactionPipeline` is a documented import that users have written.
"""

from __future__ import annotations

from proofstep_core import redaction as _redaction
from proofstep_core.redaction import *  # noqa: F403

# `import *` skips anything the source module did not name in `__all__`, and it does not carry the
# module's own `__all__` across. Deriving it here means this shim exports exactly what the real
# module exports, and keeps doing so when a name is added there.
__all__ = [name for name in vars(_redaction) if not name.startswith("_")]

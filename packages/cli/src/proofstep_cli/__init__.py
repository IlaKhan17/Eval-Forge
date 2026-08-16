from importlib import metadata as _metadata

"""Proofstep CLI."""

# Read from the installed distribution rather than written here twice. A hand-maintained
# copy drifts the first time a release bumps one and not the other — which it already did,
# reporting 0.1.0.dev0 from a 0.1.0 wheel.
__version__ = _metadata.version("proofstep-cli")

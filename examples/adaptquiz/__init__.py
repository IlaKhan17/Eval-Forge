"""AdaptQuiz — a reference adaptive-learning system, used as an EvalForge example.

**An example, not a platform feature**; see `examples/davis_sdr/__init__.py` for why that
distinction is enforced by a CI check rather than by discipline.

The interesting contrast with Davis is how *little* of this domain needs a judge. Document
ingestion is a parsing problem with ground truth, and adaptive learning is ordinary
supervised-learning evaluation. Between them they are the clearest illustration in the
project of where an LLM judge is the wrong instrument.
"""

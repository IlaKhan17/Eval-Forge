"""The credential corpus.

This test is the actual guarantee; the deny-list is merely an implementation of it.
A synthetic credential of each common shape is pushed through the SDK in every
capture mode, and asserted absent from the exported bytes.

Every credential below is synthetic and non-functional. The riskier shapes are
assembled from fragments at run time rather than written as literals: GitHub's own
push protection flagged the Slack and Stripe entries when they were inline, which is
a fair signal that the corpus is realistic — but a test fixture must never look like
a leaked key to a scanner.
"""

from __future__ import annotations

from typing import Any

import pytest
from doubles import RecordingTransport

from proofstep import redaction
from proofstep.client import Client
from proofstep.config import Config
from proofstep_types import CaptureMode

# (label, value) — one per credential shape we claim to catch.
CREDENTIALS = [
    ("openai", "sk-" + "NOTAREALKEY" + "0" * 29),
    ("anthropic", "sk-ant-" + "api03-" + "NOTAREALKEY" + "0" * 29),
    ("github_pat", "ghp_" + "NOTAREALTOKEN" + "0" * 23),
    ("github_fine", "github_" + "pat_" + "NOTAREALTOKEN" + "0" * 23),
    ("aws", "AKIA" + "NOTAREALKEY000000"[:16]),
    ("google", "AIza" + "NOTAREALKEY" + "0" * 24),
    ("slack", "xoxb-" + "1" * 12 + "-" + "2" * 13 + "-" + "NOTAREALTOKEN" + "z" * 11),
    ("stripe", "sk_" + "live_" + "NOTAREALKEY" + "0" * 13),
    ("proofstep", "ps_prod_a1b2_abcdefghijklmnopqrstuvwxyz012345"),
    (
        "jwt",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk",
    ),
    ("bearer", "Bearer abcdefghijklmnopqrstuvwxyz0123456789"),
    ("pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"),
    ("high_entropy", "Zm9vYmFyYmF6cXV4Y29ycmdlZ3JhdWx0Z2FycGx5"),
]

SECRET_KEYS = [
    "api_key",
    "apiKey",
    "authorization",
    "password",
    "access_token",
    "refresh_token",
    "session_id",
    "client_secret",
    "private_key",
    "cookie",
]


def export(payload: dict[str, Any], *, capture_mode: CaptureMode = CaptureMode.REDACTED) -> str:
    """Run a payload through the SDK and return the raw exported bytes as text."""
    transport = RecordingTransport()
    client = Client(
        Config(
            project="p",
            api_key="k",
            export=True,
            capture_mode=capture_mode,
            flush_interval_s=0.01,
        ),
        transport=transport,
    )
    with client.trace("t") as trace:
        with client.span("s", span_type="tool", tool_name="call") as span:
            span.set_input(payload)
            span.set_output(payload)
            span.set_args(payload)
            span.set_attributes(**payload)
        trace.set_metadata(**payload)
    client.flush(2.0)
    text: str = transport.decoded()
    client.shutdown(0.1)
    return text


class TestCredentialCorpus:
    @pytest.mark.parametrize(("label", "value"), CREDENTIALS, ids=[c[0] for c in CREDENTIALS])
    def test_credential_never_reaches_the_wire(self, label: str, value: str) -> None:
        exported = export({"note": f"the token is {value} keep it safe"})
        assert value not in exported, f"{label} credential survived export"

    @pytest.mark.parametrize(("label", "value"), CREDENTIALS, ids=[c[0] for c in CREDENTIALS])
    def test_credential_is_redacted_even_in_full_capture_mode(self, label: str, value: str) -> None:
        """`full` means full payloads, not full credentials.

        There is no configuration in which Proofstep intentionally stores a secret.
        """
        exported = export({"note": value}, capture_mode=CaptureMode.FULL)
        assert value not in exported, f"{label} survived in full capture mode"

    @pytest.mark.parametrize("key", SECRET_KEYS)
    def test_secret_named_keys_are_redacted_whatever_the_value(self, key: str) -> None:
        exported = export({key: "some-ordinary-looking-value-123"})
        assert "some-ordinary-looking-value-123" not in exported

    def test_credentials_nested_deep_inside_structures_are_caught(self) -> None:
        secret = "sk-" + "NOTAREALKEY" + "0" * 29
        payload = {"a": [{"b": {"c": [{"headers": {"authorization": secret}}]}}]}
        assert secret not in export(payload)

    def test_the_surrounding_text_survives_redaction(self) -> None:
        """Substitute, don't discard: an error message minus its token is still useful."""
        secret = "sk-" + "NOTAREALKEY" + "0" * 29
        exported = export({"note": f"auth failed for {secret}"})
        assert "auth failed for" in exported
        assert "[REDACTED:" in exported


class TestCaptureModes:
    def test_metadata_only_sends_no_payloads_at_all(self) -> None:
        exported = export(
            {"secret_business_data": "acme merger"}, capture_mode=CaptureMode.METADATA_ONLY
        )
        assert "acme merger" not in exported

    def test_metadata_only_still_sends_structure(self) -> None:
        exported = export({"x": "y"}, capture_mode=CaptureMode.METADATA_ONLY)
        assert '"name": "s"' in exported
        assert '"tool_name": "call"' in exported

    def test_disabled_records_nothing(self) -> None:
        assert export({"x": "y"}, capture_mode=CaptureMode.DISABLED) == ""

    def test_redacted_keeps_ordinary_payloads(self) -> None:
        exported = export({"body": "Congratulations on the Series B"})
        assert "Congratulations on the Series B" in exported


class TestRedactionCounting:
    def test_redactions_are_counted_on_the_span(self) -> None:
        client = Client(Config(project="p", export=False))
        with client.trace("t") as trace, client.span("s") as span:
            span.set_input({"api_key": "x", "safe": "y"})
        assert trace.snapshot().spans[0].redaction_count >= 1

    def test_a_clean_payload_counts_zero(self) -> None:
        client = Client(Config(project="p", export=False))
        with client.trace("t") as trace, client.span("s") as span:
            span.set_input({"body": "hello", "count": 3})
        assert trace.snapshot().spans[0].redaction_count == 0


class TestEntropyHeuristic:
    def test_ordinary_prose_is_not_flagged(self) -> None:
        """The heuristic must not eat model output, which is the point of the trace."""
        prose = (
            "Thank you for reaching out about the platform. I would be glad to "
            "walk through how the integration works next week."
        )
        assert not redaction.looks_like_a_secret(prose)

    def test_a_dense_random_blob_is_flagged(self) -> None:
        assert redaction.looks_like_a_secret("aB3dEf7hIj9kLm2nOp5qRs8tUv1wXy4zAb6cDe0f")

    def test_short_strings_are_never_flagged(self) -> None:
        assert not redaction.looks_like_a_secret("aB3dEf7h")

    def test_a_uuid_is_not_flagged(self) -> None:
        """Common, harmless, and dense enough to trip a naive threshold."""
        assert not redaction.looks_like_a_secret("550e8400-e29b-41d4-a716-446655440000")


class TestCustomRedactors:
    def test_configured_keys_are_redacted(self) -> None:
        client = Client(Config(project="p", export=False, redact_keys=["prospect_email"]))
        with client.trace("t") as trace, client.span("s") as span:
            span.set_input({"prospect_email": "someone@acme.com", "company": "Acme"})
        payload = trace.snapshot().spans[0].input
        assert payload["prospect_email"].startswith("[REDACTED:")
        assert payload["company"] == "Acme"

    def test_a_regex_redactor_substitutes(self) -> None:
        pipeline = redaction.RedactionPipeline(
            [redaction.regex(r"CUST-\d{8}", replacement="[CUSTOMER_ID]")]
        )
        assert pipeline.apply({"note": "see CUST-12345678"}) == {"note": "see [CUSTOMER_ID]"}


class TestLimits:
    def test_an_oversized_field_is_truncated_with_a_marker(self) -> None:
        pipeline = redaction.RedactionPipeline(max_field_bytes=100)
        result = pipeline.apply({"big": "x" * 5000})
        assert result["big"]["_truncated"] is True
        assert result["big"]["_original_bytes"] == 5000

    def test_deep_nesting_is_bounded(self) -> None:
        payload: dict[str, Any] = {"v": 1}
        for _ in range(50):
            payload = {"nested": payload}
        assert "TRUNCATED:depth" in str(redaction.RedactionPipeline().apply(payload))

    def test_huge_lists_are_bounded(self) -> None:
        result = redaction.RedactionPipeline().apply({"items": list(range(5000))})
        assert len(result["items"]) <= redaction.MAX_ITEMS + 1

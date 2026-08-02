"""Exporter, sampling, and cross-service propagation."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from conftest import RecordingTransport

import evalforge
from evalforge.client import Client, sampled
from evalforge.config import Config
from evalforge.exporter import Exporter


def exporting_client(transport: RecordingTransport, **overrides: object) -> Client:
    settings: dict[str, object] = {
        "project": "p",
        "api_key": "k",
        "export": True,
        "flush_interval_s": 0.01,
    }
    settings.update(overrides)
    return Client(Config(**settings), transport=transport)  # type: ignore[arg-type]


class TestBatching:
    def test_a_trace_is_exported_after_flush(self) -> None:
        transport = RecordingTransport()
        client = exporting_client(transport)
        with client.trace("t"), client.span("s"):
            pass
        assert client.flush(2.0)
        payloads = transport.payloads()
        assert len(payloads) == 1
        assert payloads[0]["traces"][0]["name"] == "t"
        assert payloads[0]["spans"][0]["name"] == "s"
        client.shutdown(0.1)

    def test_the_body_is_gzipped_json(self) -> None:
        transport = RecordingTransport()
        client = exporting_client(transport)
        with client.trace("t"):
            pass
        client.flush(2.0)
        assert json.loads(gzip.decompress(transport.bodies[0]))["resource"]["environment"]
        client.shutdown(0.1)

    def test_multiple_traces_share_a_batch(self) -> None:
        transport = RecordingTransport()
        client = exporting_client(transport, flush_interval_s=0.5)
        for i in range(5):
            with client.trace(f"t{i}"):
                pass
        client.flush(3.0)
        total = sum(len(p["traces"]) for p in transport.payloads())
        assert total == 5
        client.shutdown(0.1)

    def test_resource_metadata_is_attached(self) -> None:
        transport = RecordingTransport()
        client = exporting_client(transport, service_name="svc", environment="staging")
        with client.trace("t"):
            pass
        client.flush(2.0)
        resource = transport.payloads()[0]["resource"]
        assert resource["service.name"] == "svc"
        assert resource["environment"] == "staging"
        client.shutdown(0.1)


class TestRetries:
    def test_a_transient_failure_is_retried_then_succeeds(self) -> None:
        transport = RecordingTransport(fail_times=2)
        client = exporting_client(transport, max_retries=5)
        with client.trace("t"):
            pass
        client.flush(5.0)
        assert len(transport.bodies) == 1
        assert transport.attempts == 3
        client.shutdown(0.1)

    def test_persistent_failure_gives_up_without_raising(self) -> None:
        transport = RecordingTransport(always_fail=True)
        client = exporting_client(transport, max_retries=2)
        with client.trace("t"):
            pass
        client.flush(3.0)
        assert transport.bodies == []
        assert client.exporter.stats.failures > 0
        client.shutdown(0.1)

    def test_spooling_preserves_a_batch_the_api_refused(self, tmp_path: Path) -> None:
        transport = RecordingTransport(always_fail=True)
        client = exporting_client(transport, max_retries=1, spool_dir=tmp_path)
        with client.trace("t"):
            pass
        client.flush(3.0)
        spooled = list(tmp_path.glob("*.json.gz"))
        assert len(spooled) == 1
        assert json.loads(gzip.decompress(spooled[0].read_bytes()))["traces"][0]["name"] == "t"
        client.shutdown(0.1)


class TestExportSuppression:
    def test_nothing_is_sent_without_an_api_key(self) -> None:
        transport = RecordingTransport()
        client = Client(Config(project="p", api_key=None, export=True), transport=transport)
        with client.trace("t"):
            pass
        client.flush(0.5)
        assert transport.bodies == []

    def test_export_false_records_but_does_not_send(self) -> None:
        transport = RecordingTransport()
        client = Client(Config(project="p", api_key="k", export=False), transport=transport)
        with client.trace("t") as trace, client.span("s"):
            pass
        client.flush(0.5)
        assert transport.bodies == []
        assert len(trace.snapshot().spans) == 1  # still recorded locally

    def test_an_oversized_batch_is_dropped_with_a_log_not_a_crash(self) -> None:
        transport = RecordingTransport()
        client = exporting_client(transport, max_batch_bytes=200)
        with client.trace("t") as trace, client.span("s") as span:
            span.set_input({"big": "x" * 10_000})
            trace.set_metadata(more="y" * 10_000)
        client.flush(2.0)
        assert transport.bodies == []
        client.shutdown(0.1)


class TestSampling:
    def test_full_rate_keeps_everything(self) -> None:
        assert all(sampled(f"{i:032x}", 1.0) for i in range(100))

    def test_zero_rate_keeps_nothing(self) -> None:
        assert not any(sampled(f"{i:032x}", 0.0) for i in range(100))

    def test_sampling_is_deterministic_for_a_trace_id(self) -> None:
        """A trace must be captured whole or not at all.

        A half-recorded trajectory is worse than none: the policy engine reads the
        gaps as evidence.
        """
        trace_id = "3f" * 16
        assert len({sampled(trace_id, 0.5) for _ in range(50)}) == 1

    def test_the_rate_is_roughly_honoured(self) -> None:
        import secrets

        ids = [secrets.token_hex(16) for _ in range(4000)]
        kept = sum(sampled(i, 0.25) for i in ids)
        assert 0.20 < kept / len(ids) < 0.30

    def test_an_unsampled_trace_is_not_exported(self) -> None:
        transport = RecordingTransport()
        client = exporting_client(transport, sample_rate=0.0)
        with client.trace("t"):
            pass
        client.flush(0.5)
        assert transport.bodies == []
        client.shutdown(0.1)

    def test_errors_are_kept_even_when_unsampled(self) -> None:
        """The traces you most want are the ones sampling is most likely to discard."""
        transport = RecordingTransport()
        client = exporting_client(transport, sample_rate=0.0, always_sample_on_error=True)
        with pytest.raises(ValueError, match="boom"), client.trace("t"):
            msg = "boom"
            raise ValueError(msg)
        client.flush(2.0)
        assert len(transport.bodies) == 1
        client.shutdown(0.1)


class TestTraceContextPropagation:
    def test_inject_emits_a_w3c_traceparent(self, client: Client) -> None:
        evalforge._client = client
        with client.trace("t") as trace, client.span("s") as span:
            headers = evalforge.inject({"content-type": "application/json"})

        assert headers["content-type"] == "application/json"
        assert headers["traceparent"] == f"00-{trace.trace_id}-{span.span_id}-01"

    def test_inject_is_a_noop_outside_a_span(self, client: Client) -> None:
        evalforge._client = client
        assert evalforge.inject({}) == {}

    def test_extract_parses_a_valid_header(self) -> None:
        trace_id, span_id = "a" * 32, "b" * 16
        assert evalforge.extract({"traceparent": f"00-{trace_id}-{span_id}-01"}) == (
            trace_id,
            span_id,
            True,
        )

    def test_extract_is_case_insensitive(self) -> None:
        assert evalforge.extract({"TraceParent": f"00-{'a' * 32}-{'b' * 16}-00"}) is not None

    @pytest.mark.parametrize(
        "header",
        [
            "garbage",
            "00-tooshort-abcdefabcdef0000-01",
            f"99-{'a' * 32}-{'b' * 16}-01",
            f"00-{'0' * 32}-{'b' * 16}-01",  # all-zero trace id is invalid
            f"00-{'a' * 32}-{'0' * 16}-01",
            "",
        ],
    )
    def test_malformed_headers_are_rejected(self, header: str) -> None:
        """A caller-supplied header is untrusted and must not corrupt our ids."""
        assert evalforge.extract({"traceparent": header}) is None

    def test_a_round_trip_preserves_the_ids(self, client: Client) -> None:
        evalforge._client = client
        with client.trace("t") as trace, client.span("s") as span:
            extracted = evalforge.extract(evalforge.inject({}))
        assert extracted == (trace.trace_id, span.span_id, True)

    def test_an_extracted_context_continues_the_same_trace(self, client: Client) -> None:
        upstream = "c" * 32
        with client.trace("downstream", trace_id=upstream) as trace, client.span("s") as span:
            assert span.trace_id == upstream
        assert trace.snapshot().trace_id == upstream


class TestStats:
    def test_counters_are_reported(self) -> None:
        transport = RecordingTransport()
        client = exporting_client(transport)
        with client.trace("t"), client.span("s"):
            pass
        client.flush(2.0)
        stats = client.exporter.stats.as_dict()
        assert stats["exported_traces"] == 1
        assert stats["exported_spans"] == 1
        assert stats["dropped_traces"] == 0
        client.shutdown(0.1)

    def test_shutdown_is_idempotent(self) -> None:
        exporter = Exporter(Config(project="p", api_key="k"), transport=RecordingTransport())
        exporter.shutdown(0.1)
        exporter.shutdown(0.1)

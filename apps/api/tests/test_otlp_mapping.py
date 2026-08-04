"""The OTLP attribute mapping and wire decoding.

Unit tests against literal attribute dicts, which is the only sensible way to test a
mapping table: every bug in one is "this field silently ended up somewhere else", and that
is invisible from an end-to-end test that only checks a span arrived.

No database, no HTTP. The round-trip through the real endpoint is in
`test_otlp_receiver.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from evalforge_api.otlp.decode import JSON_CONTENT_TYPE, OtlpDecodeError, decode, kv
from evalforge_api.otlp.mapping import map_span, span_type_for, status_for, trace_fields
from evalforge_api.otlp.receiver import translate
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.trace.v1.trace_pb2 import Span as PbSpan
from opentelemetry.proto.trace.v1.trace_pb2 import Status

TRACE_ID = bytes.fromhex("4bf92f3577b34da6a3ce929d0e0e4736")
SPAN_ID = bytes.fromhex("00f067aa0ba902b7")
PARENT_ID = bytes.fromhex("00f067aa0ba902b8")
BASE_NANOS = 1_767_225_600_000_000_000  # 2026-01-01T00:00:00Z


def build_request(
    spans: list[PbSpan],
    *,
    resource: dict[str, object] | None = None,
    scope_name: str = "openinference.instrumentation.langchain",
    scope_version: str = "0.1.0",
) -> ExportTraceServiceRequest:
    request = ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    for key, value in (resource or {"service.name": "sdr-agent"}).items():
        resource_spans.resource.attributes.append(kv(key, value))
    scope_spans = resource_spans.scope_spans.add()
    scope_spans.scope.name = scope_name
    scope_spans.scope.version = scope_version
    scope_spans.spans.extend(spans)
    return request


def build_span(
    *,
    name: str = "ChatAnthropic",
    span_id: bytes = SPAN_ID,
    parent: bytes | None = None,
    trace_id: bytes = TRACE_ID,
    attributes: dict[str, object] | None = None,
    start_nanos: int = BASE_NANOS,
    duration_nanos: int = 250_000_000,
    status_code: int = 0,
    status_message: str = "",
) -> PbSpan:
    span = PbSpan(
        trace_id=trace_id,
        span_id=span_id,
        name=name,
        kind=PbSpan.SpanKind.SPAN_KIND_INTERNAL,
        start_time_unix_nano=start_nanos,
        end_time_unix_nano=start_nanos + duration_nanos,
        status=Status(code=status_code, message=status_message),  # type: ignore[arg-type]
    )
    if parent is not None:
        span.parent_span_id = parent
    for key, value in (attributes or {}).items():
        span.attributes.append(kv(key, value))
    return span


class TestSpanType:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("LLM", "llm"),
            ("llm", "llm"),
            ("CHAIN", "workflow"),
            ("TOOL", "tool"),
            ("RETRIEVER", "retriever"),
            ("EMBEDDING", "embedding"),
            ("AGENT", "agent"),
            ("GUARDRAIL", "guardrail"),
            ("EVALUATOR", "evaluator"),
            ("UNKNOWN", "custom"),
        ],
    )
    def test_openinference_kinds(self, kind: str, expected: str) -> None:
        assert span_type_for({"openinference.span.kind": kind}) == expected

    def test_a_reranker_is_retrieval_not_a_model_call(self) -> None:
        # It has no tokens and no cost. Mapping it to `llm` would put it in the columns a
        # cost dashboard sums, contributing zeroes that look like free model calls.
        assert span_type_for({"openinference.span.kind": "RERANKER"}) == "retriever"

    @pytest.mark.parametrize(
        ("operation", "expected"),
        [
            ("chat", "llm"),
            ("text_completion", "llm"),
            ("embeddings", "embedding"),
            ("execute_tool", "tool"),
            ("invoke_agent", "agent"),
        ],
    )
    def test_genai_operations(self, operation: str, expected: str) -> None:
        assert span_type_for({"gen_ai.operation.name": operation}) == expected

    def test_openinference_wins_over_genai(self) -> None:
        # A span carrying both is almost always OpenInference instrumentation that picked up
        # a GenAI-shaped attribute from a library underneath it. The more specific producer
        # is the one to trust.
        attributes = {"openinference.span.kind": "TOOL", "gen_ai.operation.name": "chat"}
        assert span_type_for(attributes) == "tool"

    def test_token_counts_imply_a_model_call(self) -> None:
        # A span with tokens is an LLM span whatever it calls itself.
        assert span_type_for({"llm.token_count.prompt": 10}) == "llm"
        assert span_type_for({"gen_ai.usage.output_tokens": 4}) == "llm"

    def test_a_tool_name_implies_a_tool(self) -> None:
        assert span_type_for({"tool.name": "gmail.send"}) == "tool"

    def test_a_workflow_is_inferred_from_a_narrow_set_of_prefixes(self) -> None:
        assert span_type_for({}, span_name="langgraph.invoke") == "workflow"
        assert span_type_for({}, span_name="chain.run") == "workflow"

    def test_an_unrecognised_span_stays_custom(self) -> None:
        # Guessing harder would mislabel spans, and a wrong span_type is worse than
        # `custom`: trajectory policies filter on it, so a mislabelled span can silently
        # drop out of a safety rule's scope.
        assert span_type_for({"http.method": "GET"}, span_name="GET /health") == "custom"


class TestStatus:
    def test_error_is_error(self) -> None:
        assert status_for("STATUS_CODE_ERROR") == "error"

    def test_unset_is_ok(self) -> None:
        # A deliberate reading of the spec. Unset is the default for a span that finished
        # without the instrumentation saying anything, and almost all of those succeeded.
        # Recording them as `unset` would bury real errors in a sea of unknowns and make the
        # error rate meaningless.
        assert status_for("STATUS_CODE_UNSET") == "ok"

    def test_an_exception_event_overrides_an_unset_status(self) -> None:
        # A span that recorded an exception and left its status unset is an instrumentation
        # gap, not a success.
        assert status_for("STATUS_CODE_UNSET", has_exception=True) == "error"


class TestModelAndTokens:
    def test_openinference_llm_attributes(self) -> None:
        mapped = map_span(
            {
                "openinference.span.kind": "LLM",
                "llm.model_name": "claude-sonnet-5",
                "llm.provider": "anthropic",
                "llm.token_count.prompt": 120,
                "llm.token_count.completion": 45,
                "llm.token_count.total": 165,
            }
        )
        assert mapped.span_type == "llm"
        assert mapped.model == "claude-sonnet-5"
        assert mapped.provider == "anthropic"
        assert (mapped.prompt_tokens, mapped.completion_tokens, mapped.total_tokens) == (
            120,
            45,
            165,
        )

    def test_genai_semconv_attributes(self) -> None:
        mapped = map_span(
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.system": "openai",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.input_tokens": 30,
                "gen_ai.usage.output_tokens": 8,
            }
        )
        assert mapped.provider == "openai"
        assert mapped.model == "gpt-4o"
        assert mapped.total_tokens == 38

    def test_the_response_model_beats_the_requested_one(self) -> None:
        # What actually answered is what the cost and the comparison should be attributed to.
        mapped = map_span(
            {"gen_ai.request.model": "gpt-4o", "gen_ai.response.model": "gpt-4o-2024-11-20"}
        )
        assert mapped.model == "gpt-4o-2024-11-20"

    def test_the_older_prompt_tokens_spelling_is_still_read(self) -> None:
        # Released instrumentation still emits it; ignoring it would report zero tokens for
        # a real model call.
        mapped = map_span({"gen_ai.usage.prompt_tokens": 11, "gen_ai.usage.completion_tokens": 2})
        assert mapped.total_tokens == 13

    def test_an_understated_total_is_recomputed(self) -> None:
        # Trusting a declared total blindly lets a broken exporter report 0 for a span that
        # clearly used tokens, and cost dashboards are built on this number.
        mapped = map_span(
            {
                "llm.token_count.prompt": 100,
                "llm.token_count.completion": 20,
                "llm.token_count.total": 0,
            }
        )
        assert mapped.total_tokens == 120

    def test_a_larger_declared_total_is_kept(self) -> None:
        # Cached and reasoning tokens are real and are not in prompt+completion.
        mapped = map_span(
            {
                "llm.token_count.prompt": 100,
                "llm.token_count.completion": 20,
                "llm.token_count.total": 500,
            }
        )
        assert mapped.total_tokens == 500

    def test_tokens_sent_as_doubles_are_accepted(self) -> None:
        # OTLP's attribute type is a union and the choice is the exporter's.
        mapped = map_span({"llm.token_count.prompt": 12.0})
        assert mapped.prompt_tokens == 12

    def test_a_garbage_token_count_becomes_zero_not_an_error(self) -> None:
        mapped = map_span({"llm.token_count.prompt": "lots"})
        assert mapped.prompt_tokens == 0

    def test_an_empty_model_name_does_not_shadow_a_populated_one(self) -> None:
        # `llm.model_name: ""` means "I do not know". Letting it win would lose the model.
        mapped = map_span({"llm.model_name": "", "gen_ai.request.model": "claude-opus-5"})
        assert mapped.model == "claude-opus-5"

    def test_cost_is_read_and_a_negative_one_refused(self) -> None:
        assert map_span({"llm.cost.total": "0.0042"}).cost == Decimal("0.0042")
        # A negative cost is meaningless and would corrupt a project's spend total.
        assert map_span({"llm.cost.total": "-1"}).cost is None

    def test_a_long_model_name_is_truncated_not_rejected(self) -> None:
        mapped = map_span({"llm.model_name": "m" * 500})
        assert mapped.model is not None
        assert len(mapped.model) == 200


class TestPayloads:
    def test_a_json_payload_is_parsed_into_structure(self) -> None:
        # Not cosmetic: trajectory predicates and deterministic evaluators resolve paths like
        # `output.intent`, and a JSON *string* has no paths inside it.
        mapped = map_span(
            {
                "openinference.span.kind": "LLM",
                "output.value": '{"intent": "unsubscribe"}',
                "output.mime_type": "application/json",
            }
        )
        assert mapped.output == {"intent": "unsubscribe"}

    def test_plain_text_stays_a_string(self) -> None:
        mapped = map_span(
            {"output.value": "Sounds good, Thursday works", "output.mime_type": "text/plain"}
        )
        assert mapped.output == "Sounds good, Thursday works"

    def test_json_shaped_text_is_parsed_without_a_mime_type(self) -> None:
        mapped = map_span({"input.value": '{"body": "hello"}'})
        assert mapped.input == {"body": "hello"}

    def test_a_bare_scalar_is_not_coerced(self) -> None:
        # Parsing every string would turn the output "42" into an integer and "null" into
        # None — a silent type change in data an evaluator compares against.
        assert map_span({"output.value": "42"}).output == "42"
        assert map_span({"output.value": "null"}).output == "null"

    def test_unparseable_json_keeps_the_original_text(self) -> None:
        # Guessing that broken text was meant to be structured is worse than showing what
        # actually arrived.
        mapped = map_span({"output.value": "{not json", "output.mime_type": "application/json"})
        assert mapped.output == "{not json"

    def test_tool_arguments_come_from_input_value_on_a_tool_span(self) -> None:
        mapped = map_span(
            {
                "openinference.span.kind": "TOOL",
                "tool.name": "gmail.send",
                "input.value": '{"to": "a@b.c"}',
            }
        )
        assert mapped.tool_name == "gmail.send"
        assert mapped.tool_args == {"to": "a@b.c"}

    def test_an_llm_prompt_is_not_filed_as_tool_arguments(self) -> None:
        mapped = map_span({"openinference.span.kind": "LLM", "input.value": '{"messages": []}'})
        assert mapped.tool_args is None
        assert mapped.input == {"messages": []}

    def test_non_object_tool_arguments_are_wrapped_not_dropped(self) -> None:
        mapped = map_span({"openinference.span.kind": "TOOL", "tool.parameters": "[1, 2]"})
        assert mapped.tool_args == {"value": [1, 2]}


class TestLosslessness:
    def test_unmapped_attributes_survive_verbatim(self) -> None:
        # A receiver that silently drops the attribute someone needs is worse than one that
        # stores too much: the storage is cheap and the debugging session is not.
        mapped = map_span(
            {
                "openinference.span.kind": "LLM",
                "llm.model_name": "claude-sonnet-5",
                "llm.invocation_parameters": '{"temperature": 0}',
                "my.company.tenant": "acme",
                "http.status_code": 200,
            }
        )
        assert mapped.attributes["my.company.tenant"] == "acme"
        assert mapped.attributes["http.status_code"] == 200
        assert mapped.attributes["llm.invocation_parameters"] == '{"temperature": 0}'

    def test_a_promoted_attribute_is_not_stored_twice(self) -> None:
        mapped = map_span({"llm.model_name": "m", "llm.token_count.prompt": 3})
        assert "llm.model_name" not in mapped.attributes
        assert "llm.token_count.prompt" not in mapped.attributes

    def test_every_spelling_in_a_group_is_consumed(self) -> None:
        # Leaving the losing spellings behind would store the same fact twice, and a reader
        # comparing them would reasonably conclude they disagree.
        mapped = map_span({"llm.model_name": "winner", "gen_ai.request.model": "loser"})
        assert mapped.model == "winner"
        assert "gen_ai.request.model" not in mapped.attributes


class TestResource:
    def test_service_environment_and_commit(self) -> None:
        fields = trace_fields(
            {
                "service.name": "sdr-agent",
                "deployment.environment.name": "production",
                "git.commit": "abc123",
            }
        )
        assert fields == {
            "service_name": "sdr-agent",
            "environment": "production",
            "git_commit": "abc123",
        }

    def test_the_older_environment_key_still_works(self) -> None:
        assert trace_fields({"deployment.environment": "staging"})["environment"] == "staging"


class TestDecode:
    def test_protobuf_round_trip(self) -> None:
        request = build_request([build_span(attributes={"llm.model_name": "claude-sonnet-5"})])
        scopes = decode(request.SerializeToString(), "application/x-protobuf")

        assert len(scopes) == 1
        assert scopes[0].resource["service.name"] == "sdr-agent"
        assert scopes[0].scope_name == "openinference.instrumentation.langchain"
        span = scopes[0].spans[0]
        assert span.trace_id == TRACE_ID.hex()
        assert span.span_id == SPAN_ID.hex()
        assert span.attributes["llm.model_name"] == "claude-sonnet-5"

    def test_json_round_trip_produces_the_same_result(self) -> None:
        # Both encodings must agree. A divergence here means the same application reports
        # differently depending on a transport setting it did not think was semantic.
        request = build_request([build_span(attributes={"llm.token_count.prompt": 7})])
        from_proto = decode(request.SerializeToString(), "application/x-protobuf")
        from_json = decode(
            json.dumps(MessageToDict(request, preserving_proto_field_name=True)).encode(),
            JSON_CONTENT_TYPE,
        )
        assert from_proto == from_json

    def test_an_unknown_content_type_is_treated_as_protobuf(self) -> None:
        # Collectors send `application/x-protobuf`, `application/protobuf`, and occasionally
        # nothing. Rejecting those would be pedantry with a real cost.
        request = build_request([build_span()])
        assert decode(request.SerializeToString(), "application/protobuf")
        assert decode(request.SerializeToString(), "")

    def test_a_malformed_json_body_raises(self) -> None:
        with pytest.raises(OtlpDecodeError, match="not valid JSON"):
            decode(b"{oops", JSON_CONTENT_TYPE)

    def test_a_json_body_sent_as_protobuf_raises(self) -> None:
        with pytest.raises(OtlpDecodeError, match="not a valid OTLP protobuf"):
            decode(b'{"resourceSpans": []}' * 20, "application/x-protobuf")

    def test_an_unknown_json_field_does_not_lose_the_batch(self) -> None:
        # A newer collector must not be able to discard a batch by adding a field.
        body = json.dumps(
            {
                "resourceSpans": [
                    {
                        "resource": {"attributes": []},
                        "somethingNew": 1,
                        "scopeSpans": [{"scope": {"name": "x"}, "spans": []}],
                    }
                ]
            }
        ).encode()
        assert decode(body, JSON_CONTENT_TYPE)[0].scope_name == "x"

    def test_timestamps_become_aware_datetimes(self) -> None:
        span = decode(build_request([build_span()]).SerializeToString(), "")[0].spans[0]
        assert span.start == datetime(2026, 1, 1, tzinfo=UTC)
        assert span.end == datetime(2026, 1, 1, 0, 0, 0, 250_000, tzinfo=UTC)

    def test_an_unfinished_span_has_no_end(self) -> None:
        proto = build_span()
        proto.end_time_unix_nano = 0
        assert decode(build_request([proto]).SerializeToString(), "")[0].spans[0].end is None

    def test_an_absurd_timestamp_does_not_kill_the_batch(self) -> None:
        # A broken exporter must not be able to discard the valid spans beside it.
        proto = build_span(start_nanos=2**63 - 1)
        span = decode(build_request([proto]).SerializeToString(), "")[0].spans[0]
        assert span.start.year >= 1970

    def test_an_all_zero_parent_is_treated_as_absent(self) -> None:
        # Some exporters set the field rather than leaving it empty. Kept as-is, the span
        # would be an orphan pointing at a span that cannot exist.
        proto = build_span(parent=bytes(8))
        assert (
            decode(build_request([proto]).SerializeToString(), "")[0].spans[0].parent_span_id
            is None
        )

    def test_attribute_value_types(self) -> None:
        proto = build_span(attributes={"s": "text", "i": 3, "f": 1.5, "b": True})
        attributes = decode(build_request([proto]).SerializeToString(), "")[0].spans[0].attributes
        assert attributes == {"s": "text", "i": 3, "f": 1.5, "b": True}

    def test_an_exception_event_is_surfaced(self) -> None:
        proto = build_span(status_code=2, status_message="boom")
        event = proto.events.add()
        event.name = "exception"
        event.time_unix_nano = BASE_NANOS
        event.attributes.append(kv("exception.type", "RuntimeError"))
        event.attributes.append(kv("exception.message", "recipient suppressed"))

        span = decode(build_request([proto]).SerializeToString(), "")[0].spans[0]
        assert span.has_exception
        assert span.exception_type == "RuntimeError"
        assert span.exception_message == "recipient suppressed"


class TestTranslate:
    def test_a_root_span_names_the_trace(self) -> None:
        # OTLP has no trace concept, so the name has to come from somewhere.
        request = build_request(
            [
                build_span(name="sdr.run", attributes={"openinference.span.kind": "AGENT"}),
                build_span(name="ChatAnthropic", span_id=PARENT_ID, parent=SPAN_ID),
            ]
        )
        translation = translate(decode(request.SerializeToString(), ""))
        assert len(translation.batch.traces) == 1
        assert translation.batch.traces[0].name == "sdr.run"
        assert len(translation.batch.spans) == 2

    def test_a_child_only_batch_declares_no_trace(self) -> None:
        # It cannot name the trace, so it must not try. The ingest service stubs the trace
        # from its spans and the name arrives with the root.
        request = build_request([build_span(span_id=PARENT_ID, parent=SPAN_ID)])
        translation = translate(decode(request.SerializeToString(), ""))
        assert translation.batch.traces == []
        assert len(translation.batch.spans) == 1

    def test_the_trace_extent_covers_a_child_that_outlives_its_parent(self) -> None:
        # A fire-and-forget task closes after the span that started it. Clipping the trace to
        # the root's own end time would hide it.
        request = build_request(
            [
                build_span(name="root", duration_nanos=100_000_000),
                build_span(
                    span_id=PARENT_ID,
                    parent=SPAN_ID,
                    start_nanos=BASE_NANOS + 50_000_000,
                    duration_nanos=900_000_000,
                ),
            ]
        )
        trace = translate(decode(request.SerializeToString(), "")).batch.traces[0]
        assert trace.ended_at == datetime(2026, 1, 1, 0, 0, 0, 950_000, tzinfo=UTC)

    def test_session_and_user_are_lifted_from_whichever_span_carries_them(self) -> None:
        # OpenInference puts them on spans, not on the resource.
        request = build_request(
            [
                build_span(name="root"),
                build_span(
                    span_id=PARENT_ID,
                    parent=SPAN_ID,
                    attributes={"session.id": "sess-9", "user.id": "u-4"},
                ),
            ]
        )
        trace = translate(decode(request.SerializeToString(), "")).batch.traces[0]
        assert trace.session_id == "sess-9"
        assert trace.user_ref == "u-4"

    def test_capture_mode_defaults_to_redacted(self) -> None:
        # An OTLP client cannot declare one, and assuming `full` for traffic whose
        # provenance we do not control would opt someone into storing raw prompts.
        request = build_request([build_span(name="root")])
        assert translate(decode(request.SerializeToString(), "")).batch.traces[0].capture_mode == (
            "redacted"
        )

    def test_a_malformed_id_rejects_only_that_span(self) -> None:
        # Rejecting the request would discard the valid spans beside the broken one.
        good = build_span(name="root")
        bad = build_span(span_id=PARENT_ID, trace_id=b"\x01\x02")
        translation = translate(decode(build_request([good, bad]).SerializeToString(), ""))
        assert len(translation.batch.spans) == 1
        assert translation.rejected_count == 1
        assert "trace_id must be" in translation.rejection_summary

    def test_all_zero_ids_are_rejected(self) -> None:
        # OTLP's "invalid" sentinel. Storing it would merge every broken exporter's spans
        # into one nonsense trace.
        bad = build_span(trace_id=bytes(16), span_id=bytes(8))
        translation = translate(decode(build_request([bad]).SerializeToString(), ""))
        assert translation.batch.spans == []
        assert "all-zero" in translation.rejection_summary

    def test_the_instrumentation_scope_is_recorded_on_the_span(self) -> None:
        # "Which instrumentation produced this?" is the first question when a span maps
        # oddly, and OTLP puts the answer on the scope rather than the span.
        request = build_request([build_span(name="root")], scope_name="my.instrumentation")
        span = translate(decode(request.SerializeToString(), "")).batch.spans[0]
        assert span.attributes["otel.scope.name"] == "my.instrumentation"

    def test_producer_side_attribute_drops_are_surfaced(self) -> None:
        # A span whose attributes were truncated upstream may be missing its model or token
        # counts, and zero tokens with no explanation reads as a free call.
        proto = build_span(name="root")
        proto.dropped_attributes_count = 4
        span = translate(decode(build_request([proto]).SerializeToString(), "")).batch.spans[0]
        assert span.attributes["otel.dropped_attributes_count"] == 4

    def test_an_errored_span_carries_the_exception_type(self) -> None:
        proto = build_span(name="root", status_code=2, status_message="failed")
        event = proto.events.add()
        event.name = "exception"
        event.time_unix_nano = BASE_NANOS
        event.attributes.append(kv("exception.type", "SuppressionError"))

        span = translate(decode(build_request([proto]).SerializeToString(), "")).batch.spans[0]
        assert span.status == "error"
        assert span.error_type == "SuppressionError"

    def test_resource_environment_reaches_the_batch(self) -> None:
        request = build_request(
            [build_span(name="root")],
            resource={"service.name": "sdr", "deployment.environment.name": "production"},
        )
        batch = translate(decode(request.SerializeToString(), "")).batch
        assert batch.resource.environment == "production"
        assert batch.resource.service_name == "sdr"

    def test_an_empty_request_translates_to_an_empty_batch(self) -> None:
        translation = translate(decode(ExportTraceServiceRequest().SerializeToString(), ""))
        assert translation.batch.spans == []
        assert translation.rejected_count == 0

    def test_tokens_are_omitted_rather_than_zeroed_for_a_non_model_span(self) -> None:
        # A tool span with `tokens: {0, 0, 0}` is indistinguishable from a model call that
        # reported nothing, and the second is a bug worth seeing.
        request = build_request([build_span(name="root", attributes={"tool.name": "search"})])
        span = translate(decode(request.SerializeToString(), "")).batch.spans[0]
        assert span.tokens is None

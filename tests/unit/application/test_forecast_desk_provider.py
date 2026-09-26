"""Structured protocol and bounded provider boundary never need live credentials/network."""

from __future__ import annotations

import json
from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from mesoforge.application import forecast_desk_provider as providers
from mesoforge.contracts.forecast_desk import (
    DeskConfig,
    DeskProviderError,
    DeskProviderUnavailableError,
    DeskResponse,
    DeskUsage,
    response_json_schema,
    validate_action,
)

# Exercise transport only behind the fake opener below; the suite-wide guard keeps
# ordinary forecast tests from ever reaching a real paid provider.
_TRANSPORT_UNDER_TEST = providers._post

TIME = "2026-09-25T18:00:00+00:00"
NO_EDIT = {"type": "no_edit", "rationale": "No material edit justified by retained evidence."}
EDIT = {
    "type": "edit_proposal",
    "proposal": {
        "field": "liquid_equivalent_precipitation_amount_1h",
        "operation": "scale",
        "valid_times": [TIME],
        "cell_ids": ["3:3"],
        "parameters": {"factor": 1.2},
        "taper": None,
        "rationale": "Fixture scaling only",
        "evidence_refs": ["pinned:summary"],
    },
}


def _response(action=NO_EDIT, **overrides):
    return json.dumps(
        {
            "status": "completed",
            "model": "explicit-model-snapshot",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps({"action": action})}],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 30},
            **overrides,
        }
    ).encode()


def test_protocol_all_actions_and_strict_nested_shape():
    actions = [
        {"type": "assessment", "summary": "Pinned forecast inspected."},
        {"type": "task_priority", "fields": ["air_temperature_2m"], "rationale": "Dry case."},
        {
            "type": "evidence_request",
            "request": {
                "tool": "inspect_contributors",
                "field": "air_temperature_2m",
                "valid_times": [TIME],
                "region": "context",
                "max_rows": 10,
            },
        },
        EDIT,
        NO_EDIT,
        {"type": "final_review", "accepted": True, "rationale": "Valid."},
        {"type": "complete", "rationale": "Finished."},
    ]
    for action in actions:
        assert validate_action(action) == action
        assert validate_action(action) is not action
        with pytest.raises(ValueError, match="unknown properties"):
            validate_action({**action, "shell": "prohibited"})
    schema = response_json_schema()
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert len(schema["properties"]["action"]["anyOf"]) == len(actions)


@pytest.mark.parametrize(
    "changes",
    [
        {"parameters": {"factor": -1}},
        {"parameters": {"factor": float("nan")}},
        {"parameters": {"factor": True}},
        {"parameters": {"factor": 1, "code": "bad"}},
        {"valid_times": ["2026-09-25T18:00:00"]},
        {"cell_ids": ["../path"]},
        {"evidence_refs": []},
        {"operation": "python"},
        {"taper": {"width_m": 0}},
    ],
)
def test_protocol_rejects_malformed_edit_arguments(changes):
    action = deepcopy(EDIT)
    action["proposal"].update(changes)
    with pytest.raises(ValueError):
        validate_action(action)


def test_budgets_and_config_have_no_modes(monkeypatch):
    assert DeskConfig().hard_seconds == 900
    monkeypatch.setenv("MESOFORGE_AI_REQUEST_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("MESOFORGE_AI_MAX_PROVIDER_CALLS", "4")
    configured = providers.config_from_environment()
    assert configured.request_timeout_seconds == 12.5
    assert configured.max_provider_calls == 4
    with pytest.raises(TypeError):
        DeskConfig(mode="off")
    for options in (
        {"hard_seconds": 620},
        {"max_review_passes": 2},
        {"max_tool_calls": True},
        {"max_cost_usd": float("nan")},
        {"max_output_tokens": 100001},
    ):
        with pytest.raises(ValueError):
            DeskConfig(**options)


def test_missing_configuration_attempts_unavailable_without_network(monkeypatch):
    for name in ("MESOFORGE_AI_PROVIDER", "MESOFORGE_AI_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(providers, "_post", lambda *a: pytest.fail("Must not call a provider"))
    provider = providers.provider_from_environment()
    with pytest.raises(DeskProviderUnavailableError):
        provider.request({}, timeout_seconds=1, max_output_tokens=100)


def test_adapter_strict_json_no_hosted_tools_secrets_or_default_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-never-persist")
    monkeypatch.setenv("MESOFORGE_AI_PROVIDER", "openai")
    monkeypatch.setenv("MESOFORGE_AI_MODEL", "explicit-model")
    captured = []

    def post(body, credential, timeout):
        captured.append(json.loads(body))
        assert credential == "fixture-secret-never-persist" and timeout == 5
        assert credential not in body.decode()
        return _response()

    monkeypatch.setattr(providers, "_post", post)
    provider = providers.provider_from_environment()
    result = provider.request({"phase": "assess"}, timeout_seconds=5, max_output_tokens=200)
    assert result == DeskResponse(NO_EDIT, 100, 30, None, "explicit-model-snapshot")
    request = captured[0]
    assert request["model"] == "explicit-model"
    assert request["store"] is False and request["tools"] == []
    assert request["text"]["format"]["strict"] is True
    assert request["max_output_tokens"] == 200
    assert "fixture-secret" not in repr(provider.__dict__) + repr(result)
    with pytest.raises(DeskProviderError, match="Credential material"):
        provider.request(
            {"unexpected": "fixture-secret-never-persist"}, timeout_seconds=5, max_output_tokens=200
        )


def test_transport_minification_preserves_complete_structured_input_and_schema():
    payload = {
        "phase": "task",
        "context": {
            "location": "Minneapolis",
            "description": "Space inside meteorological strings remains unchanged.",
            "temperature": {"value": 273.15, "unit": "K", "missing": None},
            "valid_times": [TIME],
            "symbol": "°F",
        },
    }
    provider = providers.OpenAIResponsesProvider("explicit-model", reasoning_effort="low")
    raw = provider._body(payload, 200)
    parsed = json.loads(raw)
    assert json.loads(parsed["input"]) == payload
    assert parsed["text"]["format"]["schema"] == response_json_schema()
    assert parsed["input"] == json.dumps(
        payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    )
    assert (
        raw
        == json.dumps(parsed, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()
    )
    expanded = {**parsed, "input": json.dumps(payload, ensure_ascii=True, allow_nan=False)}
    assert len(raw) < len(json.dumps(expanded, ensure_ascii=True).encode())


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        _response(status="incomplete"),
        _response(action={"type": "shell"}),
        _response(
            output=[{"type": "message", "content": [{"type": "refusal", "refusal": "private"}]}]
        ),
        _response(output=[{"type": "web_search_call"}]),
        _response(usage={"input_tokens": -1, "output_tokens": 2}),
    ],
)
def test_malformed_refused_or_incomplete_response_is_sanitized(monkeypatch, raw):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setattr(providers, "_post", lambda *args: raw)
    with pytest.raises(DeskProviderError) as captured:
        providers.OpenAIResponsesProvider("test").request(
            {}, timeout_seconds=1, max_output_tokens=100
        )
    assert "private" not in str(captured.value) and "fixture-secret" not in str(captured.value)


def test_transport_timeout_failure_size_and_no_redirect(monkeypatch):
    for exception, expected in (
        (TimeoutError("secret response"), TimeoutError),
        (URLError("secret response"), DeskProviderUnavailableError),
        (HTTPError("private-url", 429, "secret response", {}, None), DeskProviderError),
    ):

        def fail(*args, error=exception, **kwargs):
            raise error

        monkeypatch.setattr(providers, "build_opener", lambda *args: SimpleNamespace(open=fail))
        with pytest.raises(expected) as captured:
            _TRANSPORT_UNDER_TEST(b"{}", "fixture-secret", 1)
        assert "secret" not in str(captured.value)
    monkeypatch.setattr(
        providers,
        "build_opener",
        lambda *args: SimpleNamespace(
            open=lambda *a, **k: BytesIO(b"x" * (providers._MAX_RESPONSE_BYTES + 1))
        ),
    )
    with pytest.raises(DeskProviderError, match="byte limit"):
        _TRANSPORT_UNDER_TEST(b"{}", "fixture-secret", 1)
    with pytest.raises(DeskProviderError, match="redirects"):
        providers._NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere")


def test_operator_pricing_is_explicit_and_usage_cost_auditable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setattr(providers, "_post", lambda *args: _response())
    assert providers.OpenAIResponsesProvider("test").estimate_max_cost({}, 100) is None
    provider = providers.OpenAIResponsesProvider(
        "test", input_usd_per_million=1, output_usd_per_million=2
    )
    response = provider.request({}, timeout_seconds=1, max_output_tokens=100)
    assert response.cost_usd == pytest.approx(160 / 1e6)
    assert provider.estimate_max_cost({}, 100) > response.cost_usd
    with pytest.raises(ValueError, match="Both configured"):
        providers.OpenAIResponsesProvider("test", input_usd_per_million=1)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"status": "incomplete"}, "response_incomplete"),
        ({"action": {"type": "unrecognized"}}, "response_invalid_action"),
        (
            {
                "output": [
                    {"type": "message", "content": [{"type": "refusal", "refusal": "private"}]}
                ]
            },
            "response_refused",
        ),
    ],
)
def test_failed_output_preserves_known_usage_without_private_content(monkeypatch, changes, reason):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setattr(providers, "_post", lambda *args: _response(**changes))
    provider = providers.OpenAIResponsesProvider(
        "test", input_usd_per_million=1, output_usd_per_million=2
    )
    with pytest.raises(DeskProviderError) as captured:
        provider.request({}, timeout_seconds=1, max_output_tokens=100)
    error = captured.value
    assert error.reason_code == reason
    assert error.usage == DeskUsage(100, 30, 160 / 1e6, "explicit-model-snapshot")
    assert "private" not in repr(error.__dict__)


def test_missing_usage_remains_unknown_and_reasoning_effort_is_explicit(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setattr(providers, "_post", lambda *args: _response(usage=None))
    with pytest.raises(DeskProviderError) as captured:
        providers.OpenAIResponsesProvider("test").request(
            {}, timeout_seconds=1, max_output_tokens=100
        )
    assert captured.value.usage is None
    assert "reasoning" not in json.loads(providers.OpenAIResponsesProvider("test")._body({}, 100))
    monkeypatch.setenv("MESOFORGE_AI_PROVIDER", "openai")
    monkeypatch.setenv("MESOFORGE_AI_MODEL", "explicit-model")
    monkeypatch.setenv("MESOFORGE_AI_REASONING_EFFORT", "low")
    assert providers.provider_from_environment().reasoning_effort == "low"
    assert json.loads(providers.provider_from_environment()._body({}, 100))["reasoning"] == {
        "effort": "low"
    }
    with pytest.raises(ValueError, match="reasoning effort"):
        providers.OpenAIResponsesProvider("test", reasoning_effort="arbitrary")


@pytest.mark.parametrize("code", ["invalid_json_schema", "private-credential-content", None])
def test_http_failure_retains_only_safe_status_and_allowlisted_code(monkeypatch, code):
    def fail(*args, **kwargs):
        raise HTTPError(
            "secret-url",
            400,
            "secret-header",
            {},
            BytesIO(
                json.dumps(
                    {
                        "error": {"code": code, "message": "secret-response-credential"},
                    }
                ).encode()
            ),
        )

    monkeypatch.setattr(providers, "build_opener", lambda *args: SimpleNamespace(open=fail))
    with pytest.raises(DeskProviderError) as captured:
        _TRANSPORT_UNDER_TEST(b"{}", "fixture-secret", 1)
    assert captured.value.reason_code == (
        "http_400_invalid_json_schema" if code == "invalid_json_schema" else "http_400"
    )
    assert "secret" not in repr(captured.value.__dict__) + str(captured.value)
    assert captured.value.usage is None


def _http_failure(status, code):
    def fail(*args, **kwargs):
        raise HTTPError(
            "secret-url",
            status,
            "secret-header",
            {},
            BytesIO(json.dumps({"error": {"code": code, "message": "secret-body"}}).encode()),
        )

    return fail


@pytest.mark.parametrize("code", ["insufficient_quota", "rate_limit_exceeded"])
def test_http_429_quota_and_rate_limit_fall_back_with_sanitized_reason(monkeypatch, code):
    """A real adapter 429 is a failed attempt: unchanged parent, unknown usage, safe code."""
    from mesoforge.application.forecast_desk import run_forecast_desk
    from tests.unit.application.test_forecast_desk import forecast

    monkeypatch.setattr(providers, "_post", _TRANSPORT_UNDER_TEST)
    monkeypatch.setattr(
        providers,
        "build_opener",
        lambda *args: SimpleNamespace(open=_http_failure(429, code)),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-never-persist")
    monkeypatch.setenv("MESOFORGE_AI_PROVIDER", "openai")
    monkeypatch.setenv("MESOFORGE_AI_MODEL", "explicit-model")
    parent = forecast()
    final, report = run_forecast_desk(parent)
    assert final is parent
    assert report["completion_reason"] == "provider_failure"
    assert report["provider_failure_code"] == f"http_429_{code}"
    assert report["usage"]["provider_calls"] == 1
    assert report["usage"]["unreported_provider_calls"] == 1
    assert report["usage"]["input_tokens"] == 0 and report["usage"]["cost_usd"] is None
    assert report["validation"]["status"] == "valid"
    serialized = json.dumps(report, default=str)
    assert "secret" not in serialized


def test_transport_framing_and_connect_timeout_are_sanitized(monkeypatch):
    import http.client

    for exception, expected in (
        (URLError(TimeoutError("secret connect timeout")), TimeoutError),
        (http.client.IncompleteRead(b"secret partial"), DeskProviderError),
        (http.client.BadStatusLine("secret status"), DeskProviderError),
    ):

        def fail(*args, error=exception, **kwargs):
            raise error

        monkeypatch.setattr(providers, "build_opener", lambda *args: SimpleNamespace(open=fail))
        with pytest.raises(expected) as captured:
            _TRANSPORT_UNDER_TEST(b"{}", "fixture-secret", 1)
        assert "secret" not in str(captured.value)
        if expected is DeskProviderError:
            assert captured.value.reason_code == "transport_failure"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MESOFORGE_AI_PROVIDER", "unsupported-provider"),
        ("MESOFORGE_AI_INPUT_USD_PER_MILLION", "$1.25"),
        ("MESOFORGE_AI_REASONING_EFFORT", "arbitrary"),
        ("MESOFORGE_AI_MODEL", "../not a model"),
        ("MESOFORGE_AI_MAX_PROVIDER_CALLS", "1e3"),
        ("MESOFORGE_AI_TARGET_SECONDS", "10m"),
        ("MESOFORGE_AI_MAX_OUTPUT_TOKENS", "0"),
    ],
)
def test_invalid_runtime_configuration_is_named_without_value_and_falls_back(
    monkeypatch, name, value
):
    from mesoforge.application.forecast_desk import run_forecast_desk
    from tests.unit.application.test_forecast_desk import forecast

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret-never-persist")
    monkeypatch.setenv("MESOFORGE_AI_PROVIDER", "openai")
    monkeypatch.setenv("MESOFORGE_AI_MODEL", "explicit-model")
    if name == "MESOFORGE_AI_INPUT_USD_PER_MILLION":
        monkeypatch.setenv("MESOFORGE_AI_OUTPUT_USD_PER_MILLION", "2")
    monkeypatch.setenv(name, value)
    parent = forecast()
    final, report = run_forecast_desk(parent)
    assert final is parent
    assert report["completion_reason"] == "configuration_invalid"
    assert report["configuration_error"] == name
    assert report["usage"]["provider_calls"] == 0
    if len(value) > 3:  # Short numerals legitimately occur elsewhere in the report.
        assert value not in json.dumps(report, default=str)


def test_reasoning_effort_is_normalized_to_the_explicit_vocabulary(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setenv("MESOFORGE_AI_PROVIDER", "OpenAI")
    monkeypatch.setenv("MESOFORGE_AI_MODEL", "explicit-model")
    for raw, expected in ((" Minimal ", "minimal"), ("NONE", "none"), ("  ", None)):
        monkeypatch.setenv("MESOFORGE_AI_REASONING_EFFORT", raw)
        assert providers.provider_from_environment().reasoning_effort == expected


def test_inspection_row_bound_is_shared_by_protocol_and_context():
    from mesoforge.contracts.forecast_desk import MAX_INSPECTION_ROWS

    request = {
        "type": "evidence_request",
        "request": {
            "tool": "inspect_contributors",
            "field": "air_temperature_2m",
            "valid_times": [TIME],
            "region": "context",
            "max_rows": MAX_INSPECTION_ROWS,
        },
    }
    assert validate_action(request) == request
    request["request"]["max_rows"] = MAX_INSPECTION_ROWS + 1
    with pytest.raises(ValueError, match="row budget"):
        validate_action(request)

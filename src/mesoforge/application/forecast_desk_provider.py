"""Optional network adapter for the always-attempted, provider-neutral forecast desk.

Only this boundary performs inference HTTP. It does not offer provider-hosted tools,
web access or arbitrary destinations. Missing configuration is a failed attempt, not
an AI execution mode. Errors deliberately omit response bodies and request headers.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import re
import time
from dataclasses import fields
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from mesoforge.contracts.forecast_desk import (
    DESK_POLICY,
    DeskConfig,
    DeskConfigurationError,
    DeskProviderError,
    DeskProviderUnavailableError,
    DeskResponse,
    DeskUsage,
    ForecastDeskProvider,
    response_json_schema,
    validate_action,
)

_ENDPOINT = "https://api.openai.com/v1/responses"
_MAX_RESPONSE_BYTES = 131072
_MAX_REQUEST_BYTES = 262144
# Explicit operator vocabulary; an unsupported value for a given model is an HTTP 400
# failed attempt with a sanitized code, never a silently substituted effort.
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
_SAFE_HTTP_CODES = frozenset(
    {
        "model_not_found",
        "invalid_api_key",
        "insufficient_quota",
        "rate_limit_exceeded",
        "unsupported_parameter",
        "unsupported_value",
        "invalid_parameter",
        "invalid_value",
        "invalid_json_schema",
        "context_length_exceeded",
        "permission_denied",
    }
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise DeskProviderError(
            "Provider redirects are prohibited", reason_code="redirect_prohibited"
        )


class UnavailableDeskProvider:
    """An explicit failed provider attempt; operational fallback is owned by the controller."""

    provider_name = "unconfigured"
    model_name = "unconfigured"

    def request(
        self, payload: dict[str, Any], *, timeout_seconds: float, max_output_tokens: int
    ) -> DeskResponse:
        raise DeskProviderUnavailableError(
            "Forecast desk requires configured provider, model and credential",
            reason_code="configuration_unavailable",
        )


class OpenAIResponsesProvider:
    """Explicitly configured Responses API adapter with strict structured output.

    The controller supervises the total call deadline as well as transport timeouts.
    Prices are optional operator-supplied USD per million tokens, not guessed pricing.
    """

    provider_name = "openai"

    def __init__(
        self,
        model: str,
        *,
        input_usd_per_million: float | None = None,
        output_usd_per_million: float | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        if not isinstance(model, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", model
        ):
            raise ValueError("Runtime model must be an explicit bounded provider model name")
        for price in (input_usd_per_million, output_usd_per_million):
            if price is not None and (
                type(price) not in (int, float) or not math.isfinite(price) or price < 0
            ):
                raise ValueError("Configured token prices must be finite and non-negative")
        if (input_usd_per_million is None) != (output_usd_per_million is None):
            raise ValueError("Both configured input and output token prices are required")
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("Unsupported explicit runtime reasoning effort")
        self.model_name = model
        self._input_price = input_usd_per_million
        self._output_price = output_usd_per_million
        self.reasoning_effort = reasoning_effort

    def _body(self, payload: dict[str, Any], max_output_tokens: int) -> bytes:
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 32768:
            raise ValueError("Provider output token budget is invalid")
        body = json.dumps(
            {
                "model": self.model_name,
                "instructions": DESK_POLICY["instructions"],
                "input": json.dumps(
                    payload, ensure_ascii=True, allow_nan=False, separators=(",", ":")
                ),
                "max_output_tokens": max_output_tokens,
                "store": False,
                "tools": [],
                **(
                    {"reasoning": {"effort": self.reasoning_effort}}
                    if self.reasoning_effort is not None
                    else {}
                ),
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "mesoforge_forecast_desk",
                        "strict": True,
                        "schema": response_json_schema(),
                    }
                },
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > _MAX_REQUEST_BYTES:
            raise DeskProviderError(
                "Forecast-desk request exceeds the transport byte limit",
                reason_code="request_byte_limit",
            )
        return body

    def estimate_max_cost(self, payload: dict[str, Any], max_output_tokens: int) -> float | None:
        """Conservative preflight bound: full serialized bytes plus protocol allowance.

        Byte length deliberately overcounts ordinary tokenizer input. Output includes
        the requested reasoning/output token ceiling. Actual usage is retained separately.
        """
        if self._input_price is None or self._output_price is None:
            return None
        input_bound = self.estimate_input_tokens(payload, max_output_tokens)
        return (input_bound * self._input_price + max_output_tokens * self._output_price) / 1e6

    def estimate_input_tokens(self, payload: dict[str, Any], max_output_tokens: int) -> int:
        """Conservative preflight including instructions/schema, not only user context."""
        return len(self._body(payload, max_output_tokens)) + 4096

    def _usage(self, response: dict[str, Any]) -> DeskUsage:
        usage = response["usage"]
        # Validate counts before arithmetic so bools/strings/negative values cannot
        # enter accounting even when the action itself is rejected.
        result = DeskUsage(
            usage["input_tokens"], usage["output_tokens"], response_model=response.get("model")
        )
        cost = None
        if self._input_price is not None and self._output_price is not None:
            cost = (
                result.input_tokens * self._input_price + result.output_tokens * self._output_price
            ) / 1e6
        return DeskUsage(result.input_tokens, result.output_tokens, cost, result.response_model)

    def request(
        self, payload: dict[str, Any], *, timeout_seconds: float, max_output_tokens: int
    ) -> DeskResponse:
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Provider timeout must be finite and positive")
        # Read only at the actual call boundary; secrets never become provider state,
        # dataclass fields, repr(), context, provenance, policy or returned error content.
        credential = os.environ.get("OPENAI_API_KEY")
        if not credential:
            raise DeskProviderUnavailableError(
                "OpenAI runtime credential is unavailable", reason_code="credential_unavailable"
            )
        body = self._body(payload, max_output_tokens)
        if credential.encode("utf-8") in body:
            raise DeskProviderError(
                "Credential material cannot enter forecast-desk context",
                reason_code="prohibited_credential_material",
            )
        raw = _post(body, credential, timeout_seconds)
        if credential.encode("utf-8") in raw:
            raise DeskProviderError(
                "Provider response contains prohibited credential material",
                reason_code="prohibited_credential_material",
            )
        accounting = None
        reason_code = "response_malformed"
        try:
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise ValueError("Oversized provider response")
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError("Malformed provider response")
            accounting = self._usage(response)
            if response.get("status") != "completed":
                reason_code = "response_incomplete"
                raise ValueError("Incomplete provider response")
            texts = []
            for item in response["output"]:
                if item.get("type") == "message":
                    for part in item["content"]:
                        if part.get("type") == "refusal":
                            reason_code = "response_refused"
                            raise ValueError("Provider refused response")
                        if part.get("type") == "output_text":
                            texts.append(part["text"])
                elif item.get("type") != "reasoning":
                    raise ValueError("Unrequested provider output type")
            if len(texts) != 1:
                raise ValueError("Exactly one structured provider message is required")
            envelope = json.loads(texts[0])
            if not isinstance(envelope, dict) or set(envelope) != {"action"}:
                raise ValueError("Provider action envelope is invalid")
            reason_code = "response_invalid_action"
            action = validate_action(envelope["action"])
            return DeskResponse(
                action,
                accounting.input_tokens,
                accounting.output_tokens,
                accounting.cost_usd,
                accounting.response_model,
            )
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            raise DeskProviderError(
                "Provider returned malformed, refused or incomplete structured output",
                usage=accounting,
                reason_code=reason_code,
            ) from None


def _post(body: bytes, credential: str, timeout_seconds: float) -> bytes:
    """Fixed HTTPS endpoint, no redirects/proxy inheritance/retries, bounded response."""
    request = Request(
        _ENDPOINT,
        data=body,
        headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=timeout_seconds) as response:
            chunks: list[bytes] = []
            size = 0
            while True:
                if time.monotonic() - started >= timeout_seconds:
                    raise TimeoutError("Provider request deadline exceeded")
                chunk = response.read(min(16384, _MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    raise DeskProviderError(
                        "Provider response exceeds the transport byte limit",
                        reason_code="response_byte_limit",
                    )
                chunks.append(chunk)
            return b"".join(chunks)
    except HTTPError as error:
        # Retain only a fixed diagnostic vocabulary, never arbitrary API messages,
        # URLs, response text, parameter values, headers or credential material.
        reason = f"http_{error.code}" if 100 <= error.code <= 599 else "http_failure"
        try:
            failed_response = json.loads(error.read(_MAX_RESPONSE_BYTES + 1))
            code = failed_response.get("error", {}).get("code")
            if isinstance(code, str) and code in _SAFE_HTTP_CODES:
                reason += f"_{code}"
        except (
            AttributeError,
            TypeError,
            ValueError,
            OSError,
            RecursionError,
            http.client.HTTPException,
        ):
            pass
        raise DeskProviderError("Provider HTTP request failed", reason_code=reason) from None
    except TimeoutError:
        raise TimeoutError("Provider request timed out") from None
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise TimeoutError("Provider request timed out") from None
        raise DeskProviderUnavailableError(
            "Provider network request failed", reason_code="network_unavailable"
        ) from None
    except OSError:
        raise DeskProviderUnavailableError(
            "Provider network request failed", reason_code="network_unavailable"
        ) from None
    except http.client.HTTPException:
        # Truncated/invalid HTTP framing is a failed attempt with no retained body.
        raise DeskProviderError(
            "Provider transport response was invalid", reason_code="transport_failure"
        ) from None


def provider_from_environment() -> ForecastDeskProvider:
    """No model/provider is inferred from the coding agent or a development login."""
    provider = os.environ.get("MESOFORGE_AI_PROVIDER")
    model = os.environ.get("MESOFORGE_AI_MODEL")
    if not provider or not model or not os.environ.get("OPENAI_API_KEY"):
        return UnavailableDeskProvider()
    if provider.strip().lower() != "openai":
        raise DeskConfigurationError("MESOFORGE_AI_PROVIDER")
    prices = []
    for name in ("INPUT", "OUTPUT"):
        setting = f"MESOFORGE_AI_{name}_USD_PER_MILLION"
        raw = os.environ.get(setting)
        try:
            prices.append(float(raw) if raw is not None else None)
        except ValueError:
            raise DeskConfigurationError(setting) from None
    if (prices[0] is None) != (prices[1] is None) or any(
        price is not None and (not math.isfinite(price) or price < 0) for price in prices
    ):
        raise DeskConfigurationError("MESOFORGE_AI_INPUT_USD_PER_MILLION")
    effort = os.environ.get("MESOFORGE_AI_REASONING_EFFORT")
    effort = effort.strip().lower() if effort is not None and effort.strip() else None
    if effort is not None and effort not in REASONING_EFFORTS:
        raise DeskConfigurationError("MESOFORGE_AI_REASONING_EFFORT")
    try:
        return OpenAIResponsesProvider(
            model.strip(),
            input_usd_per_million=prices[0],
            output_usd_per_million=prices[1],
            reasoning_effort=effort,
        )
    except ValueError:
        raise DeskConfigurationError("MESOFORGE_AI_MODEL") from None


def config_from_environment() -> DeskConfig:
    """All execution budgets have explicit optional overrides, never an off/shadow mode."""
    defaults = DeskConfig()
    values: dict[str, Any] = {}
    for field in fields(defaults):
        setting = f"MESOFORGE_AI_{field.name.upper()}"
        raw = os.environ.get(setting)
        if raw is not None:
            original = getattr(defaults, field.name)
            try:
                values[field.name] = (
                    raw.strip()
                    if isinstance(original, str)
                    else float(raw)
                    if field.name.endswith("seconds") or field.name == "max_cost_usd"
                    else int(raw)
                )
            except ValueError:
                raise DeskConfigurationError(setting) from None
    try:
        return DeskConfig(**values)
    except ValueError:
        # Name an individually invalid setting when one exists; otherwise the
        # combination (for example target versus hard deadline) is inconsistent.
        for name, value in values.items():
            try:
                DeskConfig(**{name: value})
            except ValueError:
                raise DeskConfigurationError(f"MESOFORGE_AI_{name.upper()}") from None
        raise DeskConfigurationError("MESOFORGE_AI_BUDGETS") from None

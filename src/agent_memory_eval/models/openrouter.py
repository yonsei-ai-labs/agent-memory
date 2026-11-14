"""Small stateless OpenRouter transport. Credentials never enter request artifacts."""

from copy import deepcopy
import math
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agent_memory_eval.contracts.core import ContractError, canonical, read_json


API_ROOT = "https://openrouter.ai/api/v1"


class ProviderError(ContractError):
    def __init__(self, code, response=None, submitted=False):
        self.code, self.response, self.submitted = code, response, submitted
        super().__init__(code)


def model_catalog():
    with urlopen(API_ROOT + "/models", timeout=30) as response:
        return read_json(response.read())


def resolve_model(catalog, model_id, parameters):
    model = next((item for item in catalog["data"] if item["id"] == model_id), None)
    if model is None:
        raise ContractError("Requested OpenRouter model is unavailable")
    if set(parameters) - set(model["supported_parameters"]):
        raise ContractError("Requested parameters are not supported by this model")
    return deepcopy(model)


class SpendGuard:
    def __init__(self, *, max_calls, max_usd, max_seconds):
        if max_calls < 1 or max_usd <= 0 or max_seconds <= 0:
            raise ContractError("A live check needs finite positive call, spend and time guards")
        self.max_calls, self.max_usd = max_calls, max_usd
        self.deadline = time.monotonic() + max_seconds
        self.calls, self.reported_usd, self.unknown_cost = 0, 0.0, False

    def before(self, request, model):
        if self.calls >= self.max_calls:
            raise ProviderError("CALL_LIMIT")
        if time.monotonic() >= self.deadline:
            raise ProviderError("TIME_LIMIT")
        if self.unknown_cost:
            raise ProviderError("UNKNOWN_COST_STOP")
        # Conservative byte-based reservation; not a provider-enforced monetary cap.
        prompt_bound = len(canonical(request)) + 64 * len(request["messages"])
        output = request["max_completion_tokens"]
        reserve = prompt_bound * float(model["pricing"]["prompt"]) + output * float(model["pricing"]["completion"])
        if self.reported_usd + reserve > self.max_usd:
            raise ProviderError("SPEND_LIMIT")
        self.calls += 1

    def after(self, response):
        cost = response.get("usage", {}).get("cost")
        if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            self.unknown_cost = True
        else:
            self.reported_usd += cost


class OpenRouterClient:
    offline = False

    def __init__(self, model, guard, *, opener=urlopen):
        self.model, self.guard, self.opener = model, guard, opener

    def respond(self, request):
        if request["model"] != self.model["id"]:
            raise ProviderError("MODEL_MISMATCH")
        expected_route = dict(only=["openai"], allow_fallbacks=False, require_parameters=True)
        if request.get("provider") != expected_route or request.get("stream") is not False:
            raise ProviderError("UNPINNED_ROUTE")
        if any(key in request for key in ("api_key", "previous_response_id", "conversation", "thread_id", "models", "plugins")):
            raise ProviderError("UNSUPPORTED_REQUEST_STATE")
        if len(canonical(request)) > 64000:
            raise ProviderError("INPUT_SIZE_LIMIT")
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise ProviderError("OPENROUTER_API_KEY_MISSING")
        self.guard.before(request, self.model)
        wire = Request(API_ROOT + "/chat/completions", data=canonical(request), method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                     "X-OpenRouter-Metadata": "enabled"})
        try:
            with self.opener(wire, timeout=min(45, max(0.1, self.guard.deadline - time.monotonic()))) as response:
                body = response.read()
        except HTTPError as error:
            # Do not serialize headers, exceptions or raw errors that might echo a credential.
            self.guard.unknown_cost = True
            raise ProviderError("HTTP_" + str(error.code), dict(error=dict(code=error.code)), submitted=True) from None
        except (URLError, TimeoutError, OSError):
            self.guard.unknown_cost = True
            raise ProviderError("TRANSPORT_UNKNOWN", submitted=None) from None
        if any(secret and secret.encode() in body for secret in (key, os.environ.get("OPENAI_API_KEY"))):
            self.guard.unknown_cost = True
            raise ProviderError("CREDENTIAL_IN_RESPONSE", submitted=True)
        try:
            result = read_json(body)
        except (ValueError, UnicodeError):
            self.guard.unknown_cost = True
            raise ProviderError("INVALID_RESPONSE_JSON", submitted=True) from None
        self.guard.after(result)
        if "error" in result:
            raise ProviderError("PROVIDER_RESPONSE_ERROR", result, submitted=True)
        return result

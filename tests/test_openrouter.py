"""Mock HTTP only. Unit tests never use the real keys or make network calls."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from jsonschema import ValidationError

from agent_memory_eval.contracts.core import ContractError, RunContext, canonical
from agent_memory_eval.contracts.connection import forward_seed, native_parameters, provider_route, validate_profile
from agent_memory_eval.models.openrouter import OpenRouterClient, ProviderError, SpendGuard, resolve_endpoint, resolve_model
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry


MODEL = dict(id="openai/gpt-6-luna", canonical_slug="openai/gpt-6-luna-test",
    pricing=dict(prompt="0.0000001", completion="0.0000005"),
    supported_parameters=["seed", "reasoning", "max_completion_tokens", "tools", "tool_choice"])
ROUTE = dict(only=["openai"], allow_fallbacks=False, require_parameters=True)


def response(cost=0.0001):
    usage = dict(prompt_tokens=12, completion_tokens=5, total_tokens=17)
    if cost is not None:
        usage["cost"] = cost
    return dict(id="generation-test", model=MODEL["id"], provider="OpenAI", object="chat.completion", created=0,
        choices=[dict(index=0, finish_reason="stop", message=dict(role="assistant", content="hello"))], usage=usage)


class Reply:
    def __init__(self, body):
        self.body = canonical(body)
    def read(self):
        return self.body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, OPENROUTER_API_KEY="test-router-secret", OPENAI_API_KEY="unused-openai-secret")
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.guard = SpendGuard(max_calls=3, max_usd=0.25, max_seconds=30)
        self.request = dict(model=MODEL["id"], messages=[dict(role="user", content="hello")],
            provider=deepcopy(ROUTE), stream=False, max_completion_tokens=100, reasoning=dict(effort="low"), seed=0)

    def test_stateless_wire_uses_only_router_key_and_records_no_credentials(self):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            return Reply(response())
        body = OpenRouterClient(MODEL, self.guard, route=ROUTE, opener=opener).respond(self.request)
        self.assertEqual(seen[0].full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer test-router-secret")
        self.assertEqual(seen[0].data, canonical(self.request))
        self.assertNotIn(b"secret", seen[0].data)
        self.assertEqual(self.guard.reported_usd, body["usage"]["cost"])

    def test_missing_cost_is_unknown_and_blocks_next_paid_attempt(self):
        client = OpenRouterClient(MODEL, self.guard, route=ROUTE, opener=lambda *a, **kw: Reply(response(None)))
        client.respond(self.request)
        with self.assertRaisesRegex(ProviderError, "UNKNOWN_COST_STOP"):
            client.respond(self.request)
        self.assertEqual(self.guard.calls, 1)
        self.assertTrue(self.guard.unknown_cost)

    def test_call_spend_and_deadline_guards_stop_before_http(self):
        for attribute, value, code in (("max_calls", 0, "CALL_LIMIT"), ("max_usd", 0, "SPEND_LIMIT"),
                                       ("deadline", 0, "TIME_LIMIT")):
            with self.subTest(code=code):
                guard = SpendGuard(max_calls=3, max_usd=0.25, max_seconds=30)
                setattr(guard, attribute, value)
                with self.assertRaisesRegex(ProviderError, code):
                    OpenRouterClient(MODEL, guard, route=ROUTE, opener=lambda *a, **kw: self.fail("HTTP attempted")).respond(self.request)

    def test_fallback_implicit_session_and_other_model_are_rejected(self):
        variants = [dict(self.request, provider=dict(ROUTE, allow_fallbacks=True)),
                    dict(self.request, provider=dict(ROUTE, only=["deepseek"])),
                    dict(self.request, previous_response_id="old-session"), dict(self.request, model="other-model")]
        for request in variants:
            with self.subTest(request_keys=list(request)):
                with self.assertRaises(ProviderError):
                    OpenRouterClient(MODEL, self.guard, route=ROUTE, opener=lambda *a, **kw: self.fail("HTTP attempted")).respond(request)
        self.assertEqual(self.guard.calls, 0)

    def test_http_error_body_and_credential_echo_are_not_persisted(self):
        def error(*args, **kwargs):
            raise HTTPError("https://openrouter.ai", 401, "unauthorized", {}, io.BytesIO(b"test-router-secret"))
        with self.assertRaises(ProviderError) as caught:
            OpenRouterClient(MODEL, self.guard, route=ROUTE, opener=error).respond(self.request)
        self.assertEqual(caught.exception.response, dict(error=dict(code=401)))
        self.assertNotIn("secret", str(caught.exception))
        def no_endpoint(*args, **kwargs):
            raise HTTPError("https://openrouter.ai", 404, "unavailable", {},
                io.BytesIO(canonical(dict(error=dict(message="No endpoints found matching your data policy test-router-secret")))))
        with self.assertRaises(ProviderError) as routing:
            OpenRouterClient(MODEL, SpendGuard(max_calls=2,max_usd=1,max_seconds=10), route=ROUTE,
                opener=no_endpoint).respond(self.request)
        self.assertEqual(routing.exception.response, dict(error=dict(code=404,routing_reason="data_policy")))
        with self.assertRaisesRegex(ProviderError, "CREDENTIAL_IN_RESPONSE"):
            OpenRouterClient(MODEL, SpendGuard(max_calls=2,max_usd=1,max_seconds=10), route=ROUTE,
                opener=lambda *a, **kw: Reply(dict(choices=[{"content":"test-router-secret"}]))).respond(self.request)

    def test_unsupported_model_parameters_are_not_silently_dropped(self):
        catalog = dict(data=[MODEL])
        resolve_model(catalog, MODEL["id"], dict(reasoning=dict(effort="low")))
        with self.assertRaisesRegex(ContractError, "not supported"):
            resolve_model(catalog, MODEL["id"], dict(temperature=0))

    def test_deepseek_role_route_and_max_tokens_limit(self):
        model = dict(MODEL, id="deepseek/deepseek-v4.1-flash-1017")
        route = dict(ROUTE, only=["deepseek"])
        request = dict(model=model["id"], provider=route, stream=False,
            messages=self.request["messages"], max_tokens=512, temperature=0, reasoning=dict(enabled=False))
        seen = []
        def opener(wire, timeout):
            seen.append(json.loads(wire.data))
            return Reply(dict(response(), model=model["id"], provider="DeepSeek"))
        client = OpenRouterClient(model, self.guard, route=route, opener=opener)
        client.respond(request)
        self.assertEqual(seen, [request])
        self.assertNotIn("seed", seen[0])
        for invalid in (dict(request, max_tokens=None), dict(request, max_completion_tokens=512)):
            with self.assertRaisesRegex(ProviderError, "INVALID_OUTPUT_LIMIT"):
                client.respond(invalid)
        self.assertEqual(self.guard.calls, 1)

    def test_provider_support_and_reservation_use_selected_endpoint(self):
        endpoint = dict(tag="openai", provider_name="OpenAI", status=0,
            supported_parameters=["reasoning", "max_tokens"],
            pricing=dict(prompt="0.0000002", completion="0.000001",
                overrides=[dict(prompt="0.0000004", completion="0.000002")]))
        catalog = dict(data=dict(id=MODEL["id"], endpoints=[endpoint]))
        model = resolve_endpoint(MODEL, catalog, ROUTE, dict(max_completion_tokens=100, reasoning={}))
        self.assertEqual(model["reservation_pricing"], dict(prompt=0.0000004, completion=0.000002))
        with self.assertRaisesRegex(ContractError, "pinned provider"):
            resolve_endpoint(MODEL, catalog, ROUTE, dict(seed=0))
        with self.assertRaisesRegex(ContractError, "unavailable"):
            resolve_endpoint(MODEL, catalog, dict(ROUTE, only=["deepseek"]), {})
        # A cap that passes with the global cheapest price must fail at the pinned rate.
        self.guard.max_usd = 0.0001
        with self.assertRaisesRegex(ProviderError, "SPEND_LIMIT"):
            OpenRouterClient(model, self.guard, route=ROUTE,
                opener=lambda *a, **kw: self.fail("HTTP attempted")).respond(self.request)

    def test_profile_versions_preserve_history_and_explicit_seed_handling(self):
        root = Path(__file__).resolve().parents[1]
        current = json.loads((root/"protocols/checks/openrouter-tau2-smoke.json").read_text())
        previous = json.loads((root/"protocols/checks/openrouter-tau2-smoke-v1.0.json").read_text())
        for profile in (current, previous):
            validate_profile(profile)
        self.assertEqual(provider_route(previous, "user_simulator"), ROUTE)
        self.assertEqual(provider_route(current, "user_simulator")["only"], ["fireworks"])
        self.assertFalse(forward_seed(current, "user_simulator"))
        self.assertEqual(native_parameters(current, "user_simulator")["seed"], 0)
        self.assertNotIn("seed", current["user_simulator"]["parameters"])
        for field, value in (("forward_native_seed", True), ("model_id", "openai/gpt-4.1")):
            changed = deepcopy(current)
            changed["user_simulator"][field] = value
            with self.assertRaises(ValidationError):
                validate_profile(changed)

    def test_observed_gateway_saves_usage_cost_and_role_restriction(self):
        with TemporaryDirectory() as folder:
            with Journal(folder, "observed-test") as journal:
                ctx = RunContext("observed-test", "r", "s", "connection", "diagnostic", "solve", "observed")
                public = dict(role="user", content="hello")
                start = ctx.event("run.started", dict(bank_id=None,start_snapshot_id=None,fresh_session=True,
                    source_run_ids=[],task_ref=journal.blobs.put(public)))
                journal.append([start])
                registry = SourceRegistry(journal, ctx)
                template = registry.template({k:v for k,v in self.request.items() if k!="messages"}, roles=("user_simulator",))
                msg = registry.template(dict(message=public), roles=("user_simulator",))
                client = OpenRouterClient(MODEL, self.guard, route=ROUTE, opener=lambda *a, **kw: Reply(response()))
                _, source, _ = RecordingGateway(journal).call(ctx, registry, "user_simulator", template,
                    [(msg,"/message")],client,max_requests=3)
                finished = [e for e in journal.events.values() if e["type"]=="model.request_finished"][0]
                self.assertEqual(finished["payload"]["cost_status"], "observed")
                self.assertEqual(finished["payload"]["usage"], response()["usage"])
                self.assertEqual(registry.sources[source]["roles"], ["user_simulator"])
                with self.assertRaisesRegex(ValueError, "role or run"):
                    registry.value(source,"worker",len(journal.events)+1)
                private = ctx.event("business.transition",dict(actor_role="user_simulator",
                    model_message_ref=journal.blobs.put(dict(role="user",content="private-user-only"))))
                journal.append([private])
                forged_role = registry.add("benchmark_message",private["event_id"],roles=("worker",))
                with self.assertRaisesRegex(ValueError,"different recipient"):
                    registry.value(forged_role,"worker",len(journal.events)+1)
                for blob in (Path(folder)/"blobs").rglob("*"):
                    if blob.is_file():
                        self.assertNotIn(b"test-router-secret",blob.read_bytes())


if __name__ == "__main__":
    unittest.main()

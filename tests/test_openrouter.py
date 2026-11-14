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

from agent_memory_eval.contracts.core import ContractError, RunContext, canonical
from agent_memory_eval.models.openrouter import OpenRouterClient, ProviderError, SpendGuard, resolve_model
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
        body = OpenRouterClient(MODEL, self.guard, opener=opener).respond(self.request)
        self.assertEqual(seen[0].full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer test-router-secret")
        self.assertEqual(seen[0].data, canonical(self.request))
        self.assertNotIn(b"secret", seen[0].data)
        self.assertEqual(self.guard.reported_usd, body["usage"]["cost"])

    def test_missing_cost_is_unknown_and_blocks_next_paid_attempt(self):
        client = OpenRouterClient(MODEL, self.guard, opener=lambda *a, **kw: Reply(response(None)))
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
                    OpenRouterClient(MODEL, guard, opener=lambda *a, **kw: self.fail("HTTP attempted")).respond(self.request)

    def test_fallback_implicit_session_and_other_model_are_rejected(self):
        variants = [dict(self.request, provider=dict(ROUTE, allow_fallbacks=True)),
                    dict(self.request, previous_response_id="old-session"), dict(self.request, model="other-model")]
        for request in variants:
            with self.subTest(request_keys=list(request)):
                with self.assertRaises(ProviderError):
                    OpenRouterClient(MODEL, self.guard, opener=lambda *a, **kw: self.fail("HTTP attempted")).respond(request)
        self.assertEqual(self.guard.calls, 0)

    def test_http_error_body_and_credential_echo_are_not_persisted(self):
        def error(*args, **kwargs):
            raise HTTPError("https://openrouter.ai", 401, "unauthorized", {}, io.BytesIO(b"test-router-secret"))
        with self.assertRaises(ProviderError) as caught:
            OpenRouterClient(MODEL, self.guard, opener=error).respond(self.request)
        self.assertEqual(caught.exception.response, dict(error=dict(code=401)))
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaisesRegex(ProviderError, "CREDENTIAL_IN_RESPONSE"):
            OpenRouterClient(MODEL, SpendGuard(max_calls=2,max_usd=1,max_seconds=10),
                opener=lambda *a, **kw: Reply(dict(choices=[{"content":"test-router-secret"}]))).respond(self.request)

    def test_unsupported_model_parameters_are_not_silently_dropped(self):
        catalog = dict(data=[MODEL])
        resolve_model(catalog, MODEL["id"], dict(reasoning=dict(effort="low")))
        with self.assertRaisesRegex(ContractError, "not supported"):
            resolve_model(catalog, MODEL["id"], dict(temperature=0))

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
                client = OpenRouterClient(MODEL, self.guard, opener=lambda *a, **kw: Reply(response()))
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

"""Pinned native tau2 baseline connection; memory routing is explicitly unsupported."""

from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from importlib.metadata import distribution
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, digest, new_id
from agent_memory_eval.contracts.connection import forward_seed, native_parameters, provider_route, validate_profile
from agent_memory_eval.models.openrouter import OpenRouterClient, SpendGuard, model_catalog, model_endpoints, resolve_endpoint, resolve_model
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry


PIN = "c06b07dd7c4d4ec61ba6ff9bcdf0b15dd7ef36d4"


def verify_checkout(checkout):
    checkout = Path(checkout).resolve()
    revision = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    clean = subprocess.run(["git", "-C", str(checkout), "status", "--porcelain"], check=True, capture_output=True, text=True).stdout
    if revision != PIN or clean:
        raise ContractError("tau2 checkout must be clean at the pinned revision")
    direct = json.loads(distribution("tau2").read_text("direct_url.json") or "{}")
    if direct.get("vcs_info", {}).get("commit_id") != PIN:
        raise ContractError("Install the locked tau2 extra before this connection check")
    return checkout


def native_modules(checkout):
    os.environ["TAU2_DATA_DIR"] = str(checkout / "data")
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    from tau2.agent import llm_agent
    from tau2.user import user_simulator
    from tau2.utils import llm_utils
    from tau2.utils.utils import DATA_DIR
    if DATA_DIR.resolve() != (checkout / "data").resolve():
        raise ContractError("tau2 was already imported with a different data directory")
    import tau2
    installed = Path(tau2.__file__).parent
    for path in installed.rglob("*.py"):
        source = checkout / "src/tau2" / path.relative_to(installed)
        if not source.is_file() or digest(source.read_bytes()) != digest(path.read_bytes()):
            raise ContractError("Installed tau2 source differs from the pinned checkout")
    if llm_utils.LLM_CACHE_ENABLED:
        raise ContractError("Full-response cache must be disabled")
    llm_utils.litellm.disable_cache()
    return llm_agent, user_simulator, llm_utils


class Tau2Bridge:
    """Retain native prompt/message conversion, parsing and environment behavior."""
    def __init__(self, journal, ctx, orchestrator, config, models, guard, modules, opener=None, *, execution_limits=None):
        self.journal, self.ctx, self.orchestrator = journal, ctx, orchestrator
        self.config, self.models, self.guard = config, models, guard
        self.execution_limits = execution_limits
        self.agent_module, self.user_module, self.utils = modules
        self.registry = SourceRegistry(journal, ctx)
        self.gateway = RecordingGateway(journal)
        self.systems = dict(worker=orchestrator.agent.system_prompt, user_simulator=orchestrator.user.system_prompt)
        self.clients = {role: OpenRouterClient(model, guard, route=provider_route(config, role),
            **({"opener": opener} if opener else {}),
            **({"max_input_bytes": execution_limits.max_input_bytes} if execution_limits else {})) for role, model in models.items()}
        self.message_sources, self.costs = {}, {}
        self.active = None
        self.last_response = None
        self.verify_wire = None
        self.project_worker_history = False
        self.original_generate = self.utils.generate

    def config_role(self, actor):
        if actor not in {"worker", "user_simulator"}:
            raise ContractError("Unsupported native actor")
        return actor

    def check_tools(self, actor, tools_schema):
        if any(tool["function"]["name"].startswith("memory_") for tool in (tools_schema or [])):
            raise ContractError("Native memory tool routing is not implemented in this baseline binding")

    def invoke(self, actor, *, model, messages, tools=None, tool_choice=None, call_name=None, **kwargs):
        if self.active is not None:
            raise ContractError("This connection check is single-threaded")
        role = self.config_role(actor)
        expected_model = self.config[role]["model_id"]
        if model != expected_model or kwargs != native_parameters(self.config, role):
            raise ContractError("Native model settings differ from the pinned connection profile")
        rendered = self.utils.to_litellm_messages(messages)
        if not rendered or rendered[0] != dict(role="system", content=self.systems[actor]):
            raise ContractError("Native system prompt differs from the approved role prompt")
        if any(m.get("role") == "system" for m in rendered[1:]):
            raise ContractError("Unapproved additional system message")
        tools_schema = [tool.openai_schema for tool in tools] if tools else None
        self.check_tools(actor, tools_schema)
        choice = tool_choice or ("auto" if tools_schema else None)
        expected = dict(model=model, messages=rendered, tools=tools_schema, tool_choice=choice, num_retries=0, **kwargs)
        self.active = (actor, expected)
        try:
            return self.original_generate(model=model, messages=messages, tools=tools, tool_choice=tool_choice,
                                          call_name=call_name, num_retries=0, **kwargs)
        finally:
            self.active = None

    def message_entries(self, actor, messages):
        entries = []
        for message in messages:
            identity = (actor, digest(canonical(message)))
            if identity not in self.message_sources:
                if message["role"] == "system":
                    source = self.registry.template(dict(message=message), roles=(actor,))
                    self.message_sources[identity] = (source, "/message")
                else:
                    event = self.ctx.event("business.transition", dict(actor_role=actor,
                        origin="tau2_native_state", model_message_ref=self.journal.blobs.put(message)))
                    self.journal.append([event])
                    source = self.registry.add("benchmark_message", event["event_id"], roles=(actor,))
                    self.message_sources[identity] = (source, "")
            entries.append(self.message_sources[identity])
        return entries

    def completion(self, **kwargs):
        if self.active is None:
            raise ContractError("An untracked native model call was blocked")
        actor, expected = self.active
        if canonical(kwargs) != canonical(expected):
            raise ContractError("Native request wrapper changed approved content or parameters")
        role = self.config_role(actor)
        config = {k: deepcopy(v) for k, v in expected.items() if k not in {"messages", "num_retries"} and v is not None}
        if not forward_seed(self.config, role):
            del config["seed"]
        config.update(provider=deepcopy(provider_route(self.config, role)), stream=False)
        template = self.registry.template(config, roles=(actor,))
        entries = self.message_entries(actor, expected["messages"])
        began = time.monotonic()
        validation = {}
        if self.project_worker_history and actor == "worker":
            validation["project_self_authored"] = True
        if self.verify_wire is not None:
            validation["validate_wire"] = lambda request: self.verify_wire(actor, request, expected["messages"])
        message, source, request_id = self.gateway.call(self.ctx, self.registry, actor, template, entries,
            self.clients[actor], max_requests=None if self.execution_limits else self.config["guards"]["max_model_requests"],
            execution_limits=self.execution_limits, **validation)
        finish = next(e for e in reversed(list(self.journal.events.values()))
                      if e["type"] == "model.request_finished" and e["payload"]["request_id"] == request_id)
        response = self.journal.blobs.get(finish["payload"]["response_ref"])
        self.last_response = (message, source, request_id)
        self.journal.append([self.ctx.event("business.transition", dict(request_id=request_id,
            actor_role=actor, client_elapsed_seconds=time.monotonic() - began))])
        self.costs[response["id"]] = finish["payload"]["cost_usd"]
        allowed_models = {self.models[actor]["id"], self.models[actor].get("canonical_slug")}
        if response.get("model") not in allowed_models:
            raise ContractError("OpenRouter returned a different model identity")
        expected_provider = self.models[actor]["selected_endpoint"]["provider_name"]
        if response.get("provider") != expected_provider:
            raise ContractError("OpenRouter returned an unverified provider identity")
        from litellm import ModelResponse
        return ModelResponse(**response)

    def __enter__(self):
        self.stack = ExitStack()
        for module, role in ((self.agent_module, "worker"), (self.user_module, "user_simulator")):
            self.stack.enter_context(patch.object(module, "generate", lambda role=role, **kw: self.invoke(role, **kw)))
        self.stack.enter_context(patch.object(self.utils, "completion", self.completion))
        self.stack.enter_context(patch.object(self.utils, "get_response_cost", lambda response: self.costs.get(response.id)))
        return self

    def __exit__(self, *args):
        self.stack.close()


def prepare_native(config, checkout):
    validate_profile(config)
    if config.get("intent") != "connection_check" or config.get("memory_enabled") is not False:
        raise ContractError("This binding supports a no-memory connection check only")
    if config["benchmark"]["source_revision"] != PIN:
        raise ContractError("Unsupported tau2 revision")
    checkout = verify_checkout(checkout)
    modules = native_modules(checkout)
    from loguru import logger
    logger.disable("tau2")
    from tau2.data_model.simulation import TextRunConfig
    from tau2.runner.build import build_text_orchestrator
    from tau2.runner.helpers import get_tasks, load_task_splits
    bench = config["benchmark"]
    if bench["task_id"] not in load_task_splits(bench["domain"])[bench["split"]]:
        raise ContractError("Task is outside the declared split")
    task = get_tasks(bench["domain"], task_split_name=bench["split"], task_ids=[bench["task_id"]])[0]
    native = TextRunConfig(domain=bench["domain"], agent="llm_agent", user="user_simulator",
        llm_agent=config["worker"]["model_id"], llm_args_agent=config["worker"]["parameters"],
        llm_user=config["user_simulator"]["model_id"], llm_args_user=config["user_simulator"]["parameters"],
        max_steps=bench["max_steps"], max_errors=bench["max_errors"], seed=bench["seed"],
        timeout=config["guards"]["max_seconds"], localize_db=bench["localize_db"],
        communicate_scoring=dict(scorer=bench["communicate_scorer"]),
        task_ids=[bench["task_id"]], task_split_name=bench["split"], num_trials=1, max_concurrency=1)
    orchestrator = build_text_orchestrator(native, task, seed=bench["seed"])
    catalog = model_catalog()
    models = {role: resolve_model(catalog, config[field]["model_id"], config[field]["parameters"])
              for role, field in (("worker", "worker"), ("user_simulator", "user_simulator"))}
    for role, model in models.items():
        parameters = dict(config[role]["parameters"])
        if role == "worker":
            parameters.update(tools=[], tool_choice="auto")
        models[role] = resolve_endpoint(model, model_endpoints(model["id"]), provider_route(config, role), parameters)
    return orchestrator, native, models, modules


def run_native_check(journal, config, prepared, *, live):
    orch, native, models, modules = prepared
    ctx = RunContext(journal.experiment_id, new_id("r"), new_id("s"), "connection_check", "diagnostic", "solve", "observed")
    state = deepcopy(journal.state)
    state["connection_check"] = dict(requested=config, native_resolved=native.model_dump(mode="json"),
        model_catalog_entries=models, task_ref=journal.blobs.put(orch.task.model_dump(mode="json")),
        capabilities=dict(native_grading=True, memory_tool_routing=False), response_cache_enabled=False)
    journal.append([], state=state)
    if not live:
        return dict(intent="connection_check", status="prepared_only", benchmark_or_model_calls=0,
                    worker=models["worker"]["id"], task_id=orch.task.id, native_revision=PIN)
    journal.append([ctx.at("prepare").event("run.started", dict(bank_id=None, start_snapshot_id=None,
        fresh_session=True, source_run_ids=[], task_ref=journal.blobs.put(dict(role="system", content=orch.agent.system_prompt))))])
    guard = SpendGuard(max_calls=config["guards"]["max_model_requests"], max_usd=config["guards"]["max_reported_cost_usd"],
                       max_seconds=config["guards"]["max_seconds"])
    grading, success, score, termination, error_code = "not_available", None, None, "connection_error", None
    report_ref = None
    try:
        from tau2.runner.simulation import run_simulation
        with Tau2Bridge(journal, ctx, orch, config, models, guard, modules):
            result = run_simulation(orch)
        report_ref = journal.blobs.put(result.model_dump(mode="json"))
        score = result.reward_info.reward
        success, grading, termination = score == 1.0, "graded", result.termination_reason
    except Exception as error:
        error_code = getattr(error, "code", type(error).__name__)
        grading = "error" if getattr(orch, "done", False) else "incomplete"
        journal.append([ctx.event("runtime.error", dict(code=error_code))])
        if getattr(orch, "messages", None):
            report_ref = journal.blobs.put(dict(partial_trajectory=[m.model_dump(mode="json") for m in orch.messages]))
    journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status=grading,
        official_success=success, official_score=score, official_artifact_ref=report_ref if grading == "graded" else None))])
    journal.append([ctx.at("seal").event("run.ended", dict(termination_reason=termination,
        grading_status=grading, end_snapshot_id=None))])
    journal.export_views()
    finished = [e for e in journal.events.values() if e["type"] == "model.request_finished"]
    roles = Counter(e["payload"]["actor_role"] for e in journal.events.values() if e["type"] == "model.request_started")
    responses = [journal.blobs.get(e["payload"]["response_ref"]) for e in finished
                 if e["payload"]["status"] == "response"]
    return dict(intent="connection_check", status="completed" if grading == "graded" else "error", run_id=ctx.run_id,
        worker=models["worker"]["id"], user_simulator=models["user_simulator"]["id"],
        native_revision=PIN, domain=config["benchmark"]["domain"], task_id=orch.task.id,
        termination_reason=termination, grading_status=grading, official_success=success, official_score=score,
        requests_by_role=dict(roles), http_calls=guard.calls, reported_cost_usd=guard.reported_usd,
        native_steps=orch.step_count, native_tool_errors=orch.num_errors,
        returned_models=dict(Counter(r.get("model", "unknown") for r in responses)),
        returned_providers=dict(Counter(str(r.get("provider", "unknown")) for r in responses)),
        reported_tokens=sum((r.get("usage") or {}).get("total_tokens", 0) for r in responses),
        unknown_cost=guard.unknown_cost or any(e["payload"]["cost_status"] == "unknown" for e in finished),
        error_code=error_code, report_ref=report_ref, memory_enabled=False)

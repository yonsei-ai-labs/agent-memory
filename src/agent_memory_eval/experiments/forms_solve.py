"""One fresh, fully native τ² episode using a forms-study allocation's sealed bank."""

from collections import Counter
from contextlib import nullcontext
from copy import copy, deepcopy
from dataclasses import asdict
from decimal import Decimal
import re
import time

from agent_memory_eval.adapters.tau2 import PIN, Tau2Bridge
from agent_memory_eval.adapters.tau2_assessment import assess_reached_state
from agent_memory_eval.contracts.connection import provider_route
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, canonical, digest, new_id, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.experiments.writing_comparison import _note
from agent_memory_eval.memory.context import memory_system_suffix, past_memory_instruction
from agent_memory_eval.memory.imports import install_snapshot
from agent_memory_eval.memory.preparation import SnapshotBudgetExceeded
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.models.openrouter import OpenRouterClient, ProviderError
from agent_memory_eval.recording.experience import public_text_message
from agent_memory_eval.runner.tau2_baseline import native_config
from agent_memory_eval.runner.tau2_memory import fresh_orchestrator


WRITTEN_ARMS = {"fact", "episode", "lesson", "procedure", "summary"}
ROLES = ("worker", "user_simulator")


def _bank_input(arm, task_id, repetition, writer_bank, public_record, expected_hash, *, written_arms=WRITTEN_ARMS):
    if expected_hash is not None and (not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise ContractError("Invalid public context digest")
    if (arm in written_arms) != (writer_bank is not None) or (arm == "public_record") != (public_record is not None):
        raise ContractError("This allocation requires its own specified source input")
    if arm in written_arms:
        bank = writer_bank
        required = {"arm", "task_id", "repetition", "public_context_sha256", "snapshot_id", "run_id", "items", "head_revision", "status"}
        if not isinstance(bank, dict) or not required <= bank.keys():
            raise ContractError("Writer bank is missing its sealed allocation descriptor")
        if (not expected_hash or type(bank["repetition"]) is not int
                or (bank["arm"], bank["task_id"], bank["repetition"], bank["public_context_sha256"])
                != (arm, task_id, repetition, expected_hash)):
            raise ContractError("Writer bank belongs to a different allocation or public experience")
        items, revision = bank["items"], bank["head_revision"]
        if (not isinstance(items, list) or bank["status"] != ("completed" if items else "empty")
                or not all(isinstance(bank[k], str) and bank[k] for k in ("snapshot_id", "run_id"))
                or type(revision) is not int or revision < len(items) or (not items and revision != 0)):
            raise ContractError("Solver requires a completed or voluntarily empty sealed writer bank")
        if (any(not isinstance(i, dict) or set(i) != {"id", "version", "title", "body"}
                or not all(isinstance(v, str) and v.strip() for v in i.values()) for i in items)
                or any(len({i[k] for i in items}) != len(items) for k in ("id", "version"))):
            raise ContractError("Writer bank has invalid public items or duplicate identities")
        return items, revision
    if arm == "public_record":
        if not isinstance(public_record, str) or digest(public_record.encode()) != expected_hash:
            raise ContractError("Public record differs from the verified writer context")
        record = read_json(public_record.encode())
        if (canonical(record).decode() != public_record or set(record) != {"kind", "task_context", "messages"}
                or record["kind"] != "past_public_experience" or record["task_context"].get("role") != "system"
                or not isinstance(record["messages"], list) or not record["messages"]
                or any(m.get("role") == "system" for m in record["messages"])
                or any(public_text_message(m) != m for m in [record["task_context"], *record["messages"]])):
            raise ContractError("Raw memory requires the canonical public experience")
        return [_note(dict(title="Earlier public interaction", body=public_record))], 0
    if arm not in {"no_memory", "empty_preload"}:
        raise ContractError("Unknown forms-study solve arm")
    return [], 0


def resolve_retrieval_pool(delivery, sources, task_id, repetition, target_hash, written_arms):
    """Import independent public writer versions in their preregistered order."""
    if (not isinstance(sources, list) or not sources or not delivery
            or delivery.get("read_mode") != "on_demand" or delivery.get("pool") not in {"own", "mixed"}
            or delivery.get("sources_sha256") != digest(canonical(sources))):
        raise ContractError("Retrieval pool differs from its registered sources")
    items, lineage = [], []
    for bank in sources:
        if (not isinstance(bank, dict) or not isinstance(bank.get("task_id"), str)
                or not isinstance(bank.get("journal_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", bank["journal_sha256"])):
            raise ContractError("Missing source task or writer journal digest")
        public_hash = bank.get("public_context_sha256")
        block, _ = _bank_input(delivery["writer"], bank["task_id"], repetition, bank, None,
                               public_hash, written_arms=written_arms)
        items.extend(block)
        lineage.append({k: bank[k] for k in ("task_id", "run_id", "snapshot_id", "journal_sha256",
                                              "public_context_sha256", "status")})
        lineage[-1]["heads"] = [[i["id"], i["version"]] for i in block]
    if (any(len({b[k] for b in lineage}) != len(sources) for k in ("task_id", "run_id", "snapshot_id"))
            or [b["public_context_sha256"] for b in lineage if b["task_id"] == task_id] != [target_hash]
            or (delivery["pool"] == "own") != (len(sources) == 1)
            or any(len({i[k] for i in items}) != len(items) for k in ("id", "version"))):
        raise ContractError("Pool must contain the target and distinct independent source versions")
    return items, lineage


class _EpisodeGuard:
    """Share the batch reservation while bounding this episode's sends and HTTP deadline."""

    def __init__(self, batch, limits):
        self.batch = batch
        self.cap, self.calls = limits["max_total_requests_per_episode"], 0
        self.episode_deadline = time.monotonic() + limits["episode_seconds"]

    @property
    def deadline(self):
        return min(self.episode_deadline, self.batch.deadline)

    @property
    def unknown_cost(self):
        return self.batch.unknown_cost

    @unknown_cost.setter
    def unknown_cost(self, value):
        self.batch.unknown_cost = value

    def before(self, request, model):
        if self.calls >= self.cap:
            raise ProviderError("EPISODE_REQUEST_LIMIT")
        if time.monotonic() >= self.episode_deadline:
            raise ProviderError("EPISODE_TIME_LIMIT")
        self.batch.before(request, model)
        self.calls += 1

    def after(self, response):
        self.batch.after(response)


class _PreloadBridge(Tau2Bridge):
    """Change only the worker wire. Native agents and business dispatch keep their own history."""

    def prepare_preload(self, memory, bank, policy, rendered_instruction):
        self.native_system = self.systems["worker"]
        self.systems["worker"] += memory_system_suffix(policy, rendered_instruction)
        preload = memory.preload(self.ctx, bank)
        self.preload_source = self.registry.add("memory_snapshot", preload["event_id"])

    def invoke(self, actor, *, messages, **kwargs):
        if actor == "worker":
            if not messages or messages[0].content != self.native_system:
                raise ContractError("Native worker policy changed before preload")
            messages = [messages[0].model_copy(update={"content": self.systems["worker"]}), *messages[1:]]
        return super().invoke(actor, messages=messages, **kwargs)

    def message_entries(self, actor, messages):
        entries = super().message_entries(actor, messages)
        if actor == "worker":
            entries.insert(1, (self.preload_source, ""))
        return entries


def model_free_basis(domain, criteria):
    if criteria is None:
        return False
    basis = set(criteria.reward_basis)
    if domain == "airline":
        return basis == {"DB", "COMMUNICATE"}
    return (domain == "retail" and basis in ({"DB", "NL_ASSERTION"}, {"DB"})
            and not criteria.nl_assertions and not criteria.communicate_info)


def _prepared_for_job(connection, prepared, limits, task_id, expected_split="train", expected_domain="airline", full_retail=False):
    template, native, models, _ = prepared
    bench = connection["benchmark"]
    criteria = template.task.evaluation_criteria
    basis_ok = (expected_domain == "retail" and criteria is not None
                and set(criteria.reward_basis) in ({"DB"}, {"DB", "NL_ASSERTION"})) if full_retail else model_free_basis(expected_domain, criteria)
    if (expected_split not in {"train", "test"} or (bench["domain"], bench["split"], bench["source_revision"], bench["communicate_scorer"])
            != (expected_domain, expected_split, PIN, "substring")
            or not basis_ok
            or bench["task_id"] != task_id or template.task.id != task_id or native != native_config(connection)
            or (native.max_steps, native.max_errors, native.timeout) != (
                limits["native_steps"], limits["native_errors"], limits["episode_seconds"])
            or connection["worker"]["parameters"]["seed"] != native.seed
            or not connection["worker"]["forward_native_seed"] or connection["user_simulator"]["forward_native_seed"]
            or "seed" in connection["user_simulator"]["parameters"]):
        raise ContractError("Prepared native task, seed, evaluator or limits differ from the allocation")
    if set(models) != set(ROLES) or any(t.name.startswith("memory_") for t in (*template.agent.tools, *(template.user.tools or []))):
        raise ContractError("Prepared roles or native tool namespace differ from the allocation")
    for role in ROLES:
        model = models[role]
        endpoint = model["selected_endpoint"]
        if (model["id"] != connection[role]["model_id"] or [endpoint["tag"]] != provider_route(connection, role)["only"]
                or not isinstance(endpoint["provider_name"], str) or not endpoint["provider_name"].strip()):
            raise ContractError("Prepared model route differs from the allocation")


def _usage(journal, run_id):
    starts, fields = {}, {}
    by_role, by_delivery = {}, Counter()
    normal_response_spans = 0
    roles = list(ROLES)
    if any(e["type"] == "model.request_started" and e["payload"]["actor_role"] == "grader" for e in journal.events.values()):
        roles.append("grader")
    for role in roles:
        by_role[role] = dict(requests=0, http_attempts=0, responses=0, input_bytes=0, input_bytes_by_delivery=Counter(),
                             reported_prompt_tokens=0, reported_completion_tokens=0, reported_tokens=0,
                             unknown_token_requests=0, reported_cost_usd_decimal=Decimal(0), unknown_cost_requests=0)
    for e in journal.events.values():
        if e["run_id"] != run_id:
            continue
        p = e["payload"]
        if e["type"] == "model.request_started":
            request_id = p["request_id"]
            size = len(canonical(journal.blobs.get(p["request_ref"])))
            starts[request_id] = dict(p, input_bytes=size)
            fields[request_id] = Counter()
            usage = by_role[p["actor_role"]]
            usage["requests"] += 1
            usage["input_bytes"] += size
        elif e["type"] == "memory.input_included":
            fields[p["request_id"]][p["field"]] += 1
        elif e["type"] == "model.request_finished":
            started = starts[p["request_id"]]
            usage = by_role[started["actor_role"]]
            usage["http_attempts"] += p["delivery_status"] != "not_submitted"
            usage["responses"] += p["status"] == "response"
            usage["input_bytes_by_delivery"][p["delivery_status"]] += started["input_bytes"]
            spans = sum(fields[p["request_id"]].values())
            by_delivery[p["delivery_status"]] += spans
            if p["status"] == "response":
                normal_response_spans += spans
            if p["cost_status"] == "observed":
                usage["reported_cost_usd_decimal"] += Decimal(str(p["cost_usd"]))
            usage["unknown_cost_requests"] += p["cost_status"] == "unknown"
            tokens = p.get("usage") or {}
            known_tokens = type(tokens.get("total_tokens")) is int and tokens["total_tokens"] >= 0
            usage["unknown_token_requests"] += p["delivery_status"] != "not_submitted" and not known_tokens
            for field, target in (("prompt_tokens", "reported_prompt_tokens"), ("completion_tokens", "reported_completion_tokens"),
                                  ("total_tokens", "reported_tokens")):
                if type(tokens.get(field)) is int and tokens[field] >= 0:
                    usage[target] += tokens[field]
    cost = sum((row["reported_cost_usd_decimal"] for row in by_role.values()), Decimal(0))
    for row in by_role.values():
        row["reported_cost_usd_decimal"] = str(row["reported_cost_usd_decimal"])
        row["input_bytes_by_delivery"] = dict(row["input_bytes_by_delivery"])
    worker = [p["request_ref"]["sha256"] for p in starts.values() if p["actor_role"] == "worker"]
    return dict(usage_by_role=by_role, reported_cost_usd_decimal=str(cost),
                requests_by_role={r: v["requests"] for r, v in by_role.items()},
                http_calls=sum(v["http_attempts"] for v in by_role.values()),
                unknown_cost=any(v["unknown_cost_requests"] for v in by_role.values()),
                memory_input_spans=sum(by_delivery.values()), memory_input_spans_by_delivery=dict(by_delivery),
                normal_response_memory_spans=normal_response_spans,
                memory_input_fields=dict(sum(fields.values(), Counter())),
                first_worker_request_sha256=worker[0] if worker else None)


def run_forms_solve(journal, *, arm, task_id, repetition, run_id, connection, prepared, guard, limits,
                    instruction, writer_bank=None, public_record=None, public_context_sha256=None, opener=None, clients=None,
                    written_arms=WRITTEN_ARMS, delivery=None, expected_split="train", source_task_id=None,
                    expected_domain="airline", native_grader=None, writer_pool=None, preload_notice_version=None,
                    first_pass_match=None, matched_trial=None):
    """Run one assigned episode in a new per-job journal with a batch guard and verified input.

    The caller verifies a writer bank against its sealed journal and supplies the cohort's public hash.
    An optional mapping of guarded OpenRouter clients supplies per-role transports and timeouts.
    """
    if journal.contract_version != "1.2" or journal.scan["records"]:
        raise ContractError("A forms solve needs its own new v1.2 journal")
    if not run_id or type(repetition) is not int or repetition < 1 or not isinstance(task_id, str) or not task_id:
        raise ContractError("Missing solve allocation identity")
    if not isinstance(instruction, str) or (instruction != "" if arm == "no_memory" else not instruction.strip()):
        raise ContractError("Memory arms require the common instruction and pure no-memory requires none")
    if preload_notice_version is not None and (arm == "no_memory" or delivery is not None):
        raise ContractError("Versioned preload notices require a start-of-session snapshot")
    if first_pass_match is not None and delivery is None:
        if preload_notice_version not in (None, "memory-only-v1"):
            raise ContractError("Same-task comparisons use the domain-neutral memory notice")
        preload_notice_version = "memory-only-v1"
    rendered_instruction = past_memory_instruction(instruction, notice_version=preload_notice_version)
    _prepared_for_job(connection, prepared, limits, task_id, expected_split, expected_domain, native_grader is not None)
    if delivery is not None and (delivery["read_mode"] not in {"preload", "on_demand"} or arm == "no_memory"
            or arm == "empty_preload" and delivery != {"writer": "empty_preload", "read_mode": "preload"}):
        raise ContractError("A frozen-bank delivery must declare its writer and read policy")
    writer_condition = delivery["writer"] if delivery else arm
    if source_task_id is not None and (not isinstance(source_task_id, str) or not source_task_id
            or source_task_id == task_id or writer_bank is None):
        raise ContractError("Cross-task delivery needs a distinct declared source and its sealed writer bank")
    lineage = None
    if writer_pool is not None or delivery and "pool" in delivery:
        if writer_bank is not None or source_task_id is not None or public_record is not None:
            raise ContractError("Pool and single-source inputs cannot be combined")
        items, lineage = resolve_retrieval_pool(delivery, writer_pool, task_id, repetition, public_context_sha256, written_arms)
        revision = 0  # A composed read-only starting view has its own revision.
    else:
        items, revision = _bank_input(writer_condition, source_task_id or task_id, repetition, writer_bank, public_record,
                                      public_context_sha256, written_arms=written_arms)
    execution = ExecutionLimits(**{k: limits[k] for k in (
        "worker_requests", "user_simulator_requests", "writer_requests", "max_input_bytes")})
    if execution.writer_requests or any(type(limits[k]) is not int or limits[k] < 1 for k in (
            "native_steps", "native_errors", "episode_seconds", "max_total_requests_per_episode")):
        raise ContractError("Solve limits must bound the native episode and disable writing")
    if any(type(limits[k]) is not int or limits[k] < 0 for k in (
            "read_bank_items", "read_bank_payload_bytes", "read_bank_return_bytes")):
        raise ContractError("Read banks require finite item, payload and return-byte limits")
    policy = MemoryPolicy(arm != "no_memory", "none", False, page_size=max(1, limits["read_bank_items"]),
                          read_mode=delivery["read_mode"] if delivery else ("none" if arm == "no_memory" else "preload"),
                          max_items=limits["read_bank_items"],
                          max_payload_bytes=limits["read_bank_payload_bytes"], max_return_bytes=limits["read_bank_return_bytes"])
    if matched_trial is not None:
        from agent_memory_eval.experiments.first_pass_match import FirstPassMismatch
        from agent_memory_eval.runner.tau2_baseline import ScopedGuard, spend_guard
        if (first_pass_match is not None or source_task_id is not None or writer_pool is not None
                or public_record is not None or preload_notice_version is not None or clients is None
                or (policy.enabled and (writer_bank is None or delivery is None
                    or public_context_sha256 != matched_trial.get("public_context_sha256")))
                or any(c.max_input_bytes != execution.max_input_bytes
                       or c.timeout_seconds != matched_trial["http_timeout_seconds"] for c in clients.values())):
            raise FirstPassMismatch("Fresh comparison requires its registered transport and own-source bank")
        local_guard = ScopedGuard(guard, spend_guard(connection["guards"]))
    elif first_pass_match is None:
        local_guard = _EpisodeGuard(guard, limits)
    else:
        from agent_memory_eval.experiments.first_pass_match import (
            BASELINE_HTTP_SECONDS, BASELINE_INPUT_BYTES, check_common_read_instruction, check_native,
        )
        from agent_memory_eval.runner.tau2_baseline import ScopedGuard, spend_guard
        check_common_read_instruction(instruction, paired_delivery=delivery is not None)
        if (execution.max_input_bytes != BASELINE_INPUT_BYTES or source_task_id is not None or not policy.enabled
                or native_grader is not None or writer_pool is not None
                or (clients is not None and any(c.max_input_bytes != BASELINE_INPUT_BYTES or c.timeout_seconds != BASELINE_HTTP_SECONDS
                                               for c in clients.values()))):
            raise ContractError("Same-task memory comparison differs from its archived solver environment")
        local_guard = ScopedGuard(guard, spend_guard(connection["guards"]))
    if clients is not None and (opener is not None or set(clients) != set(ROLES)
            or any(c.guard is not guard or c.model != prepared[2][r] or c.route != provider_route(connection, r)
                   for r, c in clients.items())):
        raise ContractError("Injected clients must match both roles and the shared guard")

    memory = MemoryService(journal, record_kind="observed")
    parent = bank = None
    bank_sizes = None
    if policy.enabled:
        bank_sizes = dict(items=len(items), return_bytes=len(canonical(dict(items=items))),
                          payload_bytes=sum(len(canonical({k: i[k] for k in ("title", "body")})) for i in items))
        heads = {i["id"]: i["version"] for i in items}
        parent = install_snapshot(memory, policy, heads, list(heads), revision,
                                  {i["version"]: journal.blobs.put(i) for i in items})
        bank = memory.fork(parent, run_id, policy)
    ctx = RunContext(journal.experiment_id, run_id, new_id("s"), arm, "probe", "solve", "observed")
    state = deepcopy(journal.state)
    state["assignments"][run_id] = dict(
        task_id=task_id, repetition=repetition, arm=arm, policy=asdict(policy),
        execution_limits=asdict(execution), execution_contract="per-role-limits/v1",
        native_config_ref=journal.blobs.put(prepared[1].model_dump(mode="json")),
        limits=limits, bank_sizes=bank_sizes, connection_ref=journal.blobs.put(connection), model_routes_ref=journal.blobs.put(prepared[2]),
        instruction_ref=journal.blobs.put(instruction), public_context_sha256=public_context_sha256,
        source_writer_snapshot_id=writer_bank["snapshot_id"] if writer_bank else None,
        source_writer_run_id=writer_bank["run_id"] if writer_bank else None,
        source_writer_bank_sha256=digest(canonical(writer_bank)) if writer_bank else None,
        input_items_sha256=digest(canonical(items)) if policy.enabled else None)
    if first_pass_match is not None:
        state["assignments"][run_id]["archived_first_pass"] = dict(
            source_run_id=first_pass_match["run_id"], control_sha256=first_pass_match["control_sha256"],
            aggregate_request_limit=connection["guards"]["max_model_requests"])
    if preload_notice_version is not None:
        state["assignments"][run_id]["preload_notice_version"] = preload_notice_version
    if native_grader is not None:
        cfg, mdl = native_grader
        state["assignments"][run_id]["native_grade_limits"] = dict(contract="native-grader-limits/v1",
            requests=1, input_bytes=cfg["max_input_bytes"], config_ref=journal.blobs.put(cfg), model_ref=journal.blobs.put(mdl))
    if source_task_id is not None:
        state["assignments"][run_id]["source_task_id"] = source_task_id
    if delivery:
        state["assignments"][run_id]["fixed_bank_delivery"] = dict(delivery)
    source_runs = [writer_bank["run_id"]] if writer_bank else []
    if lineage is not None:
        state["assignments"][run_id]["ordered_writer_sources_ref"] = journal.blobs.put(lineage)
        source_runs = [b["run_id"] for b in lineage]
    journal.append([ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=parent,
        fresh_session=True, source_run_ids=source_runs,
        task_ref=journal.blobs.put(dict(role="system", content=prepared[0].agent.system_prompt))))], state=state)
    score = report_ref = raw_report_ref = raw_reward = error_code = None
    orch = partial_ref = end_snapshot = assessment_ref = None
    native_calls = {}
    termination, grading = "runtime_error", "incomplete"
    try:
        if bank_sizes and any(size > getattr(policy, "max_" + key) for key, size in bank_sizes.items()):
            raise SnapshotBudgetExceeded("Read bank exceeds its item, payload or return limit")
        orch = fresh_orchestrator(prepared)
        trial_wire = None
        if matched_trial is not None:
            from agent_memory_eval.experiments.matched_trial import native_control, verify_trial
            control = native_control(orch, prepared[1], connection, limits, matched_trial["http_timeout_seconds"],
                                     native_grader[0] if native_grader else None)
            trial_wire = verify_trial(journal, ctx, matched_trial, control, policy, instruction, items)
        if first_pass_match is not None:
            receipt = check_native(first_pass_match, connection, orch, prepared[1])
            journal.append([ctx.event("business.transition", dict(
                origin="forms.first_pass_match", receipt_ref=journal.blobs.put(receipt)))])
        # The stock bridge also accepts the model's canonical slug. A missing alias must never admit a missing model ID.
        models = {r: dict(m, canonical_slug=m.get("canonical_slug") or m["id"]) for r, m in prepared[2].items()}
        bridge_limits = execution if first_pass_match is None else None
        if delivery or matched_trial is not None:
            from agent_memory_eval.adapters.tau2_memory import Tau2MemoryBridge
            bridge = Tau2MemoryBridge(journal, ctx, orch, connection, models, local_guard, prepared[3], opener=opener,
                execution_limits=bridge_limits, memory=memory, bank_id=bank, policy=policy,
                solve_instruction=instruction, post_instruction="")
        else:
            bridge_type = _PreloadBridge if policy.enabled else Tau2Bridge
            bridge = bridge_type(journal, ctx, orch, connection, models, local_guard, prepared[3],
                                 opener=opener, execution_limits=bridge_limits)
        if clients is not None:
            bridge.clients = {r: copy(c) for r, c in clients.items()}
            for client in bridge.clients.values():
                client.guard = local_guard
        if delivery or matched_trial is not None:
            bridge.ensure_preload()
        elif policy.enabled:
            bridge.prepare_preload(memory, bank, policy, rendered_instruction)
        if trial_wire is not None:
            bridge.verify_wire = trial_wire
        elif first_pass_match is not None:
            from agent_memory_eval.experiments.first_pass_match import wire_verifier
            bridge.verify_wire = wire_verifier(first_pass_match, policy, instruction if delivery else rendered_instruction, items)
        from tau2.runner.simulation import run_simulation
        grade_context = nullcontext()
        if native_grader is not None:
            from agent_memory_eval.adapters.retail_grader import native_grade_transport
            judge_config, judge_model = native_grader
            judge_guard = local_guard if matched_trial is not None else guard
            judge_client = OpenRouterClient(judge_model, judge_guard, route=judge_config["route"],
                max_input_bytes=judge_config["max_input_bytes"], timeout_seconds=judge_config["timeout_seconds"])
            grade_context = native_grade_transport(journal, ctx, orch.task, native_grader, judge_client, native_calls)
        with bridge, grade_context:
            result = run_simulation(orch, communicate_scoring=prepared[1].communicate_scoring)
        raw_report_ref = journal.blobs.put(result.model_dump(mode="json"))
        termination, raw_reward = result.termination_reason.value, result.reward_info.reward
        if termination in {"user_stop", "agent_stop"}:
            score, grading, report_ref = raw_reward, "graded", raw_report_ref
        else:
            error_code = "NATIVE_" + termination.upper()
    except Exception as error:
        error_code = getattr(error, "code", type(error).__name__)
        if getattr(error, "recorded_event_id", None) not in journal.events:
            journal.append([ctx.event("runtime.error", dict(code=error_code))])
        if orch is not None:
            partial_ref = journal.blobs.put([m.model_dump(mode="json") for m in orch.get_trajectory()])
            if orch.done and orch.termination_reason is not None:
                grading, termination = "error", orch.termination_reason.value
    final_db_hash = orch.environment.get_db_hash() if orch is not None else None
    native_steps = orch.step_count if orch is not None else 0
    native_errors = orch.num_errors if orch is not None else 0
    if orch is not None and error_code in {"ROLE_REQUEST_LIMIT", "INPUT_SIZE_LIMIT", "EPISODE_REQUEST_LIMIT",
                                           "EPISODE_TIME_LIMIT", "NATIVE_MAX_STEPS", "NATIVE_TIMEOUT", "NATIVE_TOO_MANY_ERRORS"}:
        try:
            assessment = assess_reached_state(orch, connection)
        except Exception as error:
            assessment = dict(status="not_assessed", reason=type(error).__name__)
        assessment_ref = journal.blobs.put(assessment)
    success = score == 1.0 if grading == "graded" else None
    native_state = dict(termination_reason=termination, native_steps=native_steps, native_tool_errors=native_errors,
                        final_db_hash=final_db_hash, native_report_ref=raw_report_ref, native_reward=raw_reward,
                        partial_trajectory_ref=partial_ref, reached_state_assessment_ref=assessment_ref, error_code=error_code)
    native_state.update(native_calls)
    journal.append([ctx.at("grade").event("business.transition", dict(origin="forms.native_result", **native_state)),
                    ctx.at("grade").event("outcome.scored", dict(grading_status=grading, official_success=success,
                    official_score=score, official_artifact_ref=report_ref))])
    if bank is not None:
        end_snapshot = memory.freeze(bank)
        memory.seal(bank)
    journal.append([ctx.at("seal").event("run.ended", dict(termination_reason=termination,
        grading_status=grading, end_snapshot_id=end_snapshot))])
    return dict(arm=arm, task_id=task_id, repetition=repetition, run_id=run_id, session_id=ctx.session_id,
                status="completed" if grading == "graded" else "error", grading_status=grading, official_success=success,
                official_score=score, report_ref=report_ref, bank_id=bank, start_snapshot_id=parent,
                snapshot_id=end_snapshot, bank_sizes=bank_sizes, **native_state, **_usage(journal, run_id))

"""Two-source collection, own/mixed snapshots and isolated synthetic probes."""

from copy import deepcopy
from pathlib import Path
import sys

from agent_memory_eval.adapters.fake import FakeBenchmark, SyntheticTask
from agent_memory_eval.contracts.core import MemoryPolicy, RunContext, digest, new_id
from agent_memory_eval.contracts.validation import SCHEMAS
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.models.fake import FakeWorker
from agent_memory_eval.recording.provenance import OfflineGateway, RequestLimit, SourceRegistry
from .router import ToolRouter


def tool_schemas(names):
    parameters = {
        "memory_store": dict(title={"type": "string"}, body={"type": "string"},
                             id={"type": ["string", "null"]}, expected_version={"type": ["string", "null"]}),
        "memory_list": dict(cursor={"type": ["string", "null"]}, limit={"type": ["integer", "null"]}),
        "memory_get": dict(id={"type": "string"}), "inspect": {}, "apply": dict(value={"type": "string"}), "finish": {}}
    required = {"memory_store": ["title", "body"], "memory_get": ["id"], "apply": ["value"]}
    return [dict(type="function", function=dict(name=name, parameters=dict(type="object",
        properties=parameters[name], required=required.get(name, []), additionalProperties=False))) for name in names]


class OfflineRunner:
    def __init__(self, journal):
        self.journal = journal
        self.memory = MemoryService(journal)
        self.environments, self.workers = {}, {}

    def run(self, task, policy, *, stage, mode, max_requests, parent_snapshot=None, source_runs=(),
            grader_error=False, run_id=None, baseline_run=None):
        ctx = RunContext(self.journal.experiment_id, run_id or new_id("r"), new_id("s"),
                         "synthetic_" + mode, stage, "prepare")
        bank = self.memory.create_bank(ctx.run_id, policy, parent_snapshot) if policy.enabled else None
        benchmark, worker = FakeBenchmark(task), FakeWorker()
        self.environments[ctx.run_id], self.workers[ctx.run_id] = benchmark, worker
        state = deepcopy(self.journal.state)
        state["assignments"][ctx.run_id] = dict(task_key=task.key, stage=stage, mode=mode,
            policy=policy.__dict__, bank_id=bank, parent_snapshot=parent_snapshot, baseline_run=baseline_run,
            max_requests=max_requests)
        start = ctx.event("run.started", dict(bank_id=bank, start_snapshot_id=parent_snapshot,
            fresh_session=True, source_run_ids=list(source_runs), task_ref=self.journal.blobs.put(task.public_message())))
        public = ctx.at("solve").event("business.transition", dict(public_message_ref=start["payload"]["task_ref"]),
                                       causes=[start["event_id"]])
        self.journal.append([start, public], state=state)
        registry = SourceRegistry(self.journal, ctx)
        task_source = registry.add("task_initial", start["event_id"])
        history = [(task_source, "")]
        router = ToolRouter(self.journal, self.memory, benchmark, bank, policy)
        gateway = OfflineGateway(self.journal)
        termination = "synthetic_completed"

        def phase(phase_name, actor):
            current = ctx.at(phase_name)
            names = list(benchmark.tool_names) if phase_name == "solve" else []
            names += policy.tools(phase_name)
            template = registry.template(dict(model="synthetic-offline", system=dict(role="system", content="synthetic_" + actor),
                                               tools=tool_schemas(names)), roles=(actor,))
            while phase_name == "post_task_write" or not benchmark.ended:
                message, source, request_id = gateway.call(current, registry, actor, template,
                    [(template, "/system")] + history, worker, max_requests=max_requests)
                history.append((source, ""))
                calls = message.get("tool_calls", [])
                if not calls:
                    break
                for wire, kind, producer in router.dispatch(current, request_id, calls):
                    history.append((registry.add(kind, producer), ""))

        try:
            phase("solve", "worker")
        except RequestLimit:
            termination = "request_limit"
        except Exception as error:
            termination = "synthetic_runtime_error"
            self.journal.append([ctx.at("solve").event("runtime.error", dict(code=type(error).__name__))])
        grading = "not_available"
        try:
            if grader_error:
                raise RuntimeError("Injected synthetic grader failure")
            report = benchmark.grade()
            diagnostic = ctx.at("grade").event("business.transition", dict(synthetic_result_ref=self.journal.blobs.put(report)))
            self.journal.append([diagnostic])
        except Exception as error:
            benchmark.ended = True
            grading = "error"
            self.journal.append([ctx.at("grade").event("runtime.error", dict(code="SYNTHETIC_GRADER_ERROR"))])
        self.journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status=grading,
            official_success=None, official_score=None, official_artifact_ref=None))])
        try:
            if policy.tools("post_task_write") and policy.write_timing in {"post_task", "during_and_post"}:
                phase("post_task_write", "writer")
        except RequestLimit:
            termination = "post_task_request_limit"
        except Exception as error:
            termination = "post_task_runtime_error"
            self.journal.append([ctx.at("post_task_write").event("runtime.error", dict(code=type(error).__name__))])
        snapshot = self.memory.freeze(bank) if bank is not None else None
        if bank is not None:
            self.memory.seal(bank)
        self.journal.append([ctx.at("seal").event("run.ended", dict(termination_reason=termination,
            grading_status=grading, end_snapshot_id=snapshot))])
        return dict(run_id=ctx.run_id, bank_id=bank, snapshot_id=snapshot)

    def two_pass(self):
        # Explicit synthetic values only; never resolve or promote a real experiment draft.
        tasks = [SyntheticTask("synthetic-delivery", "배송지", "동쪽"),
                 SyntheticTask("synthetic-pickup", "수령시간", "오후")]
        policy = MemoryPolicy(True, "post_task", False, page_size=4)
        baseline = MemoryPolicy(False, "none", False, page_size=4, read_mode="none")
        collection_ids = [new_id("r") for _ in tasks]
        probe_ids = [{mode: new_id("r") for mode in ("no_memory", "own_experience", "mixed_experience")} for _ in tasks]
        state = deepcopy(self.journal.state)
        source_root = Path(__file__).resolve().parents[1]
        implementation = dict(python=sys.version.split()[0],
            sources={str(path.relative_to(source_root)): digest(path.read_bytes()) for path in sorted(source_root.rglob("*.py"))},
            schemas={path.name: digest(path.read_bytes()) for path in sorted(SCHEMAS.glob("*.json"))})
        state["offline_config"] = dict(worker="FakeWorker", benchmark="FakeBenchmark", max_requests=12,
            tasks=[task.__dict__ for task in tasks], policy=policy.__dict__, baseline_policy=baseline.__dict__,
            implementation_ref=self.journal.blobs.put(implementation))
        state["schedule"] = [dict(run_id=run_id, task_key=task.key, stage="collection", mode="collection", baseline_run=None)
                             for task, run_id in zip(tasks, collection_ids)]
        for task, planned in zip(tasks, probe_ids):
            state["schedule"].extend(dict(run_id=run_id, task_key=task.key, stage="probe", mode=mode,
                baseline_run=planned["no_memory"] if mode != "no_memory" else None) for mode, run_id in planned.items())
        self.journal.append([], state=state)  # fixed before any worker response or outcome
        collected = [self.run(task, policy, stage="collection", mode="collection", max_requests=12, run_id=run_id)
                     for task, run_id in zip(tasks, collection_ids)]
        mixed = self.memory.compose([run["snapshot_id"] for run in collected])
        probes = []
        for task, own, planned in zip(tasks, collected, probe_ids):
            for mode, start, selected_policy, sources in (
                ("no_memory", None, baseline, []),
                ("own_experience", own["snapshot_id"], policy, [own["run_id"]]),
                ("mixed_experience", mixed, policy, [run["run_id"] for run in collected])):
                probes.append(self.run(task, selected_policy, stage="probe", mode=mode, max_requests=12,
                    parent_snapshot=start, source_runs=sources, run_id=planned[mode],
                    baseline_run=planned["no_memory"] if mode != "no_memory" else None))
        self.journal.export_views()
        return dict(collection=collected, mixed_snapshot=mixed, probes=probes)

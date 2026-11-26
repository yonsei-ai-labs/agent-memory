"""Offline compatibility checks using preserved τ² runs and pinned native benchmark controls."""

import argparse
import hashlib
import importlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from agent_memory_eval.analysis.claim_evidence import paired_counts
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, RunContext, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.router import ToolRouter
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.models.fake import tool_call
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, Journal, read_experiment
from agent_memory_eval.recording.provenance import OfflineGateway, SourceRegistry
from scripts.analyze_memory_claims import analyze as claim_cohorts
from scripts.audit_retail_order_choice import publish


PROFILE = ROOT / "protocols/diagnostics/benchmark-portability-v1.json"
MARKER = "blue calendar marker"


def require(condition, message):
    if not condition:
        raise ContractError(message)


def file_sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_sha(folder):
    files = sorted(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    require(files, f"No input files in {folder}")
    return digest(canonical([[p.relative_to(folder).as_posix(), file_sha(p)] for p in files]))


def grade_agreement(observed_and_expected):
    """Use one binary comparison across graders, keeping unavailable scores outside the denominator."""
    pairs = list(observed_and_expected)
    require(all((a is None) == (b is None) for a, b in pairs), "Native grade availability changed")
    counts = paired_counts(pairs)
    require(not (counts["gain"] or counts["loss"]), "A native control disagrees with its expected grade")
    return dict(allocated=counts["assigned"], matched_success=counts["both_success"],
                matched_failure=counts["both_failure"], ungraded=counts["missing"])


def check_tau2(spec):
    cohorts = claim_cohorts(ROOT, cohorts_only=True)
    require(cohorts["citations_sha256"] == spec["cohort_citations_sha256"], "Prior cohort definitions changed")
    revision = subprocess.check_output(["git", "-C", ROOT / "data/tau2-bench", "rev-parse", "HEAD"], text=True).strip()
    require(revision == spec["revision"], "Different τ² checkout")
    results = {}
    pairs = []
    for case in spec["samples"]:
        folder = (ROOT / case["journal"]["path"]).parent
        asset_text(ROOT, case["journal"])
        evidence = read_experiment(folder)
        scan = evidence[0]
        require(set(scan["starts"]) == {case["run"]}, "Sample does not identify exactly one run")
        scores = [e["payload"] for e in scan["events"].values() if e["type"] == "outcome.scored"]
        if scan["incomplete_runs"]:
            require(not scores and case["expected"] == "open", "Unexpected open run or recorded grade")
            receipt = read_json(asset_text(ROOT, case["receipt"]).encode())
            require(receipt["journal_sha256"] == case["journal"]["sha256"] and receipt["status"] == "error",
                    "Failure receipt belongs to a different journal")
            try:
                request_audit = audit_requests(folder, evidence=evidence)
            except ContractError as error:
                if str(error) != "Request evidence is incomplete":
                    raise
                request_audit = "requires_closed_journal"
            finishes = [e["payload"] for e in scan["events"].values() if e["type"] == "model.request_finished"]
            results[case["id"]] = dict(journal_status="open", provider_error=receipt["provider_error"],
                                       native_success=None, common_request_audit=request_audit,
                                       unknown_cost_requests=sum(f["cost_status"] == "unknown" for f in finishes))
            pairs.append((None, None))
            continue
        score, = scores
        require((score["grading_status"], score["official_success"]) == tuple(case["expected"]),
                "Native grade differs from its previously reviewed value")
        if score["grading_status"] == "graded":
            episode = BlobStore(folder).get(score["official_artifact_ref"])
            reward = episode["reward_info"]["reward"]
            require(score["official_score"] == reward and score["official_success"] is (reward == 1.0),
                    "Recorded outcome differs from the original τ² episode reward")
        result = audit_requests(folder, evidence=evidence)
        pairs.append((score["official_success"], case["expected"][1]))
        results[case["id"]] = dict(journal_status="closed", native_status=score["grading_status"],
                                   native_success=score["official_success"], native_score=score["official_score"],
                                   native_artifact_sha256=(score["official_artifact_ref"] or {}).get("sha256"),
                                   common_request_audit=result)
    return dict(scope="recorded_worker_runs", benchmark_revision=revision,
                samples=results, grade_agreement=grade_agreement(pairs),
                retail_first_action=cohorts["retail_first_decisions"]["all"],
                airline_official_task_success=cohorts["airline_second_pass"]["all"])


class ScriptedReply:
    offline = True

    def __init__(self, message):
        self.message = message

    def respond(self, request):
        return dict(choices=[dict(message=self.message)])


def probe_routing(folder, backend, definitions, *, framework_bank):
    """Offer the two native tools on the actual recorded request and inspect the next request."""
    class NativeCalls:
        stop_tools = frozenset()
        ended = False

        def __init__(self):
            self.steps = 0
            self.names = []

        def execute(self, name, arguments):
            self.steps += 1
            self.names.append(name)
            return getattr(backend, name)(**arguments)

    bridge = NativeCalls()
    names = ("memory_append", "memory_retrieve")
    tools = [dict(type="function", function={k: definitions[n][k] for k in ("name", "description", "parameters")})
             for n in names]
    # BFCL's source format calls an object "dict"; the model-facing schema uses "object".
    for tool in tools:
        tool["function"]["parameters"] = dict(tool["function"]["parameters"], type="object")
    with Journal(folder, "portability-probe", contract_version="1.2") as journal:
        ctx = RunContext("portability-probe", "r_probe", "s_probe", "native_memory", "probe", "solve")
        task = dict(role="user", content="Use the supplied notebook.")
        memory = MemoryService(journal)
        policy = (MemoryPolicy(True, "during_task", True, 2) if framework_bank
                  else MemoryPolicy(False, "none", False, 2, read_mode="none"))
        bank = memory.create_bank(ctx.run_id, policy) if framework_bank else None
        start = ctx.at("prepare").event("run.started", dict(bank_id=bank, start_snapshot_id=None,
            fresh_session=True, source_run_ids=[], task_ref=journal.blobs.put(task)))
        journal.append([start])
        registry = SourceRegistry(journal, ctx)
        template = registry.template(dict(model="synthetic-offline", tools=tools,
                                          system=dict(role="system", content="Compatibility probe.")))
        history = [(template, "/system"), (registry.add("task_initial", start["event_id"]), "")]
        router = ToolRouter(journal, memory, bridge, bank, policy)
        gateway = OfflineGateway(journal)
        returned = {}
        for name, args in zip(names, (dict(text=MARKER), {})):
            calls = [tool_call(name, args)]
            client = ScriptedReply(dict(role="assistant", content=None, tool_calls=calls))
            _, reply_source, req = gateway.call(ctx, registry, "worker", template, history, client, max_requests=3)
            history.append((reply_source, ""))
            (wire, kind, event), = router.dispatch(ctx, req, calls)
            history.append((registry.add(kind, event), ""))
            value = read_json(wire["content"].encode())
            error = value.get("error")
            returned[name] = dict(provenance_kind=kind, error_code=error.get("code") if isinstance(error, dict) else error)
        _, _, decision = gateway.call(ctx, registry, "worker", template, history,
                                     ScriptedReply(dict(role="assistant", content="Done.")), max_requests=3)
        events = list(journal.events.values())
        final = next(e["payload"] for e in events if e["type"] == "model.request_started"
                     and e["payload"]["request_id"] == decision)
        messages = journal.blobs.get(final["request_ref"])["messages"]
        last_tool = messages[-1]
        received = read_json(last_tool["content"].encode()).get("memory_content") == MARKER
        manifest = journal.blobs.get(final["input_manifest_ref"])
        pointer = f"/messages/{len(messages) - 1}/content"
        spans = [s for s in manifest["memory_spans"] if s["pointer"] == pointer and
                 MARKER in last_tool["content"][s["char_start"]:s["char_end"]]]
        end = None
        if framework_bank:
            end = memory.freeze(bank)
            memory.seal(bank)
        journal.append([ctx.at("grade").event("outcome.scored", dict(grading_status="not_available",
            official_success=None, official_score=None, official_artifact_ref=None))])
        journal.append([ctx.at("seal").event("run.ended", dict(termination_reason="routing_probe",
            grading_status="not_available", end_snapshot_id=end))])
    audit = audit_requests(folder)
    return dict(scripted_requests=audit["requests"], common_request_audit=audit["status"],
                native_callback_received=bridge.names, tool_results=returned,
                native_value_in_next_tool_message=received, native_value_spans_in_next_input=len(spans),
                native_return_attributed=bridge.names == list(names) and received and bool(spans))


def routing_criteria(results):
    disabled = results["without_framework_bank"]
    denied = {name: rule["error_code"] for name, rule in disabled["tool_results"].items()}
    isolated = (not disabled["native_callback_received"] and not disabled["native_value_in_next_tool_message"]
                and disabled["native_value_spans_in_next_input"] == 0
                and denied == {"memory_append": "POLICY_DENIED", "memory_retrieve": "POLICY_DENIED"})
    return dict(memory_enabled_native_return_attributed=results["with_framework_bank"]["native_return_attributed"],
                memory_disabled_access_blocked=isolated)


def by_id(path):
    rows = [read_json(line) for line in path.read_bytes().splitlines() if line]
    result = {r["id"]: r for r in rows}
    require(len(rows) == len(result) and rows, "Empty or duplicate native task IDs")
    return result


def check_bfcl(checkout, folder, spec):
    revision = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    require(revision == spec["revision"], "BFCL checkout revision differs")
    package = checkout / "berkeley-function-call-leaderboard"
    base = package / "bfcl_eval"
    for f, sha in spec["inputs"].items():
        require(file_sha(base / f) == sha, "BFCL source changed: " + f)
    for name, expected in spec["packages"].items():
        require(version(name) == expected, "Different BFCL dependency: " + name)
    sys.path.insert(0, str(package))
    try:
        checker_module = importlib.import_module("bfcl_eval.eval_checker.agentic_eval.agentic_checker")
        backend_module = importlib.import_module("bfcl_eval.eval_checker.multi_turn_eval.func_source_code.memory_rec_sum")
        for module in (checker_module, backend_module):
            require(Path(module.__file__).resolve().is_relative_to(base), "Unexpected BFCL import")
        checker, backend = checker_module.agentic_checker, backend_module.MemoryAPI_rec_sum
        questions = by_id(base / "data/BFCL_v4_memory.json")
        answers = by_id(base / "data/possible_answer/BFCL_v4_memory.json")
        require(questions.keys() == answers.keys(), "BFCL task/grade coverage differs")
        controls = []
        for key in sorted(questions):
            gold = answers[key]["ground_truth"]
            controls.append((checker(gold[0], gold)["valid"], checker("zzqfonoprobe", gold)["valid"]))
        comparisons = grade_agreement((score, expected) for a, b in controls for score, expected in ((a, True), (b, False)))
        require(not comparisons["ungraded"], "BFCL control lacks a verdict")

        config = dict(model_result_dir=folder, scenario="customer")
        seed = backend()
        seed._load_scenario(dict(config, test_id="memory_rec_sum_prereq_0-customer-0"))
        seed.memory_append(MARKER)
        seed._flush_memory_to_local_file()
        reads = []
        for i in (0, 1):
            copy = backend()
            copy._load_scenario(dict(config, test_id=f"memory_rec_sum_{i}-customer-{i}"))
            reads.append(copy.memory_retrieve().get("memory_content") == MARKER)
            copy.memory_clear()
        require(all(reads), "Native snapshot was not restored")
        docs = [read_json(line) for line in (base / "data/multi_turn_func_doc/memory_rec_sum.json").read_bytes().splitlines()]
        definitions = {r["name"]: r for r in docs}
        routing = {k: probe_routing(folder / k, backend(), definitions, framework_bank=enabled)
                   for k, enabled in (("without_framework_bank", False), ("with_framework_bank", True))}
    finally:
        sys.path.remove(str(package))
    return dict(scope="native_component_controls", benchmark_revision=revision, tasks=len(questions),
                grade_agreement=comparisons,
                reference_answer_passed=sum(a for a, _ in controls), decoy_answer_passed=sum(b for _, b in controls),
                restored_marker_in_fresh_instances=reads, default_framework_router=routing,
                routing_criteria=routing_criteria(routing))


def check_appworld(runtime, python, folder, spec):
    folder.mkdir()
    for relative, expected in spec["shared_inputs"].items():
        src = runtime / relative
        dest = folder / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest)
            actual = tree_sha(dest)
        else:
            shutil.copyfile(src, dest)
            actual = file_sha(dest)
        require(actual == expected, "AppWorld common inputs changed: " + relative)
    results = {}
    train = set((folder / "data/datasets/train.txt").read_text().splitlines())
    for task, refs in spec["tasks"].items():
        require(task in train, "AppWorld calibration requires a pinned training task")
        rel = Path("data/tasks") / task
        copied = folder / rel
        shutil.copytree(runtime / rel, copied, ignore=shutil.ignore_patterns("__pycache__"))
        require(tree_sha(copied) == refs["task_sha256"], "AppWorld task or evaluation source changed")
        for state, db in (("initial", copied / "dbs"), ("recorded", runtime / "experiments/outputs/validation/tasks" / task / "dbs")):
            dest = folder / "experiments/outputs" / state / "tasks" / task / "dbs"
            shutil.copytree(db, dest)
            if state == "recorded":
                require(tree_sha(dest) == refs["recorded_db_sha256"], "Changed final DB fixture")
        worker = Path(__file__).with_name("probe_appworld_grades.py").resolve()
        raw = subprocess.check_output([str(python), "-I", "-B", str(worker), task, str(PROFILE)], cwd=folder)
        grades = read_json(raw)
        require(grades["initial"]["success"] is False and grades["recorded"]["success"] is True
                and all(v["required"] == refs["requirements"] for v in grades.values()), "AppWorld grade control changed")
        results[task] = grades
    comparisons = grade_agreement((r[state]["success"], expected) for r in results.values()
                                  for state, expected in (("initial", False), ("recorded", True)))
    return dict(scope="native_db_regrade_controls", split="train", benchmark_revision=spec["revision"], grade_agreement=comparisons,
                order_per_task=["initial", "recorded", "initial"], same_initial_grade_after_reset=True, tasks=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bfcl-checkout", type=Path, required=True)
    parser.add_argument("--appworld-runtime", type=Path, required=True)
    parser.add_argument("--appworld-python", type=Path, required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--require-bfcl-native-routing", action="store_true",
                        help="Require attributed native returns with memory enabled and blocked access with memory disabled")
    args = parser.parse_args()
    profile_bytes = PROFILE.read_bytes()
    spec = read_json(profile_bytes)
    with TemporaryDirectory() as temp:
        temp = Path(temp)
        report = dict(format="agent-memory-benchmark-portability-audit/v1", profile_sha256=digest(profile_bytes),
                      new_provider_requests=0, tau2=check_tau2(spec["tau2"]),
                      bfcl=check_bfcl(args.bfcl_checkout.resolve(), temp / "bfcl", spec["bfcl"]),
                      appworld=check_appworld(args.appworld_runtime.resolve(), args.appworld_python.absolute(),
                                             temp / "appworld", spec["appworld"]))
        if args.require_bfcl_native_routing:
            from agent_memory_eval.runner.bfcl_memory_check import check
            report["format"] = "agent-memory-benchmark-portability-audit/v2"
            report["bfcl_native_control"] = check(args.bfcl_checkout, temp / "bfcl-native")
    publish(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Read actual BFCL relevance trials, including on-demand decisions with no memory exposure."""

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.analysis.trajectory import allocation, cue, decision_path, recorded_effort, trajectory_report
from agent_memory_eval.contracts.core import digest, read_json
from agent_memory_eval.runner.bfcl_relevance import audit_task, load_inputs


METRICS = {
    "function_called": "A business function call in the first scored relevance decision.",
    "undeclared_functions": "Number of calls with a name absent from the task's declared functions.",
    "missing_arguments": "Number of required function arguments absent from decoded business calls.",
}
CUES = {
    "function_name": "Names actually called in the public source decision, compared with function names offered in its initial input.",
    "function_return": "Business call IDs with a linked public tool return. The relevance benchmark grades selection without executing the call.",
}


def _public_cues(log, decision):
    req = next((r for r in log.requests if r["actor_role"] == "worker"), None)
    public = [log.blobs.get(e["payload"]["public_message_ref"]) for e in log.of_type("business.transition")
              if e["payload"].get("origin") == "bfcl.public_observation"]
    wire = log.blobs.get(req["request_ref"]) if req else None
    calls = [c for m in public if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    names = [c["function"]["name"] for c in calls]
    if decision is not None:
        require(names == [c["name"] for c in decision["calls"]], "Source choice differs from the public experience")
    offered = [t["function"]["name"] for t in wire["tools"]] if wire else None
    ids = {c["id"] for c in calls}
    returned = [m["tool_call_id"] for m in public if m["role"] == "tool" and m.get("tool_call_id") in ids]
    current = [m["tool_call_id"] for m in wire["messages"] if m["role"] == "tool"] if wire else None
    return dict(function_name=cue(names if decision is not None else None, offered),
                function_return=cue(returned if public else None, current))


def bfcl_report(profile, recording, checkout, sources_only, *, collector=None):
    spec = read_json(profile.read_bytes())
    cases, is_empty = load_inputs(checkout, spec)
    raw = (recording / "registration.json").read_bytes()
    reg = read_json(raw)
    require(reg["profile_sha256"] == digest(profile.read_bytes()) and reg["tasks"] == [r["id"] for _, r in cases],
            "BFCL protocol or registered cohort changed")
    rows = []
    portable = {label: (name, arm) for name, pair in spec.get("policy_writer_pairs", {}).items()
                for arm, label in (("existing", pair["reference"]), ("candidate", pair["candidate"]))}
    for case in cases:
        category, task = case
        tid = task["id"]
        audited = audit_task(recording, spec, case, is_empty, sources_only=sources_only)
        writers = {a["run"]: a for a in audited["allocations"] if a["phase"] == "write"}
        for trial in audited["allocations"]:
            if trial["phase"] == "write":
                if collector is not None:
                    checked = (RecordedDecisions(recording / tid, trial["run"], audited["journal_sha256"])
                               if trial["status"] != "not_started" else None)
                    form, condition = portable.get(trial["form"], (trial["form"], trial["form"]))
                    collector.writer(tid, form, condition, trial["status"], checked,
                                     checksum=audited["journal_sha256"], reused=trial.get("reused_requests", 0))
                continue
            source = trial["phase"] == "source"
            form, condition = portable.get(trial["form"], (trial["form"], trial["mode"] or "no_memory"))
            cluster = spec.get("policy_question_families", {}).get(tid, tid)
            obs = allocation(tid, cluster, trial["phase"], condition, form=form,
                             delivery=trial["mode"], draw=None if source else 1, repeat=None if source else 1)
            obs["run"] = trial["run"]
            obs["source_run"] = None if source else tid + "-source"
            rows.append(obs)
            if trial["status"] == "not_started":
                obs["reason"] = "not_started"
                continue
            log = RecordedDecisions(recording / tid, trial["run"], audited["journal_sha256"])
            grade, = [e["payload"] for e in log.of_type("outcome.scored")]
            obs.update(status="graded" if trial["success"] is not None else "ungraded", reason=trial["status"],
                       official_success=trial["success"], snapshot=trial["snapshot"],
                       effort=recorded_effort(log, reused_local_generations=trial["reused_requests"]), **decision_path(log))
            obs["evidence"].update(journal_sha256=audited["journal_sha256"], category=category,
                                   grade_sha256=(grade["official_artifact_ref"] or {}).get("sha256"))
            if source:
                obs["source_cues"] = _public_cues(log, obs["decision"])
                if collector is not None:
                    messages = [log.blobs.get(e["payload"]["public_message_ref"]) for e in log.of_type("business.transition")
                                if e["payload"].get("origin") == "bfcl.public_observation"]
                    collector.source(tid, [log.blobs.get(log.start["task_ref"]), *messages])
            else:
                obs["writer_run"] = log.start["source_run_ids"][1]
                obs["evidence"]["public_experience_sha256"] = writers[obs["writer_run"]]["experience_sha256"]
            if collector is not None:
                collector.decision(obs, log)
            if trial["success"] is not None:
                require(obs["decision"] is not None and bool(obs["decision"]["calls"]) == trial["any_call"],
                        "BFCL grade and first public function choice disagree")
                obs["progress"] = dict(function_called=trial["any_call"], undeclared_functions=len(trial["undeclared"]),
                                       missing_arguments=len(trial["missing_required"]))
    bindings = dict(protocol_sha256=reg["profile_sha256"], benchmark_revision=spec["revision"],
                    source=dict(recording=recording.name, registration_sha256=digest(raw)))
    if collector is not None:
        # audit_task checks the zero-cost receipt of every dispatched local request.
        collector.cost = dict(confirmed_usd="0", held_usd="0", covered_usd="0")
        collector.cluster_unit = "question_family" if spec.get("policy_question_families") else "public_question"
    return trajectory_report("bfcl.relevance", bindings, rows, metrics=METRICS, cues=CUES, sources_only=sources_only,
                             requirement=spec.get("source_opportunity_requirement"))

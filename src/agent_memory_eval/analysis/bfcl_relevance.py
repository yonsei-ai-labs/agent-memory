"""Compare recorded BFCL decisions by public question, and probe what their native score can detect."""

import argparse
from copy import deepcopy
from pathlib import Path

from agent_memory_eval.analysis.paired_trials import OutcomeMetric, Trial, compare_trials
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require, sealed_bytes
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore
from agent_memory_eval.runner.bfcl_relevance import business_tools, grade_reply, load_inputs


PRESENCE = OutcomeMetric(
    "bfcl.relevance.call_presence", 1, "Decoded business-call presence matches the registered BFCL category.",
    "first_business_decision", "question", "graded_decisions", "public_question", "all_pairs_graded",
)


def trials(study, phase, form=None, mode=None):
    entries = []
    require(study["format"] == "bfcl-local-two-pass/v1" and phase in {"source", "solve"}, "Unknown decision archive")
    ids = study["registration"]["tasks"]
    require(len(set(ids)) == len(ids) and set(ids) == set(study["tasks"]), "Wrong archived tasks")
    cohort = digest(canonical(study["registration"]))
    for task in ids:
        records = study["tasks"][task]["allocations"]
        matches = [(i, r) for i, r in enumerate(records) if (r["phase"], r["form"], r["mode"]) == (phase, form, mode)]
        require(len(matches) == 1 and matches[0][1]["task"] == task, "Missing or duplicate decision allocation")
        index, row = matches[0]
        started = row["status"] != "not_started"
        effort = {}
        if started:
            effort = {"tokens": row["prompt_tokens"] + row["completion_tokens"], "requests": row["requests"]}
        entries.append(Trial(task, "decision", task + "-source", started, row["success"], effort,
                             metric=PRESENCE, cohort=cohort, evidence_pointer=f"/tasks/{task}/allocations/{index}"))
    return entries


def corruptions(original, tools):
    """Alter copies of an observed, well-formed call to test the score independently of the model."""
    first = original["tool_calls"][0]["function"]
    schema = next(t["function"]["parameters"] for t in tools if t["function"]["name"] == first["name"])
    args = read_json(first["arguments"].encode())
    changes = {}
    if schema.get("required"):
        bad_args = deepcopy(original)
        key = schema["required"][0]
        require(key in args, "Expected a present required argument")
        del args[key]
        bad_args["tool_calls"][0]["function"]["arguments"] = canonical(args).decode()
        changes["remove_required_argument"] = bad_args
    unknown = deepcopy(original)
    name = "unregistered_control_function"
    require(all(t["function"]["name"] != name for t in tools), "Control name is a declared function")
    unknown["tool_calls"][0]["function"]["name"] = name
    changes["replace_function_name"] = unknown
    changes["remove_calls"] = {"role": "assistant", "content": ""}
    return changes


def probe_sources(study, root, cases, is_empty):
    probes = []
    for category, public in cases:
        task = public["id"]
        data = study["tasks"][task]
        source, = [r for r in data["allocations"] if r["phase"] == "source"]
        if not source["success"] or source["undeclared"] or source["missing_required"]:
            continue
        record = RecordedDecisions(root / task, source["run"], data["journal_sha256"])
        req, = [e["payload"] for e in record.of_type("model.request_started")]
        response = record.reply(req)["message"]
        tools = business_tools(public["function"])
        obs = grade_reply(response, category, is_empty, tools)
        require(obs == record.blobs.get(record.of_type("outcome.scored")[0]["payload"]["official_artifact_ref"]),
                "Native grade disagrees with the unedited source")
        if category == "irrelevance":
            require(not obs["any_call"], "Expected an abstention")
            changes = {"insert_spurious_call": {"role": "assistant", "tool_calls": [
                {"type": "function", "function": {"name": tools[0]["function"]["name"], "arguments": "{}"}}]}}
        else:
            changes = corruptions(response, tools)
        for label, altered in changes.items():
            score = grade_reply(altered, category, is_empty, tools)
            probes.append(dict(task=task, change=label, original_success=obs["valid"], edited_success=score["valid"],
                               undeclared=score["undeclared_functions"], missing_required=score["missing_required"]))
    return probes


def verified_reuse(study, root):
    """Reconcile decoded receipts with the original submitted wires and exact token streams."""
    name = study["registration"].get("original_recording")
    if not name:
        return []
    require(Path(name).name == name, "Invalid original recording directory")
    old_root = root.parent / name
    old_reg = (old_root / "registration.json").read_bytes()
    require(digest(old_reg) == study["registration"]["original_registration_sha256"], "Changed original registration")
    result = []
    seen = set()
    for task, info in study["tasks"].items():
        old_folder = old_root / task
        old_events = [e for line in (old_folder / "log.jsonl").read_bytes().splitlines() for e in read_json(line)["events"]]
        old = {(e["type"], e["payload"]["request_id"]): e for e in old_events if e["type"] in
               {"model.request_started", "model.request_finished"}}
        blobs = BlobStore(old_folder)
        for row in info["allocations"]:
            if not row.get("reused_requests"):
                continue
            record = RecordedDecisions(root / task, row["run"], info["journal_sha256"])
            for event in record.of_type("model.request_started"):
                req = event["payload"]
                reply = record.blobs.get(record.finishes[req["request_id"]]["response_ref"])
                origin = reply.get("reused_local_generation")
                if not origin:
                    continue
                prior = origin["request_id"]
                start = old["model.request_started", prior]
                finish = old["model.request_finished", prior]
                ref = finish["payload"]["response_ref"]
                require((task, prior) not in seen and origin["recording"] == name and origin["task"] == task
                        and finish["run_id"] == start["run_id"] == origin["run"]
                        and req["request_ref"]["sha256"] == start["payload"]["request_ref"]["sha256"] == origin["request_sha256"]
                        and ref["sha256"] == origin["response_sha256"], "Duplicate or misbound original generation")
                before = dict(blobs.get(ref)["local_generation"])
                after = dict(reply["local_generation"])
                before.pop("decode_error", None)
                after.pop("decode_error", None)
                require(before == after, "Changed original prompt, output, tokens or usage")
                seen.add((task, prior))
                result.append(dict(task=task, run=row["run"], original_response=ref["sha256"], prompt=after["input_sha256"]))
    expected = sum(r.get("reused_requests", 0) for data in study["tasks"].values() for r in data["allocations"])
    require(len(result) == expected, "Some reported generation reuse lacks a verified receipt")
    return result


def trace_questions(study, root):
    """Keep the written texts and the observed business choices addressable outside the raw-run directory."""
    questions = {}
    for task, data in study["tasks"].items():
        folder = root / task
        blobs = BlobStore(folder)
        records = [read_json(line) for line in sealed_bytes(folder / "log.jsonl", data["journal_sha256"]).splitlines()]
        state = blobs.get(records[-1]["state_ref"])
        events = [e for record in records for e in record["events"]]
        scores = {e["run_id"]: e["payload"] for e in events if e["type"] == "outcome.scored"}
        result = {"notes": {}, "actions": {}}
        questions[task] = result
        for r in data["allocations"]:
            if r["phase"] == "write":
                if r.get("items"):
                    item, = snapshot_items(blobs, state, r["snapshot"])
                    witness = state["memory_writes"][r["run"]]["experience_ref"]
                    require(witness["sha256"] == r["experience_sha256"], "Note and public source disagree")
                    if "public_source" not in result:
                        source = blobs.get(witness)
                        result["public_source"] = dict(experience_sha256=witness["sha256"],
                            question=blobs.get(source["task_ref"])["content"],
                            observations=[dict(role=o["message"]["role"], sha256=o["source_ref"]["sha256"])
                                          for o in source["observations"]])
                    require(result["public_source"]["experience_sha256"] == witness["sha256"], "Changed common source")
                    result["notes"][r["form"]] = dict(title=item["title"], body=item["body"], item=item["id"],
                        version=item["version"], snapshot=r["snapshot"], public_experience_sha256=r["experience_sha256"])
                continue
            if r["success"] is None:
                continue
            score = scores[r["run"]]["official_artifact_ref"]
            requests = [e["payload"] for e in events if e["run_id"] == r["run"] and e["type"] == "model.request_started"]
            last = requests[-1]
            finish, = [e["payload"] for e in events if e["type"] == "model.request_finished"
                       and e["payload"]["request_id"] == last["request_id"]]
            label = r["form"] + "/" + r["mode"] if r["form"] else "source"
            text = blobs.get(finish["response_ref"])["choices"][0]["message"].get("content")
            result["actions"][label] = dict(calls=blobs.get(score)["decoded"], success=r["success"], snapshot=r["snapshot"],
                assistant_text=text, exposed_fields=r["fields"], grade_sha256=score["sha256"],
                decision_request_sha256=last["request_ref"]["sha256"],
                input_manifest_sha256=last["input_manifest_ref"]["sha256"], raw_response_sha256=finish["response_ref"]["sha256"])
    return questions


def report(study, cfg, root, cases, is_empty):
    ref = trials(study, "source")
    comparisons = {}
    for form in cfg["forms"]:
        comparisons[form] = {}
        for mode in cfg["delivery"]:
            candidate = trials(study, "solve", form, mode)
            comparisons[form][mode] = compare_trials(ref, candidate, metrics=("tokens", "requests"), test_signs=False)
    groups = {}
    for category, tasks in cfg["tasks"].items():
        groups[category] = {}
        records = [r for task in tasks for r in study["tasks"][task]["allocations"]]
        for group in ["source", "write", *cfg["delivery"]]:
            rows = [r for r in records if (r["mode"] or r["phase"]) == group]
            groups[category][group] = dict(assigned=len(rows), started=sum(r["status"] != "not_started" for r in rows),
                graded=sum(r["success"] is not None for r in rows), successes=sum(r["success"] is True for r in rows),
                stored=sum(r.get("items", 0) for r in rows), body_included=sum("body" in r.get("fields", []) for r in rows),
                title_included=sum("title" in r.get("fields", []) for r in rows),
                lists=sum(r.get("reads", {}).get("memory_list", 0) for r in rows),
                gets=sum(r.get("reads", {}).get("memory_get", 0) for r in rows),
                requests=sum(r.get("requests", 0) for r in rows),
                prompt_tokens=sum(r.get("prompt_tokens", 0) for r in rows),
                completion_tokens=sum(r.get("completion_tokens", 0) for r in rows))
    return dict(format="bfcl-relevance-policy-check/v1", registration=study["registration"], questions=trace_questions(study, root),
                reused_generations=verified_reuse(study, root), by_category=groups, paired_by_question=comparisons,
                source_score_probes=probe_sources(study, root, cases, is_empty))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("archive", "profile", "recording", "checkout"):
        parser.add_argument("--" + arg, type=Path, required=True)
    args = parser.parse_args()
    study, cfg = (read_json(p.read_bytes()) for p in (args.archive, args.profile))
    require(digest(args.profile.read_bytes()) == study["registration"]["profile_sha256"], "Changed BFCL registration")
    cases, is_empty = load_inputs(args.checkout, cfg)
    print(canonical(report(study, cfg, args.recording, cases, is_empty)).decode())

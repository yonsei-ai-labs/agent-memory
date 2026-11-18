"""Check the two evaluation tasks' scoring sensitivity on sealed airline episodes."""
import argparse
from collections import Counter
from pathlib import Path
import re
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, safe_path


def review(run, tasks):
    from tau2.data_model.message import AssistantMessage
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.data_model.simulation import SimulationRun
    from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

    done = read_json((run / "completion.json").read_bytes())
    def asset(name, key):
        return read_json(asset_text(run, dict(path=name, sha256=done[key])).encode())
    control = asset("control.json", "control_sha256")
    rows = asset("allocations.json", "allocation_sha256")
    design = control["design"]
    if design["source"]["benchmark_revision"] != PIN or set(design["task_sets"]["evaluation"]) != {"3", "5"}:
        raise ContractError("Expected the pinned airline fact-recheck evaluation tasks")
    scoring = CommunicateScoring(scorer="substring")
    criteria = tasks["3"]["evaluation_criteria"]["communicate_info"]
    def communication(messages):
        return CommunicateEvaluator.evaluate_communicate_info(messages, criteria, scoring=scoring)
    results = []
    counts = Counter()
    for row in rows:
        task = row["task_id"]
        if row["phase"] != "solve" or task not in {"3", "5"}:
            continue
        r = row.get("result", {})
        item = dict(job=row["index"], task=task, repetition=row["repetition"], arm=row["arm"],
                    official_success=r.get("official_success"))
        results.append(item)
        if r.get("grading_status") != "graded":
            continue
        folder = safe_path(run, row["directory"])
        report = BlobStore(folder).get(r["report_ref"])
        episode = SimulationRun.model_validate(report)
        if (episode.task_id != task or episode.reward_info.reward != r["official_score"]
                or (episode.reward_info.reward == 1.0) is not r["official_success"]):
            raise ContractError("Task or score differs from the sealed native report")
        item.update(native_report_sha256=r["report_ref"]["sha256"], db_match=episode.reward_info.db_check.db_match)
        counts[f"task_{task}_officially_graded"] += 1
        if task == "5":
            item["profile_lookup_turns"] = [i for i, m in enumerate(report["messages"])
                if any(c["name"] == "get_user_details" for c in m.get("tool_calls") or [])]
            counts["task_5_with_profile_lookup"] += bool(item["profile_lookup_turns"])
            continue
        original = communication(episode.messages)
        if original != episode.reward_info.communicate_checks:
            raise ContractError("Archived required-information component did not reproduce")
        messages = list(episode.messages)
        changed_turns = []
        for i, m in enumerate(messages):
            if not isinstance(m, AssistantMessage) or not m.has_text_content():
                continue
            normalized, hits = re.subn(r"\bfour\b", "4", m.content, flags=re.IGNORECASE)
            if hits:
                messages[i] = m.model_copy(update={"content": normalized})
                changed_turns.append(dict(turn=i, original=m.content))
        normalized_met = communication(messages)[0].met
        item.update(original_met=original[0].met, normalized_met=normalized_met, changed_turns=changed_turns)
        counts["task_3_original_communication_met"] += original[0].met
        counts["task_3_normalized_communication_met"] += normalized_met
    return dict(name=run.name, allocation_sha256=done["allocation_sha256"], counts=dict(counts), cells=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--workspace", type=Path, default=ROOT)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in args.runs):
        raise ContractError("Choose a new analysis output outside the sealed evidence")
    checkout = verify_checkout(args.workspace / "data/tau2-bench")
    native_modules(checkout)
    raw = (checkout / "data/tau2/domains/airline/tasks.json").read_bytes()
    tasks = {t["id"]: t for t in read_json(raw)}
    if (tasks["3"]["evaluation_criteria"]["communicate_info"] != ["4"]
            or tasks["5"]["evaluation_criteria"]["communicate_info"] != []
            or any(tasks[t]["evaluation_criteria"]["reward_basis"] != ["DB", "COMMUNICATE"] for t in ("3", "5"))):
        raise ContractError("The expected native scoring criteria have changed")
    with patch("tau2.evaluator.evaluator_communicate.generate", side_effect=RuntimeError("Unexpected model call")) as guard:
        reviews = [review(run.resolve(), tasks) for run in args.runs]
        if guard.call_count:
            raise ContractError("A scoring sensitivity check attempted a model call")
    write_json(output, dict(format="agent-memory-fact-recheck-grading-sensitivity/v1", benchmark_revision=PIN,
        benchmark_task_file_sha256=digest(raw), method_sha256=digest(Path(__file__).read_bytes()),
        purpose="Post hoc check on an in-memory copy of customer-visible assistant text. Official scores are preserved.",
        spelling_change="Case-insensitive whole word four to digit 4 on task 3. No other changes.",
        task_5_criteria_sha256=digest(canonical(tasks["5"]["evaluation_criteria"])),
        model_calls=guard.call_count, runs=reviews))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Audit pinned MemoryArena code without a model, server, GPU, or network.

Original function bodies are executed after isolating dependency imports. Only
the first travel row is inspected. Reference answers are used to probe grading,
never exported as training examples. Fake responses test prompt construction,
not worker accuracy. This is not an end-to-end benchmark run.
"""

import argparse
import ast
from collections import Counter
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import types


HERE = Path(__file__).resolve().parent
PLAN = json.loads((HERE / "intake-plan.json").read_text())
INVENTORY = json.loads((HERE / "data-inventory.json").read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_isolated(root, relative_path, *, remove=(), bindings=None):
    """Keep original code, replacing only the explicitly listed import edges."""
    path = root / relative_path
    tree = ast.parse(path.read_text(), filename=relative_path)
    removed = []
    body = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module in remove:
            removed.append(node.module)
        else:
            body.append(node)
    if set(removed) != set(remove):
        raise ValueError(f"Unexpected import structure in {relative_path}: {removed}")
    tree.body = body
    name = "_memoryarena_probe_" + relative_path.replace("/", "_").replace(".", "_")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__dict__.update(bindings or {})
    sys.modules[name] = module  # Required by dataclasses.
    exec(compile(tree, relative_path, "exec"), module.__dict__)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.upstream.resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != PLAN["code_revision"]:
        raise ValueError("Wrong upstream revision; do not reuse this audit on changed code")
    subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD"], check=True)

    structures = []
    travel_rows = None
    for entry in INVENTORY["files"]:
        path = args.data_root / entry["path"]
        if digest(path) != entry["sha256"]:
            raise ValueError(f"Data hash mismatch: {entry['path']}")
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        structural = {
            "path": entry["path"], "rows": len(rows),
            "subtasks": sum(len(row["questions"]) for row in rows),
            "unique_ids": len({row["id"] for row in rows}),
            "question_answer_length_mismatches": sum(
                len(row["questions"]) != len(row["answers"]) for row in rows
            ),
        }
        for key, value in structural.items():
            if value != entry[key]:
                raise ValueError(f"Inventory mismatch: {entry['path']}:{key}")
        structures.append(structural)
        if entry["path"].startswith("group_travel_planner/"):
            travel_rows = rows
    if travel_rows is None:
        raise ValueError("Travel data missing")
    background_counts = Counter(
        json.dumps(row["base_person"], sort_keys=True) for row in travel_rows
    )
    plan_counts = Counter(
        json.dumps(row["base_person"]["daily_plans"], sort_keys=True) for row in travel_rows
    )

    prefix = "env/env_systems/travel_planner_env/"
    loader = load_isolated(root, prefix + "data_loader.py", remove=("datasets",))
    row = loader._convert_row(travel_rows[0])
    scorer = load_isolated(
        root, prefix + "eval.py", remove=("data_loader",),
        bindings={"load_travel_data": lambda: [row]},
    )
    env_base = load_isolated(root, "env/env_systems/base_env.py")
    env_mod = load_isolated(
        root, "env/env_systems/travel_env.py",
        remove=("base_env", "travel_planner_env.data_loader"),
        bindings={"BaseEnvironment": env_base.BaseEnvironment, "load_travel_data": lambda: [row]},
    )
    with contextlib.redirect_stdout(io.StringIO()):
        env = env_mod.TravelPlannerEnvironment({"judgement_mode": "none"})
        observation = env.reset(row["id"])

    reference = {
        "id": row["id"],
        "persons": [{"person_idx": a["round_idx"], "plan": copy.deepcopy(a["daily_plans"])}
                    for a in row["answers"]],
    }
    prefix_only = copy.deepcopy(reference)
    wrong_city = copy.deepcopy(reference)
    for person in prefix_only["persons"]:
        for day in person["plan"]:
            for slot in scorer.SLOTS:
                day[slot] = str(day.get(slot, "-")).strip()[:1]
    for person in wrong_city["persons"]:
        for day in person["plan"]:
            day["current_city"] = "ZZZZZZZ_NONEXISTENT_CITY"
    missing_persons = copy.deepcopy(reference)
    missing_persons["persons"] = missing_persons["persons"][:1]
    synthetic_second_group = copy.deepcopy(row)
    synthetic_second_group["id"] = "synthetic_omitted_group"

    def evaluate_case(submission, expected_rows):
        scorer.load_travel_data = lambda: expected_rows
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "submission.jsonl"
            path.write_text(json.dumps(submission) + "\n")
            with contextlib.redirect_stdout(stdout):
                scores = scorer.evaluate(str(path))
        printed = stdout.getvalue()
        groups = re.search(r"Groups:\s+(\d+)", printed)
        persons = re.search(r"PS:\s+(\d+)\s*/\s*(\d+)", printed)
        return {
            "scores_percent": scores,
            "expected_groups": len(expected_rows),
            "counted_groups": int(groups.group(1)),
            "expected_persons": sum(len(x["answers"]) for x in expected_rows),
            "counted_persons": int(persons.group(2)),
            "passed_persons": int(persons.group(1)),
        }

    scoring = {
        "reference_sanity": evaluate_case(reference, [row]),
        "empty_submission_sanity": evaluate_case({"id": row["id"], "persons": []}, [row]),
        "one_character_per_scored_slot": evaluate_case(prefix_only, [row]),
        "wrong_current_city_only": evaluate_case(wrong_city, [row]),
        "only_first_person_submitted": evaluate_case(missing_persons, [row]),
        "one_of_two_expected_groups_submitted": evaluate_case(reference, [row, synthetic_second_group]),
    }
    scoring["wrong_current_city_only"]["environment_passed_persons"] = sum(
        env._evaluate_slots(person["plan"], answer["daily_plans"])
        for person, answer in zip(wrong_city["persons"], row["answers"])
    )

    # Prompt checks use synthetic canaries, not benchmark answers or tool data.
    prompts = load_isolated(root, prefix + "prompts.py")
    agent_base = load_isolated(root, "agent/base.py")

    class FakeClient:
        def __init__(self):
            self.captured = []

        def chat_with_tools(self, messages, tools):
            self.captured.append(copy.deepcopy(messages))
            return types.SimpleNamespace(content="FAKE_PLAN", tool_calls=[], raw_response={})

    class NoToolExecutor:
        def __init__(self, **kwargs):
            pass

        def execute(self, *args, **kwargs):
            raise AssertionError("Tool execution is prohibited in this prompt probe")

    bindings = {name: getattr(prompts, name) for name in (
        "AGENT_SYSTEM_PROMPT", "AGENT_USER_PROMPT_TEMPLATE", "HISTORY_TEMPLATE", "BASE_PERSON_TEMPLATE"
    )}
    bindings.update(BaseAgent=agent_base.BaseAgent, BaseModelClient=object,
                    ToolCall=object, ToolExecutor=NoToolExecutor, TOOLS=[])
    agent_mod = load_isolated(root, "agent/travel_planner.py", remove=(
        "base_agent", "env.env_systems.travel_planner_env.clients.base_client",
        "env.env_systems.travel_planner_env.tool_executor",
        "env.env_systems.travel_planner_env.tool_schemas",
        "env.env_systems.travel_planner_env.prompts",
    ), bindings=bindings)
    agent_mod.TravelPlannerAgent._create_client = lambda self, name: FakeClient()
    runner_tree = ast.parse((root / "run_travel.py").read_text())
    include_args = [kw.value for node in ast.walk(runner_tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "prepare_for_person"
                    for kw in node.keywords if kw.arg == "include_previous_plans"]
    expected_arg = ast.parse("memory_system is None", mode="eval").body
    if len(include_args) != 1 or ast.dump(include_args[0]) != ast.dump(expected_arg):
        raise ValueError("Runner history wiring changed")
    prompt_checks = {}
    for label, include_history, memory_context in (
        ("runner_none_history_flag", True, None),
        ("history_disabled", False, None),
        ("history_disabled_selected_memory", False, "SELECTED_MEMORY_CANARY"),
    ):
        agent = agent_mod.TravelPlannerAgent(model_name="offline-fake", max_steps=1)
        agent.set_base_person("Base", "BASE_QUERY_CANARY", "BASE_PLAN_CANARY")
        agent.accumulated_plans += "\nPREVIOUS_PERSON_PLAN_CANARY"
        with contextlib.redirect_stdout(io.StringIO()):
            agent.run_single_person("CURRENT_QUERY_CANARY", "Current", 2,
                                    include_previous_plans=include_history, memory_context=memory_context)
        content = "\n".join(message["content"] for message in agent.client.captured[0])
        prompt_checks[label] = {
            "previous_person_plan_present": "PREVIOUS_PERSON_PLAN_CANARY" in content,
            "base_plan_present": "BASE_PLAN_CANARY" in content,
            "selected_memory_present": "SELECTED_MEMORY_CANARY" in content,
            "fake_client_calls": len(agent.client.captured),
        }

    # These assert reproduced observations, NOT acceptable benchmark behavior.
    assert scoring["reference_sanity"]["scores_percent"]["sr"] == 100
    assert scoring["empty_submission_sanity"]["scores_percent"]["sr"] == 0
    assert scoring["one_character_per_scored_slot"]["scores_percent"]["sr"] == 100
    assert scoring["wrong_current_city_only"]["scores_percent"]["sr"] == 100
    assert scoring["wrong_current_city_only"]["environment_passed_persons"] == 0
    assert scoring["only_first_person_submitted"]["counted_persons"] == 1
    assert scoring["only_first_person_submitted"]["scores_percent"]["sr"] == 100
    assert scoring["one_of_two_expected_groups_submitted"]["counted_groups"] == 1
    assert prompt_checks["runner_none_history_flag"]["previous_person_plan_present"]
    assert not prompt_checks["history_disabled"]["previous_person_plan_present"]
    assert prompt_checks["history_disabled_selected_memory"]["selected_memory_present"]

    source_paths = ["run_travel.py", "agent/base.py", "agent/travel_planner.py",
                    "env/env_systems/base_env.py", "env/env_systems/travel_env.py"]
    source_paths += [prefix + name for name in ("data_loader.py", "eval.py", "prompts.py", "tool_executor.py")]
    result = {
        "code_revision": revision, "dataset_revision": INVENTORY["revision"],
        "probe_script_sha256": digest(Path(__file__)),
        "source_sha256": {p: digest(root / p) for p in source_paths},
        "data_structure_checks": structures,
        "travel_backgrounds": {
            "exact_unique_base_person_objects": len(background_counts),
            "multiplicity_histogram": dict(sorted(Counter(background_counts.values()).items())),
            "exact_unique_daily_plans": len(plan_counts),
            "daily_plan_multiplicity_histogram": dict(sorted(Counter(plan_counts.values()).items())),
            "note": "Ignore names and query wording when grouping identical base plans; this still does not prove template independence",
        },
        "exposed_travel_group_id": row["id"],
        "grading_probes": scoring, "prompt_probes": prompt_checks,
        "reset_observation_keys": sorted(observation),
        "flight_csv_present_in_clone": (root / prefix / "database/flights/clean_Flights_2022.csv").is_file(),
        "scope": {
            "real_model_calls": 0, "embedding_calls": 0, "llm_judge_calls": 0,
            "gpu_allocations": 0, "worker_utility_labels": 0,
            "end_to_end_run": False, "fake_model_calls": 3,
            "warning": "High scores here demonstrate grading weaknesses, not memory performance",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "offline observations reproduced", "output": str(args.output),
                      "grading_probes": len(scoring), "fake_prompt_probes": len(prompt_checks),
                      "worker_utility_labels": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()

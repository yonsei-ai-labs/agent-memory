"""Map sealed banking trajectories and analyst-only native targets into common measurements."""

from collections import Counter, defaultdict

from agent_memory_eval.analysis.efficiency import METRICS, business_calls_by_role
from agent_memory_eval.analysis.paired_trials import OutcomeMetric, Trial, compare_trials, component_coverage, target_effects
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.analysis.response_format import reply_format_counts
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.journal import safe_path


BANKING_SUCCESS = OutcomeMetric(
    name="banking.official_success", version=1, definition="The native banking grader awards official success.",
    stage="episode_end", unit="consultation", denominator="officially_graded_consultations", cluster_unit="source_experience",
    cluster_eligibility="all_pairs_graded",
)


def official_trial(result, policy, index, row, effort=None):
    """Bind the verified grade field and its source allocation before aggregating it."""
    status, success = row["status"], row["success"]
    unstarted = {"planned", "not_started"}
    require(status in unstarted | {"started", "interrupted", "graded", "error", "incomplete", "not_available"},
            f"Unknown banking trial status: {status}")
    require((status == "graded") == (success is not None), "Banking grade and grading status disagree")
    source = result["sources"][row["task"]]
    require(source["task"] == row["task"], "Banking source belongs to a different task")
    if status == "graded":
        require(row.get("run") and row.get("log_sha256"), "Banking grade lacks its recorded run")
    if effort is None:
        effort = row.get("effort") or {}
    key = policy.replace("~", "~0").replace("/", "~1")
    return Trial(row["task"], row["arm"], source.get("run"), status not in unstarted, success, effort,
                 metric=BANKING_SUCCESS, cohort=result["validation_registration"], evidence_pointer=f"/trials/{key}/{index}")


def banking_trials(study, condition, metric, mode=None):
    require(metric == BANKING_SUCCESS and mode is None, "Expected an official banking grade without a retail mode")
    require(isinstance(study.get("trials"), dict) and isinstance(study.get("sources"), dict)
            and study.get("validation_registration"), "Expected registered banking validation outcomes")
    rows = study["trials"].get(condition)
    require(isinstance(rows, list) and rows, f"No registered banking rows for {condition}")
    return [official_trial(study, condition, index, row) for index, row in enumerate(rows)]


def native_grep(corpus_sha256):
    """Open the benchmark's unmodified grep over the fingerprinted document corpus."""
    from tau2.domains.banking_knowledge.environment import get_knowledge_base
    from tau2.domains.banking_knowledge.retrieval import create_grep_retrieval_pipeline, resolve_variant

    corpus = get_knowledge_base()
    require(digest(canonical(corpus.model_dump(mode="json"))) == corpus_sha256, "Changed native corpus")
    cfg = resolve_variant("grep_only").grep
    pipeline = create_grep_retrieval_pipeline(corpus, top_k=cfg.top_k, case_sensitive=cfg.case_sensitive)
    return pipeline, len(corpus.documents), cfg.top_k


def grep_ranking(search, pattern, witnesses):
    hits = [doc for doc, _ in search.retrieve(pattern)]
    return dict(pattern=pattern, returned=len(hits), ranking_sha256=digest(canonical(hits)),
                witness_ranks={k: hits.index(v) + 1 for k, v in witnesses.items() if v in hits})


def known_customer_groups(domain, development, evaluated):
    customers = read_json((domain / "db.json").read_bytes())["users"]["data"]
    identities = {}
    for task in sorted(set(development) | set(evaluated)):
        case = read_json((domain / "tasks" / (task + ".json")).read_bytes())
        text = case["user_scenario"]["instructions"].casefold()
        found = {a["arguments"].get("user_id") for a in case["evaluation_criteria"]["actions"]
                 if a["name"] == "log_verification"} & customers.keys()
        found.update(key for key, person in customers.items()
                     if all(person[k] and person[k].casefold() in text for k in ("name", "phone_number")))
        identities[task] = found
    return {task: dict(identified=len(identities[task]) == 1,
                       overlapping_development=sorted(d for d in development if identities[d] & identities[task]),
                       unresolved_development=sorted(d for d in development if len(identities[d]) != 1))
            for task in sorted(evaluated)}


def _dispute_key(arguments):
    nested = arguments.get("arguments")
    if isinstance(nested, str):
        try:
            nested = read_json(nested.encode())
        except ValueError:
            nested = None
    return arguments.get("discoverable_tool_name"), canonical(nested)


NATIVE_DISPUTE_RECEIPTS = "banking-dispute-receipts/v2"


def dispute_submissions(messages, *, native_argument_errors=False):
    """Return customer submissions with the matching native receipt, keeping failed and unknown attempts."""
    receipts = {m["id"]: m for m in messages if m["role"] == "tool" and m.get("requestor") == "user"}
    submissions = []
    for message in messages:
        if message["role"] != "user":
            continue
        for call in message.get("tool_calls") or []:
            args = call["arguments"]
            if call["name"] != "call_discoverable_user_tool" or args.get("discoverable_tool_name") != "submit_cash_back_dispute_0589":
                continue
            receipt = receipts.get(call["id"])
            accepted = None
            if receipt and receipt.get("error"):
                accepted = False
            elif receipt and receipt.get("execution_status") == "executed":
                text = receipt["content"]
                if text.startswith("Cash back dispute submitted successfully."):
                    accepted = True
                elif native_argument_errors and text.startswith(("Error: Missing required parameter:", "Error: Unexpected parameter:")):
                    accepted = False
            submissions.append((call["id"], args, accepted))
    return submissions


def dispute_effects(task, messages, *, native_argument_errors=False):
    actions = [a for a in task["evaluation_criteria"]["actions"]
               if a["requestor"] == "user" and a["name"] == "call_discoverable_user_tool"
               and a["arguments"].get("discoverable_tool_name") == "submit_cash_back_dispute_0589"]
    if not actions:
        return None
    return target_effects([_dispute_key(a["arguments"]) for a in actions],
                          [(_dispute_key(args), accepted) for _, args, accepted in dispute_submissions(messages, native_argument_errors=native_argument_errors)])


def policy_table(result, root, domain):
    """The caller supplies registration-verified rows, including the scheduled but unstarted slots."""
    trials, role_totals, formats, effects, process = defaultdict(list), {}, {}, [], {}
    for policy, rows in result["trials"].items():
        role_counts, observed = Counter(), 0
        replies = Counter()
        checks = []
        allocations = {}
        for index, row in enumerate(rows):
            task, slot = row["task"], row["arm"]
            effort = dict(row.get("effort") or {})
            if row["success"] is not None:
                folder = root / f"task-{task}__{policy}" / "solve"
                if folder not in allocations:
                    entries = read_json((folder / "allocations.json").read_bytes())
                    allocations[folder] = {entry["run_id"]: entry for entry in entries}
                    require(len(allocations[folder]) == len(entries), "Duplicate banking run")
                entry = allocations[folder][row["run"]]
                require((entry["task_id"], entry["arm"], entry["result"]["log_sha256"]) ==
                        (task, slot, row["log_sha256"]), "Wrong native report allocation")
                log = RecordedDecisions(safe_path(folder, entry["directory"]), row["run"], row["log_sha256"])
                score, = log.of_type("outcome.scored")
                require(score["payload"]["official_success"] is row["success"], "Wrong native grade")
                report = log.blobs.get(score["payload"]["official_artifact_ref"])
                messages = report["messages"]
                counts = business_calls_by_role(messages)
                require(counts["assistant"] == effort["business_calls"], "Worker call count changed")
                effort.update(worker_calls=counts["assistant"], customer_calls=counts["user"])
                role_counts.update(counts)
                replies.update(reply_format_counts(log))
                observed += 1
                native = read_json((domain / "tasks" / (task + ".json")).read_bytes())
                actions = {a["action_id"] for a in native["evaluation_criteria"]["actions"]}
                native_checks = row["native_action_checks"]
                require({a["id"] for a in native_checks} == actions, "Incomplete native action checks")
                checks.append(dict(task=task, slot=slot, run=row["run"], report_sha256=score["payload"]["official_artifact_ref"]["sha256"],
                                   tagged_documents=component_coverage(native["required_documents"], row["tagged_documents_seen"]),
                                   official_actions=component_coverage(actions, [a["id"] for a in native_checks if a["matched"]])))
                effect = dispute_effects(native, messages)
                if effect is not None:
                    effects.append(dict(task=task, slot=slot, policy=policy, run=row["run"],
                                        report_sha256=score["payload"]["official_artifact_ref"]["sha256"], **effect))
            trials[policy].append(official_trial(result, policy, index, row, effort))
        role_totals[policy] = dict(observed=observed, worker=role_counts["assistant"] if observed else None,
                                   customer=role_counts["user"] if observed else None)
        formats[policy] = (dict(text_replies=replies["text_replies"], json_objects=replies["json_objects"],
                                message_only_objects=replies["message_only_objects"],
                                verbatim_in_next_input=replies["verbatim_in_next_input"]) if observed else None)
        process[policy] = checks
    table = compare_trials(trials["existing"], trials["candidate"], metrics=(*METRICS, "worker_calls", "customer_calls"))
    return dict(comparison=table, role_calls=role_totals, reply_format=formats, process_checks=process, dispute_receipts=effects,
                source_status={t: dict(run=s.get("run"), status=s.get("status", "unobserved"), success=s["success"])
                               for t, s in sorted(result["sources"].items())}, costs_by_phase=result["costs"])


def dispute_grader_controls(workspace, task_ids):
    """Exercise the native DB grader and the receipt scorer on the same known actions."""
    from copy import deepcopy
    import json

    from agent_memory_eval.adapters.banking_dispute_evidence import SUBMIT, dispute_targets
    from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout

    native_modules(verify_checkout(workspace / "data/tau2-bench"))
    from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage
    from tau2.data_model.tasks import Task
    from tau2.domains.banking_knowledge.environment import get_environment
    from tau2.evaluator.evaluator_env import EnvironmentEvaluator

    directory = workspace / "data/tau2-bench/data/tau2/domains/banking_knowledge/tasks"
    results = {}
    for tid in task_ids:
        raw_bytes = (directory / (tid + ".json")).read_bytes()
        raw = read_json(raw_bytes)
        customer, targets = dispute_targets(raw)
        task = Task.model_validate(raw)
        kwargs = dict(retrieval_variant="grep_only", task=task)
        env = get_environment(**kwargs)
        visible = env.use_tool("get_credit_card_transactions_by_user", user_id=customer)
        possible = sorted(k for k, v in env.tools.db.credit_card_transaction_history.data.items() if v["user_id"] == customer)
        require(all(i in visible for i in targets), "A native target is missing from the customer lookup")
        other = next(i for i in possible if i not in targets and i in visible)
        prefix = []
        operations = {}
        for action in task.evaluation_criteria.actions:
            if action.name != "call_discoverable_user_tool":
                prefix.append((action.name, action.requestor, action.arguments))
                continue
            require(action.requestor == "user" and action.arguments["discoverable_tool_name"] == SUBMIT, "Unexpected operation")
            native_args = json.loads(action.arguments["arguments"])
            operations[native_args["transaction_id"]] = action.arguments
        require(set(operations) == targets and [name for name, _, _ in prefix] ==
                ["log_verification", "give_discoverable_user_tool"], "Unexpected multi-dispute task")
        ids = list(operations)
        operations[other] = dict(discoverable_tool_name=SUBMIT, arguments=json.dumps(dict(user_id=customer, transaction_id=other)))
        variants = dict(reference=ids, no_disputes=[], omit_one=ids[:-1], replace_one=ids[:-1]+[other],
                        additional_other=ids+[other], duplicate_target=ids+[ids[0]])
        controls = {}
        for name, submits in variants.items():
            env = get_environment(**kwargs)
            env.set_state(initialization_data=None, initialization_actions=None, message_history=[])
            original = deepcopy(env.tools.db.cash_back_disputes.data)
            messages = []
            calls = prefix + [("call_discoverable_user_tool", "user", operations[i]) for i in submits]
            for n, (tool, actor, args) in enumerate(calls):
                call = ToolCall(id=f"public-control-{n}", name=tool, requestor=actor, arguments=deepcopy(args))
                response = env.get_response(call)
                require(not response.error, "Native control tool failed")
                cls = AssistantMessage if actor == "assistant" else UserMessage
                messages.extend([cls(role=actor, tool_calls=[call]), response])
            db = env.tools.db.cash_back_disputes.data
            changed = {r["transaction_id"] for k, r in db.items() if k not in original}
            require(all(db.get(k) == v for k, v in original.items()) and changed == set(submits), "Incorrect native DB changes")
            counts = dispute_effects(raw, [m.model_dump(mode="json") for m in messages])
            require((counts["accepted_target"], counts["accepted_other"], counts["accepted_duplicate"]) ==
                    (len(targets & changed), len(changed - targets), len(submits) - len(changed)), "Receipt and final DB differ")
            reward = EnvironmentEvaluator.calculate_reward(get_environment, task, messages, env_kwargs=kwargs)
            require(reward.db_check.db_match == (changed == targets), "Unexpected native grade on controlled changes")
            controls[name] = dict(db_success=reward.db_check.db_match, unique_target_db_rows=len(changed & targets),
                                  extra_db_rows=len(changed - targets), receipts=counts)
        results[tid] = dict(task_sha256=digest(raw_bytes), distinct_targets=len(targets), customer_transactions=len(possible),
                            required_documents=len(task.required_documents), targets_visible_on_lookup=True,
                            lookup_sha256=digest(visible.encode()), controls=controls)
    return results


def verify_dispute_state(task, messages, effects, db_sha256, official_success):
    """Strictly replay both actors' public native calls and compare DB changes with the receipt scorer."""
    from copy import deepcopy

    from agent_memory_eval.adapters.banking_dispute_evidence import dispute_targets
    from pydantic import TypeAdapter
    from tau2.data_model.message import Message
    from tau2.data_model.tasks import Task
    from tau2.domains.banking_knowledge.environment import get_environment

    native = Task.model_validate(task)
    require(native.initial_state is None, "Banking dispute replay requires a default initial state")
    env = get_environment(retrieval_variant="grep_only", task=native)
    original = deepcopy(env.tools.db.cash_back_disputes.data)
    env.set_state(initialization_data=None, initialization_actions=None,
                  message_history=TypeAdapter(list[Message]).validate_python(messages), strict=True)
    current = env.tools.db.cash_back_disputes.data
    require(all(current.get(k) == v for k, v in original.items()), "A prior dispute changed in the replayed database")
    added = [v for k, v in current.items() if k not in original]
    owner, targets = dispute_targets(task)
    require(all(v["user_id"] == owner for v in added), "Another customer's state was changed")
    ids = {v["transaction_id"] for v in added}
    checksum = env.get_db_hash()
    require(db_sha256 and checksum == db_sha256 and
            (len(ids & targets), len(ids - targets)) == (effects["accepted_target"], effects["accepted_other"]),
            "Replayed native state disagrees with recorded state or receipts")
    if official_success is not None:
        require((ids == targets) == official_success, "Native DB success differs from the observed grade")
    return dict(scope="graded_final_state" if official_success is not None else "observed_prefix",
                db_sha256=checksum, new_dispute_rows=len(added), distinct_targets=len(ids & targets),
                distinct_other=len(ids - targets))

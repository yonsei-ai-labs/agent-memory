"""Join sealed, outcome-masked action judgments to native grades and actual exposure."""

from functools import lru_cache
import json
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, read_json
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore, read_experiment


def _quote_in_message(quote, message):
    if not isinstance(quote, str) or not quote:
        return False
    if quote in (message.get("content") or ""):
        return True
    for call in message.get("tool_calls", []):
        if quote in call["name"] or quote in canonical(call.get("arguments")).decode():
            return True
        # Reviewers can quote a naturally displayed complete function call or its literal JSON arguments.
        rendered = json.dumps(call, ensure_ascii=False, sort_keys=True)
        if quote in rendered:
            return True
    return False


def check_judgment(label, sides):
    if label["category"] not in {"identity_or_lookup", "request_clarification", "policy_or_eligibility",
            "current_facts_or_arithmetic", "approval_or_reconfirmation", "business_tool_or_arguments",
            "communication", "simulator_led", "no_material_difference", "other"}:
        raise ContractError("Unknown paired-action category")
    if label["user_context"] not in {"comparable", "different", "uncertain"}:
        raise ContractError("Unknown current-user context classification")
    if not label["first_difference"].strip() or len(label["sides"]) != 2:
        raise ContractError("Each paired action needs two sides and a first-divergence description")
    for judged, actual in zip(label["sides"], sides):
        messages = actual["messages"]
        for idx, excerpt in (("index", "quote"), ("end_index", "end_quote")):
            n = judged[idx]
            if type(n) is not int or not 0 <= n < len(messages) or not _quote_in_message(judged[excerpt], messages[n]):
                raise ContractError("Paired action quote does not match its indicated public message")


def join_actions(run, results, waves, labels):
    run, waves = Path(run), Path(waves)
    schedule = read_json((run / "allocations.json").read_bytes())
    jobs = {x["index"]: x for x in schedule}
    expected = {(p["control_job"], p["treatment_job"]) for c in results["paired_contrasts"]
                if c["control"] == "no_memory" and c["treatment"] in ("fact", "episode", "lesson", "procedure")
                for pairs in c["review_cases"].values() for p in pairs}
    pairs_seen = set()
    key_rows, public = {}, {}
    for key_file in sorted(waves.glob("blocks-*/key.json")):
        keys = read_json(key_file.read_bytes())
        if set(key_rows) & set(keys):
            raise ContractError("Case appears in multiple reading packets")
        key_rows.update(keys)
        for name in ("actions-1.json", "actions-2.json"):
            public.update(read_json((key_file.parent / name).read_bytes()))
    if set(key_rows) != set(public) or set(key_rows) != set(labels):
        raise ContractError("The adjudicated case IDs do not match the masked cases")

    @lru_cache(maxsize=None)
    def evidence(idx):
        row = jobs[idx]
        folder = run / row["directory"]
        blobs = BlobStore(folder)
        _, state, _ = read_experiment(folder)
        result = row["result"]
        report = blobs.get(result["report_ref"])
        reward = report["reward_info"]
        parent = result["start_snapshot_id"]
        items = snapshot_items(blobs, state, parent) if parent else []
        return dict(job=idx, arm=row["arm"], run_id=row["run_id"], official_success=result["official_success"],
                    reward=result["official_score"], db_match=(reward.get("db_check") or {}).get("db_match"),
                    communicate_checks=reward.get("communicate_checks"), termination=result["termination_reason"],
                    normal_response_memory_spans=result["normal_response_memory_spans"],
                    exposed_notes=[{k: i[k] for k in ("title", "body")} for i in items])

    joined = {}
    for case, key in key_rows.items():
        pair = key["control_job"], key["treatment_job"]
        if pair in pairs_seen:
            raise ContractError("Duplicate primary pair in the masked sample")
        pairs_seen.add(pair)
        check_judgment(labels[case], public[case])
        joined[case] = dict(**key, judgment=labels[case], sides_in_blind_order=[evidence(i) for i in key["jobs_in_blind_order"]])
    if pairs_seen != expected:
        raise ContractError("Blind review did not cover the prespecified discordant and concordant cases")
    return dict(format="agent-memory-two-pass-forms-action-review/v1", reviewed_pairs=len(joined), pairs=joined)

"""Post-hoc action comparisons; original pilot evidence and metrics stay intact."""

from copy import deepcopy

from agent_memory_eval.contracts.core import ContractError


def without_transfer_summary(actions):
    """Keep call order and all arguments except the human-transfer synopsis."""
    result = deepcopy(actions)
    for action in result:
        if action["name"] == "transfer_to_human_agents":
            action["arguments"].pop("summary", None)
    return result


def compare_business_actions(left, right):
    comparable = left["started"] and right["started"]
    raw = left["actions"] != right["actions"] if comparable else None
    normalized = (
        without_transfer_summary(left["actions"]) != without_transfer_summary(right["actions"])
        if comparable else None
    )
    left_db, right_db = left.get("final_db_hash"), right.get("final_db_hash")
    db_changed = left_db != right_db if comparable and left_db is not None and right_db is not None else None
    return dict(
        raw_actions_changed=raw,
        actions_changed_without_transfer_summary=normalized,
        transfer_summary_only_changed=raw and not normalized if comparable else None,
        final_db_changed=db_changed,
    )


def build_action_supplement(summary):
    if summary.get("audit_status") != "audit_passed":
        raise ContractError("An audited pilot summary is required")
    runs = {run["run_id"]: run for run in summary["runs"]}
    comparisons = []
    for pair in summary["blocked_comparisons"]:
        comparison = compare_business_actions(runs[pair["baseline_run_id"]], runs[pair["treated_run_id"]])
        if comparison["raw_actions_changed"] != pair["actions_changed"]:
            raise ContractError("Original action comparison disagrees with run evidence")
        comparisons.append({**{key: pair[key] for key in (
            "block_id", "baseline", "treated", "baseline_run_id", "treated_run_id"
        )}, **comparison})
    return dict(
        format="agent-memory-tau2-action-supplement/v1",
        inference="posthoc_descriptive_shared_runs_not_independent_pairs",
        source={key: summary[key] for key in (
            "experiment_id", "audit_status", "log_sha256", "summary_sha256"
        )},
        comparison_rule=dict(
            preserve="ordered tool names and every other argument",
            excluded_arguments=[dict(tool="transfer_to_human_agents", argument="summary")],
            interpretation="difference is not a judgment of usefulness, harm or policy compliance",
        ),
        counts=dict(
            scheduled_pairs=len(comparisons),
            comparable_action_pairs=sum(row["raw_actions_changed"] is not None for row in comparisons),
            raw_action_changes=sum(row["raw_actions_changed"] is True for row in comparisons),
            action_changes_without_transfer_summary=sum(
                row["actions_changed_without_transfer_summary"] is True for row in comparisons
            ),
            transfer_summary_only_changes=sum(row["transfer_summary_only_changed"] is True for row in comparisons),
            comparable_final_db_pairs=sum(row["final_db_changed"] is not None for row in comparisons),
            final_db_changes=sum(row["final_db_changed"] is True for row in comparisons),
        ),
        blocked_comparisons=comparisons,
    )


def analyze_pilot_actions(root):
    from agent_memory_eval.analysis.tau2_autonomy import audit_pilot

    return build_action_supplement(audit_pilot(root))

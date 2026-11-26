"""Read first-action and final DB observations from the archived two-visit retail study."""

from agent_memory_eval.analysis.paired_trials import OutcomeMetric, Trial
from agent_memory_eval.contracts.core import ContractError, canonical, digest


FIRST_TARGET = OutcomeMetric(
    name="retail.first_target_lookup", version=1, definition="The first business action looks up only the target order.",
    stage="first_business_action", unit="revisit_case", denominator="observed_first_actions", cluster_unit="customer",
    cluster_eligibility="any_pair_graded",
)
FINAL_DB = OutcomeMetric(
    name="retail.final_db_success", version=1, definition="The official final retail database reward equals one.",
    stage="episode_end", unit="revisit_case", denominator="completed_consultations_with_db_grade", cluster_unit="customer",
    cluster_eligibility="any_pair_graded",
)


def _first_action(row):
    status, behavior = row["decision_status"], row["behavior"]
    if status in {"checkpoint", "decision"}:
        if behavior == "unobserved":
            return True, None
        if behavior not in {"focused_target", "includes_target_with_other_calls", "other_order_only",
                            "customer_reply", "other_tool", "invalid_lookup", "mutation_requested"}:
            raise ContractError(f"Unknown first retail action: {behavior}")
        return True, behavior == "focused_target"
    if status not in {"not_started", "bank_missing", "interrupted", "error", "private_read_limit"} or behavior != "unobserved":
        raise ContractError(f"Inconsistent retail decision status and action: {status}, {behavior}")
    return status not in {"not_started", "bank_missing"}, None


def _final_db(row):
    status, reward = row["completion"], row["db_reward"]
    if status == "completed":
        if reward is None:
            return True, None
        if type(reward) not in (int, float) or reward not in (0, 1):
            raise ContractError("Expected a binary retail DB reward")
        return True, reward == 1
    if status not in {"not_started", "decision_missing", "interrupted", "error"} or reward is not None:
        raise ContractError(f"Inconsistent retail continuation status and grade: {status}, {reward}")
    return status not in {"not_started", "decision_missing"}, None


def revisit_trials(study, condition, metric, mode):
    if not mode or study.get("study") != "tau2-retail-two-visit-01":
        raise ContractError("Retail observations need a two-visit result and a customer request mode")
    if metric == FIRST_TARGET:
        collection, score = "decisions", _first_action
    elif metric == FINAL_DB:
        collection, score = "continuations", _final_db
    else:
        raise ContractError("Unknown retail revisit metric")
    cohort = digest(canonical([study["registration_sha256"], study["schedule_sha256"],
                               study.get("recovery_registration_sha256"), mode]))
    trials = []
    for index, row in enumerate(study[collection]):
        if row["arm"] == condition and row["mode"] == mode:
            started, success = score(row)
            trials.append(Trial(row["task"], row["source"], row["customer"], started, success,
                                metric=metric, cohort=cohort, evidence_pointer=f"/{collection}/{index}"))
    if not trials:
        raise ContractError(f"No registered retail rows for {mode}/{condition}")
    return trials

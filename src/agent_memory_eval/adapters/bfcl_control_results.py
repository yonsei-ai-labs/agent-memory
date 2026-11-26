"""Score the two read permissions of a recorded, scripted BFCL native-memory control."""

from agent_memory_eval.analysis.paired_trials import OutcomeMetric, Trial
from agent_memory_eval.contracts.core import ContractError, canonical, digest


BFCL_ANSWER = OutcomeMetric(
    name="bfcl.official_answer", version=1, definition="The native BFCL agentic checker accepts the answer.",
    stage="episode_end", unit="probe", denominator="graded_scripted_probes", cluster_unit="prerequisite",
    cluster_eligibility="all_pairs_graded",
)
BFCL_EXPOSURE = OutcomeMetric(
    name="bfcl.body_in_next_input", version=1, definition="A version-bound memory body is in the next worker input.",
    stage="next_worker_input", unit="probe", denominator="audited_scripted_probes", cluster_unit="prerequisite",
    cluster_eligibility="all_pairs_graded",
)


def control_trials(study, condition, metric, mode=None):
    if (mode is not None or study.get("format") != "bfcl-native-memory-control/v1" or study.get("scripted") is not True
            or study.get("equal_first_reader_wire") is not True or study.get("native_snapshot_restored") is not True):
        raise ContractError("Expected a scripted BFCL control with matched inputs and a restored snapshot")
    if condition not in {"read_enabled", "access_control"}:
        raise ContractError(f"Unknown BFCL control condition: {condition}")
    if metric == BFCL_ANSWER:
        field = "official_success"
        success = study[field][condition]
        if type(success) is not bool:
            raise ContractError("Missing or invalid BFCL control grade")
    elif metric == BFCL_EXPOSURE:
        field = "version_bound_input_spans"
        spans = study[field][condition]
        if type(spans) is not int or spans < 0:
            raise ContractError("Invalid number of BFCL input spans")
        success = spans > 0
    else:
        raise ContractError("Unknown BFCL control metric")
    source = study["prerequisite"]
    cohort = digest(canonical([study["format"], study["benchmark_revision"], source, study["source_fact_sha256"]]))
    return [Trial(study["task"], "probe", source, True, success, metric=metric, cohort=cohort,
                  evidence_pointer=f"/{field}/{condition}")]

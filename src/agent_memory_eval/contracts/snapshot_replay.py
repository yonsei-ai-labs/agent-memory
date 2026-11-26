"""One recorded reply from a frozen context; never an episode or policy-effect claim."""

from functools import lru_cache

from jsonschema import Draft202012Validator, ValidationError

from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT
from agent_memory_eval.recording.provenance import public_assistant_message


SCOPE = "single_reply_no_business_execution_or_official_score"


@lru_cache(maxsize=1)
def _validator():
    return Draft202012Validator(read_json((SCHEMA_ROOT / "snapshot-replay.schema.json").read_bytes()))


def validate_replay_record(value):
    try:
        canonical(value)
        _validator().validate(value)
    except (ValueError, TypeError, ValidationError) as error:
        raise ContractError("Invalid snapshot replay record") from error
    if value["format"] == "agent-memory-snapshot-replay-config/v1":
        policy, limits = MemoryPolicy(**value["policy"]), ExecutionLimits(**value["execution_limits"])
        if (not policy.preloads or policy.tools("solve") or policy.write_timing != "none"
                or policy.same_episode_read or not value["instruction"].strip()
                or (limits.worker_requests, limits.writer_requests, limits.user_simulator_requests) != (1, 0, 0)
                or (value["record_kind"] == "observed") != (value["verified_model"] is not None)):
            raise ContractError("Replay needs a frozen preload and exactly one worker reply")
    elif value["stop_required"] != (value["unknown_cost"] or value["error_code"] is not None):
        raise ContractError("Replay stop requirement differs from its error or unknown cost")


def replay_response_error(raw, verified_model):
    """Classify completion from the original body, without executing proposed tool calls."""
    if not isinstance(raw, dict):
        return "REPLAY_INVALID_RESPONSE"
    if verified_model is not None and (not isinstance(raw.get("model"), str) or raw["model"] not in {
            key for key in (verified_model["id"], verified_model["canonical_slug"]) if key is not None}
            or raw.get("provider") != verified_model["provider_name"]):
        return "REPLAY_MODEL_MISMATCH"
    choices = raw.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return "REPLAY_INVALID_RESPONSE"
    choice, message = choices[0], choices[0].get("message")
    if not isinstance(message, dict):
        return "REPLAY_INVALID_RESPONSE"
    if not isinstance(choice.get("finish_reason"), str):
        return "REPLAY_INVALID_RESPONSE"
    if message.get("refusal") or choice.get("finish_reason") not in {"stop", "tool_calls"}:
        return "REPLAY_INCOMPLETE_REPLY"
    try:
        public = public_assistant_message(message)
        calls = public.get("tool_calls") or []
        if (bool(calls) != (choice["finish_reason"] == "tool_calls")
                or len({c["id"] for c in calls}) != len(calls)
                or any(not c["id"] or not c["function"]["name"] for c in calls)):
            return "REPLAY_INVALID_RESPONSE"
    except (ContractError, KeyError, TypeError):
        return "REPLAY_INVALID_RESPONSE"
    return None

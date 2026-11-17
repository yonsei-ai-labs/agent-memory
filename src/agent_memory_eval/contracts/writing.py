"""The small writing-only profile; all experimental limits must be explicit."""

from copy import deepcopy
from functools import lru_cache
import random

from jsonschema import Draft202012Validator, ValidationError

from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, read_json
from agent_memory_eval.contracts.execution import ExecutionLimits
from agent_memory_eval.contracts.validation import ROOT


@lru_cache(maxsize=1)
def _validator():
    schema = read_json((ROOT / "schemas/writing-exploration.schema.json").read_bytes())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def writing_profile(profile, *, require_ready=False):
    try:
        canonical(profile)
        _validator().validate(profile)
    except (ValueError, TypeError, ValidationError) as error:
        raise ContractError("Invalid writing-only profile") from error
    missing = [key for key in ("repetitions", "schedule_seed", "bank_limits", "page_size", "timeout_seconds")
               if profile[key] is None]
    missing += [section + "." + key for section in ("execution_limits", "guards")
                for key, value in profile[section].items() if value is None]
    ready = profile["status"] == "ready" and not missing
    if (require_ready or profile["status"] == "ready") and not ready:
        raise ContractError("Writing profile is not ready: " + ", ".join(missing or ["status=draft"]))
    if not missing:
        ExecutionLimits(**profile["execution_limits"])
        writer_policy(profile)
    return dict(profile=deepcopy(profile), ready=ready, unresolved_fields=missing)


def writer_policy(profile):
    return MemoryPolicy(True, "post_task", False, profile["page_size"], read_mode="none", **profile["bank_limits"])


def writing_allocations(profile):
    rng, rows = random.Random(profile["schedule_seed"]), []
    for repetition in range(1, profile["repetitions"] + 1):
        levels = list(profile["feedback_levels"])
        rng.shuffle(levels)
        rows.extend(dict(repetition=repetition, feedback_level=level) for level in levels)
    return rows

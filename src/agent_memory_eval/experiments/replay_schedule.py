"""A shared reply belongs to one case, canonical input and replay repetition."""

from collections import defaultdict
from random import Random

from jsonschema import Draft202012Validator, ValidationError

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT
from agent_memory_eval.experiments.writing_comparison import ARMS


def validate_profile(profile, *, require_ready=False):
    try:
        canonical(profile)
        schema = read_json((SCHEMA_ROOT / "written-replay-batch.schema.json").read_bytes())
        Draft202012Validator(schema).validate(profile)
    except (ValueError, TypeError, ValidationError) as error:
        raise ContractError("Invalid written replay batch profile or unresolved ready settings") from error
    # JSON Schema also accepts integral floats. Runtime limits and seeds require integers.
    for key in ("repetitions", "schedule_seed", "max_input_bytes", "max_model_requests"):
        if profile[key] is not None and type(profile[key]) is not int:
            raise ContractError("Expected an integer for " + key)
    if profile["bank_limits"] is not None and any(cap is not None and type(cap) is not int
                                                  for cap in profile["bank_limits"].values()):
        raise ContractError("Bank admission limits must be integer or explicitly unbounded")
    if require_ready and profile["status"] != "ready":
        raise ContractError("Live replay requires its own ready, explicitly authorized batch profile")
    return sorted(key for key, value in profile.items() if value is None)


def input_groups(rows):
    grouped, pairs = defaultdict(list), defaultdict(list)
    for row in rows:
        case, writer, arm = row["case_id"], row["writer_run_id"], row["arm"]
        if row["pair_id"] != case + ":" + writer:
            raise ContractError("Invalid case and writer pairing")
        pairs[case, writer].append(arm)
        grouped[case, row["request_sha256"]].append({key: row[key] for key in (
            "arm", "pair_id", "writer_run_id", "feedback_level", "writer_repetition", "writing_status", "snapshot_id")})
    if not pairs or any(sorted(arms) != sorted(ARMS) for arms in pairs.values()):
        raise ContractError("Each case and writing allocation needs exactly four distinct arms")
    return [dict(case_id=case, request_sha256=key, members=members)
            for (case, key), members in sorted(grouped.items())]


def schedule(groups, repetitions, seed):
    """One shuffled block per repetition. Shared controls keep all allocation links."""
    random = Random(seed)
    result = []
    for repetition in range(1, repetitions + 1):
        block = []
        for group in groups:
            identity = [repetition, group["case_id"], group["request_sha256"]]
            block.append(dict(run_id="replay_" + digest(canonical(identity)), repetition=repetition,
                              case_id=group["case_id"], request_sha256=group["request_sha256"]))
        random.shuffle(block)
        result.extend(block)
    return result


def local_snapshot(catalog, member):
    if member["arm"] == "model_written":
        return catalog["writers"][member["writer_run_id"]]
    return catalog["controls"][member["arm"]]

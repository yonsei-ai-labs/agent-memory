"""Load a declared study plan, pin its assets, and list the representation × reading conditions it runs."""

from pathlib import Path

from jsonschema import Draft202012Validator

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import SCHEMA_ROOT
from agent_memory_eval.recording.assets import asset_text

FORMAT = "agent-memory-study-plan/v1"
SCHEMA = "study-plan-v1.schema.json"


def _schema():
    return read_json((SCHEMA_ROOT / SCHEMA).read_bytes())


def content_text(root, content):
    """Inline text or the exact bytes of a pinned file, without a trailing newline."""
    if "text" in content:
        return content["text"]
    return asset_text(root, content).rstrip("\r\n")


def arm_name(representation, reading):
    return f"{representation}__{reading}"


def conditions(plan):
    return [(c["representation"], c["reading"]) for c in plan["conditions"]]


def plan_digest(plan):
    return digest(canonical(plan))


def check_plan(plan, root):
    error = next(Draft202012Validator(_schema()).iter_errors(plan), None)
    if error is not None:
        raise ContractError(f"Invalid study plan: {error.message}")
    held_out = plan["benchmark"].get("confirmation_tasks", [])
    if not set(held_out) <= set(plan["benchmark"]["tasks"]):
        raise ContractError("Confirmation tasks must be planned tasks")
    if held_out and len(held_out) == len(plan["benchmark"]["tasks"]):
        raise ContractError("Confirmation tasks must leave development tasks to analyze")
    pairs = conditions(plan)
    if len(set(pairs)) != len(pairs):
        raise ContractError("Duplicate condition in the study plan")
    if pairs and not ("writing" in plan and "reading" in plan):
        raise ContractError("Conditions need declared writing and reading policies")
    reps, readers = plan.get("writing", {}).get("representations", {}), plan.get("reading", {})
    if any(rep not in reps or reader not in readers for rep, reader in pairs):
        raise ContractError("Undeclared representation or reading in a condition")
    modes = [reader["read_mode"] for reader in readers.values()]
    if len(set(modes)) != len(modes):
        raise ContractError("Version 1 plans allow one reader per read mode")
    if any("tool_descriptions" in r and r["read_mode"] != "on_demand" for r in readers.values()):
        raise ContractError("Tool descriptions apply to on_demand readers only")
    if any("notice" not in r and "tool_descriptions" not in r for r in readers.values()):
        raise ContractError("A reader needs a notice, tool descriptions, or both")
    refs = [plan["benchmark"]["setup"], *([plan["writing"]["instruction"]] if "writing" in plan else []), *reps.values(),
            *plan.get("decision", {}).get("evidence", []),
            *(r["notice"] for r in readers.values() if "notice" in r),
            *(r["setup"] for r in plan.get("references", []) if "setup" in r)]
    for ref in refs:
        if "path" in ref:
            asset_text(root, ref)
    return plan


def load_plan(path, root):
    """Read and fully check a plan, including every pinned file it names."""
    return check_plan(read_json(Path(path).read_bytes()), Path(root))

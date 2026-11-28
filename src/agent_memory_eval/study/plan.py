"""Load a declared study plan, pin its assets, and list the representation × reading conditions it runs."""

from pathlib import Path

from jsonschema import Draft202012Validator

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import SCHEMA_ROOT
from agent_memory_eval.recording.assets import asset_bytes, asset_text

FORMAT = "agent-memory-study-plan/v2"
LEGACY_FORMAT = "agent-memory-study-plan/v1"  # Runs recorded before models were declared; readable, not runnable.
SCHEMA = "study-plan-v1.schema.json"  # Covers both formats; v2 requires the models block.
STANDARD_MODELS = "protocols/models/project-standard-v1.json"
LEGACY_FOLLOWUP_FORMAT = "agent-memory-retail-followup/v1"
FOLLOWUP_FORMAT = "agent-memory-retail-followup/v3"
FOLLOWUP_FORMATS = {LEGACY_FOLLOWUP_FORMAT, "agent-memory-retail-followup/v2", FOLLOWUP_FORMAT}


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
    if isinstance(plan, dict) and plan.get("format") in FOLLOWUP_FORMATS:
        schema = read_json((SCHEMA_ROOT / "retail-followup-plan-v1.schema.json").read_bytes())
        error = next(Draft202012Validator(schema).iter_errors(plan), None)
        if error:
            raise ContractError(f"Invalid follow-up plan: {error.message}")
        if plan["id"] == plan["benchmark"]["recording"]:
            raise ContractError("The follow-up must have a different destination from its original recording")
        model_profile(plan, root)
        asset_bytes(root, plan["benchmark"]["decisions"])
        return plan
    error = next(Draft202012Validator(_schema()).iter_errors(plan), None)
    if error is not None:
        raise ContractError(f"Invalid study plan: {error.message}")
    held_out = plan["benchmark"].get("confirmation_tasks", [])
    if not set(held_out) <= set(plan["benchmark"]["tasks"]):
        raise ContractError("Confirmation tasks must be planned tasks")
    if held_out and len(held_out) == len(plan["benchmark"]["tasks"]):
        raise ContractError("Confirmation tasks must leave development tasks to analyze")
    if "required_sources_sha256" in plan["first_pass"] and (
            plan["format"] != FORMAT or plan["benchmark"]["adapter"] == "bfcl-relevance" or not plan["first_pass"].get("reuse")):
        raise ContractError("Pinning first-pass sources requires a v2 τ² plan with reuse recordings")
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
        # A τ² v2 study can vary only the two tool descriptions on the same sealed bank.
        tool_only_queries = all("tool_descriptions" in r and "notice" not in r
                                for r in readers.values() if r["read_mode"] == "on_demand")
        if (plan["format"] != FORMAT or plan["benchmark"]["adapter"] == "bfcl-relevance"
                or modes.count("preload") > 1 or not tool_only_queries):
            raise ContractError("Use one reader per read mode except for tool-only queries in a v2 τ² study")
    if any("tool_descriptions" in r and r["read_mode"] != "on_demand" for r in readers.values()):
        raise ContractError("Tool descriptions apply to on_demand readers only")
    if any("notice" not in r and "tool_descriptions" not in r for r in readers.values()):
        raise ContractError("A reader needs a notice, tool descriptions, or both")
    model_profile(plan, root)
    refs = [plan["benchmark"]["setup"],
            *([plan["writing"]["instruction"]] if "writing" in plan else []), *reps.values(),
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


def require_declared_models(plan):
    """Only plans that name the models under study may spend or run."""
    if plan["format"] in FOLLOWUP_FORMATS - {FOLLOWUP_FORMAT}:
        raise ContractError("Use the current follow-up with blinded primary review and worker limits before authorizing or running")
    if plan["format"] not in {FORMAT, FOLLOWUP_FORMAT} or "models" not in plan:
        raise ContractError("Declare the models under study (plan format v2) before running")
    return plan["models"]


def model_profile(plan, root):
    if "models" not in plan:
        return None
    models = plan["models"]
    if models["profile"]["path"] != STANDARD_MODELS and not models.get("comparison"):
        raise ContractError("Plans use the project model profile unless they declare a model comparison")
    profile = read_json(asset_text(root, models["profile"]).encode())
    if (not isinstance(profile, dict) or profile.get("format") != "agent-memory-model-profile/v1"
            or profile.get("writer") != "worker"):
        raise ContractError("Model profile must name one worker model that also writes the memories")
    terms, revisions = profile.get("endpoint_terms"), profile.get("expected_revisions")
    if (not isinstance(terms, dict) or not isinstance(terms.get("worker"), dict)
            or not terms["worker"].get("model") or not isinstance(revisions, dict) or not revisions.get("worker")):
        raise ContractError("Model profile must identify the worker endpoint and revision")
    if plan.get("format") in FOLLOWUP_FORMATS:
        if not isinstance(terms.get("user_simulator"), dict) or not revisions.get("user_simulator"):
            raise ContractError("The follow-up model profile must identify the user_simulator endpoint and revision")
    return profile

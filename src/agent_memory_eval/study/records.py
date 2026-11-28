"""Write-once study records and the sealed first-pass reader shared by staged adapters."""

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.forms_write import FormSource, _project_context
from agent_memory_eval.recording.batch import sealed_rows
from agent_memory_eval.recording.experience import _closed_run, _pair, _source
from agent_memory_eval.recording.journal import safe_path

__all__ = ["read", "fixed", "sealed_rows", "source_from", "shard_seal", "first_pass_shard"]


def read(path):
    return read_json(path.read_bytes())


def fixed(path, data):
    """Write a record once; a later write must carry identical contents."""
    if path.exists():
        if read(path) != data:
            raise ContractError(f"Previously sealed {path.name} has different contents")
    else:
        write_json(path, data)


def shard_seal(folder):
    return digest((folder / "completion.json").read_bytes())


def first_pass_shard(workspace, run_root, case, effective_sources=None, *, entry=None):
    """Locate the registered source draw, including its pinned replacement after a continuation."""
    imported = case.get("imported_first_pass")
    if effective_sources:
        binding = effective_sources.get(case["task"])
        if imported or not binding or not binding.get("seal"):
            raise ContractError(f"Missing effective first-pass binding for {case['task']}")
        folder = safe_path(workspace, binding["folder"])
        if not (folder / "completion.json").is_file():
            raise ContractError(f"Missing sealed effective first pass for {case['task']}")
        _, control, (row,) = sealed_rows(folder, binding["seal"])
        result = row.get("result") or {}
        if (control["profile"]["case"] != case["task"] or control["design"]["stage"] != "source"
                or (row["task_id"], row["arm"], row["run_id"]) != (case["task_id"], "no_memory", binding["run_id"])
                or result.get("official_success") is not binding["success"]
                or (result.get("grading_status") == "graded") != (type(binding["success"]) is bool)):
            raise ContractError(f"Effective first pass differs from its registered draw for {case['task']}")
        return folder, binding
    if (run_root / "write-continuation.json").exists():
        raise ContractError("A continued study requires its sealed effective first-pass bindings")
    if imported:
        return safe_path(workspace / "runs", imported["directory"]), imported
    return run_root / ("task-" + case["task"]) / "source", entry or {}


def source_from(folder, task, *, binding=None):
    """Public experience of a graded no-memory first pass, verified against its own journal."""
    binding = binding or {}
    _, ctl, rows = sealed_rows(folder, binding.get("seal"))
    (row,) = rows
    res = row.get("result") or {}
    if (row["arm"], row["task_id"], ctl["design"]["stage"]) != ("no_memory", task, "source"):
        raise ContractError("Expected the matching first-pass source")
    if res.get("grading_status") != "graded":
        return None
    with _source(safe_path(folder, row["directory"]), res["log_sha256"]) as (scan, blobs, _, _):
        run = row["run_id"]
        start, end, grade = _closed_run(scan, run)
        req, reply = _pair(scan, run)
        context = _project_context(blobs.get(req["payload"]["request_ref"]), blobs.get(reply["payload"]["response_ref"]))
        if (start["record_kind"] != "observed" or start["condition_id"] != "no_memory" or start["payload"]["bank_id"]
                or start["payload"]["source_run_ids"] or not start["payload"]["fresh_session"]
                or context["task_context"] != blobs.get(start["payload"]["task_ref"])
                or grade["payload"]["official_success"] is not res["official_success"]):
            raise ContractError("First-pass public projection or outcome differs")
        data = canonical(context)
        checksum = digest(data)
        if "public_context_sha256" in binding and checksum != binding["public_context_sha256"]:
            raise ContractError("First-pass public experience differs from the sealed binding")
        return FormSource(task, run, res["log_sha256"], end["event_id"], end["event_seq"], data.decode(), checksum,
                          len(data), res["official_success"], grade["event_id"])

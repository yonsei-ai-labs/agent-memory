"""Write-once study records and the sealed first-pass reader shared by staged adapters."""

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.forms_write import FormSource, _project_context
from agent_memory_eval.recording.batch import sealed_rows
from agent_memory_eval.recording.experience import _closed_run, _pair, _source
from agent_memory_eval.recording.journal import safe_path

__all__ = ["read", "fixed", "sealed_rows", "source_from", "shard_seal"]


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


def source_from(folder, task):
    """Public experience of a graded no-memory first pass, verified against its own journal."""
    _, ctl, rows = sealed_rows(folder)
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
        return FormSource(task, run, res["log_sha256"], end["event_id"], end["event_seq"], data.decode(), digest(data),
                          len(data), res["official_success"], grade["event_id"])

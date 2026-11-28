"""Register a prospective renderer fix against an exact, already stopped reader batch."""

import re
import subprocess
from pathlib import PurePosixPath

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_bytes
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.records import read, sealed_rows
from agent_memory_eval.study.writer_stage import file_ref


def boundary(folder, directory, batch):
    """Capture a stopped batch after its workers exit. A partially executed native shard cannot cross versions."""
    from agent_memory_eval.study.reader_wire_evidence import verified_recovery

    controller = directory / "controller"
    require((controller / "registration.json").is_file() and not (controller / "completion.json").exists()
            and not (directory / "result.json").exists(), "Wire repair needs an open registered reader controller")
    tasks = set(batch["tasks"])
    cases = {c["task"] for c in read(folder / "registration.json")["preflight"]["cases"]
             if "donor" in c and c["task_id"] in tasks}
    require(len(cases) == len(tasks), "Wire repair targets differ from the original schedule")
    files = {"registration.json": digest((controller / "registration.json").read_bytes())}
    funded = set()
    for path in sorted(controller.glob("*-wave-*.json")):
        files[path.name] = digest(path.read_bytes())
        if path.name.endswith(".settled.json"):
            continue
        stage = path.name.rpartition("-wave-")[0]
        grant = read(path)
        require(grant["tasks"] and set(grant["tasks"]) <= cases, "Unexpected task in the original grant")
        funded.update("task-" + case + "/" + stage for case in grant["tasks"])
    previous, recoveries = {}, {}
    for case in sorted(cases):
        for shard in sorted((folder / ("task-" + case)).glob("solve*")):
            name = str(shard.relative_to(folder))
            require(name in funded and (shard / "completion.json").is_file(), "Original reader shard is still open")
            close, control, rows = sealed_rows(shard)
            require(control["implementation"] == batch["implementation"], "Unexpected previous reader implementation")
            previous[name] = digest((shard / "completion.json").read_bytes())
            if close.get("stop_reason") in {"first_pass_mismatch", "task_start_mismatch"} or any(
                    (row.get("result") or {}).get("error_code") in {"FIRST_PASS_MISMATCH", "TASK_START_MISMATCH"}
                    for row in rows):
                recoveries[name] = verified_recovery(shard, expected_seal=previous[name])
    require(recoveries, "No proven worker-history validation stop in this batch")
    return dict(previous_shards=previous, recoveries=recoveries, controller_files=files)


def _value(root, directory, ref, identity):
    from agent_memory_eval.study.reader_stage import _check_runtime

    spec = read_json(asset_bytes(root, ref))
    folder = directory.parent.parent
    batch = read_json(asset_bytes(directory, dict(path="registration.json", sha256=spec["batch_registration_sha256"])))
    parent = read_json(asset_bytes(root, batch["batch_ref"]))
    require(PurePosixPath(ref["path"]).parent == PurePosixPath("protocols/diagnostics")
            and spec["format"] == "agent-memory-cohort-reader-wire-repair/v1"
            and re.fullmatch(r"[a-z0-9][a-z0-9-]*", spec["id"])
            and re.fullmatch(r"[0-9a-f]{40}", spec["runtime_commit"])
            and spec["batch_ref"] == batch["batch_ref"] and spec["approval_ref"] == batch["approval_ref"]
            and spec["plan"] == parent["plans"][batch["plan_id"]]["plan"]
            and parent["id"] == directory.name == batch["id"] and folder.name == batch["plan_id"]
            and spec["additional_max_usd"] == "0" and batch.get("operations") and spec["recoveries"]
            and set(spec["recoveries"]) <= set(spec["previous_shards"]), "Invalid scope or funding for a reader wire repair")
    commit = identity["implementation_commit"]
    recorded = subprocess.run(["git", "-C", root, "show", f"{commit}:{ref['path']}"], capture_output=True)
    require(recorded.returncode == 0 and digest(recorded.stdout) == ref["sha256"]
            and identity["lock_ref"] == batch["implementation"]["lock_ref"], "Wire repair was not frozen with its scope and lock")
    _check_runtime(root, spec["runtime_commit"], commit)
    asset_bytes(root, spec["approval_ref"])
    return dict(format="agent-memory-cohort-reader-wire-repair-registration/v1", id=spec["id"], spec_ref=ref,
                batch_registration_sha256=spec["batch_registration_sha256"], prior_implementation=batch["implementation"],
                implementation=identity, runtime_commit=spec["runtime_commit"], previous_shards=spec["previous_shards"],
                recoveries=spec["recoveries"], controller_files=spec["controller_files"])


def load_repair(directory, *, root=ROOT):
    """Read both identities and the immutable paid prefix for native replay or analysis."""
    marker = directory / "wire-repair.json"
    if not marker.is_file():
        return None
    saved = read(marker)
    require(saved == _value(root, directory, saved["spec_ref"], saved["implementation"]), "Changed wire repair registration")
    folder = directory.parent.parent
    for name, sha in saved["controller_files"].items():
        asset_bytes(directory / "controller", dict(path=name, sha256=sha))
    for name, sha in saved["previous_shards"].items():
        asset_bytes(safe_path(folder, name), dict(path="completion.json", sha256=sha))
    return saved


def register_repair(root, folder, directory, path, identity, *, publish):
    from agent_memory_eval.study.reader_stage import _compatible_code, _publish

    ref = file_ref(root, path)
    saved = load_repair(directory, root=root)
    value = _value(root, directory, ref, saved["implementation"] if saved else identity)
    if saved:
        _compatible_code(root, saved["implementation"], identity)
        require(saved == value, "Resume with the registered wire repair")
        return saved
    observed = boundary(folder, directory, read(directory / "registration.json"))
    require(all(value[key] == data for key, data in observed.items()), "Original reader prefix or affected slots changed")
    if publish:
        _publish(directory / "wire-repair.json", value)
    return value

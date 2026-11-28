"""Read supplemental retry attempts charged to the original cohort reader allowance."""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import NamedTuple

from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_supplement import cooldown_seconds
from agent_memory_eval.recording.assets import asset_bytes
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.orchestrator import shard_accounting
from agent_memory_eval.study.records import read, sealed_rows


class Attempt(NamedTuple):
    ordinal: int
    shard: Path
    row: dict
    cost: dict
    receipt: dict
    dispatch: dict


class Supplement(NamedTuple):
    """An open allowance includes its live dispatch grant until the last receipt releases unused funds."""

    batch: str
    case: str
    attempts: list
    pending: Path | None
    pending_grant: Decimal
    open_allowance: Decimal
    directory: Path
    registration: dict
    completion: dict | None


def _pinned(folder, name, checksum):
    return read_json(asset_bytes(folder, dict(path=name, sha256=checksum)))


def _authority(root, directory, plan, record):
    parent = directory.parent.parent
    marker = directory / "registration.json"
    scope = read(marker)
    policy = read_json(asset_bytes(root, scope["policy"]))
    require(Path(scope["policy"]["path"]).parent == Path("artifacts/approvals")
            and policy["format"] == "agent-memory-reader-http503-supplement/v1"
            and scope["format"] == "agent-memory-reader-supplement-registration/v1"
            and policy["id"] == directory.name and policy["plan_id"] == scope["plan_id"] == plan["id"]
            and policy["task"] == scope["task"] and scope["batch_id"] == parent.name and policy["user_authorization"].strip()
            and read_json(asset_bytes(root, policy["plan"])) == plan, "Reader supplement has another authority or task")
    release = read_json(asset_bytes(root, policy["parent_results"]))
    domain = release["benchmarks"][policy["benchmark"]]
    original = _pinned(parent, "registration.json", domain["batch_registration_sha256"])
    result = _pinned(parent, "result.json", domain["result_sha256"])
    collection = _pinned(parent, "controller/completion.json", result["controller_sha256"])
    batch = read_json(asset_bytes(root, policy["batch"]))
    grant = read_json(asset_bytes(root, policy["original_grant"]))
    part = batch["plans"][plan["id"]]
    eligible = {r["condition"] for r in domain["missing_conditions"] if r["reason"] == "HTTP_503"}
    own_cap = Decimal(policy["cumulative_benchmark_max_usd"])
    pool_cap = Decimal(policy["cumulative_batch_max_usd"])
    prior = Decimal(result["reported_usd"]) + Decimal(result["held_usd"])
    allowance = min(own_cap - prior, pool_cap - Decimal(release["reported_usd"]) - Decimal(release["held_usd"]))
    require(batch["id"] == release["batch"] == original["id"] == parent.name
            and policy["batch"] == original["batch_ref"] == grant["batch_ref"] and release["batch_sha256"] == policy["batch"]["sha256"]
            and policy["original_grant"] == original["approval_ref"] and part["plan"] == policy["plan"]
            and scope["parent_implementation"] == original["implementation"]
            and scope["parent_registration_sha256"] == domain["batch_registration_sha256"]
            and scope["parent_result_sha256"] == domain["result_sha256"] == policy["result_sha256"]
            and result["complete"] and part["tasks"] == original["tasks"] == [scope["task"]]
            and scope["task"] == domain["task"] and original["plan_sha256"] == record["plan_sha256"]
            and set(scope["original_readers"]) == set(scope["original_bindings"]) == set(policy["conditions"]) == eligible
            and scope["prior_reported_usd"] == result["reported_usd"] == domain["reported_usd"]
            and scope["prior_held_usd"] == result["held_usd"] == domain["held_usd"]
            and own_cap == Decimal(original["max_usd"]) == Decimal(part["max_usd"]) == Decimal(grant["plan_caps_usd"][plan["id"]])
            and pool_cap == Decimal(batch["max_usd"]) == Decimal(grant["max_usd"])
            and Decimal(scope["max_new_covered_usd"]) == allowance > 0, "Reader supplement differs from its released scope")
    case, = [c for c in record["cases"] if "donor" in c and c["task_id"] == scope["task"]]
    require(case["scheduled_stages"] == ["solve"], "Supplement needs an unambiguous original reader repeat")
    return scope, allowance, case, collection, policy["required_error_role"]


def _verify_failure(shard, row, saved):
    result = row["result"]
    log = RecordedDecisions(safe_path(shard, row["directory"]), row["run_id"], result["log_sha256"])
    event = log.of_type("model.request_finished")[-1]
    reply = event["payload"]
    start, = [r for r in log.requests if r["request_id"] == reply["request_id"]]
    error = log.blobs.get(reply["response_ref"])["error"]
    at = datetime.fromisoformat(event["timestamp_utc"])
    require(reply["status"] == "error" and reply["delivery_status"] == "response_received" and error["code"] == 503
            and saved == dict(role=start["actor_role"], at=at.timestamp(), retry_after=cooldown_seconds(error, 0, at),
                              response_sha256=reply["response_ref"]["sha256"]), "Changed provider failure receipt")


def _previous(folder, plan, scope, case, collection, role):
    last = len(plan["retry"]["wait_seconds"])
    seen = set()
    latest = {}
    for i in range(last + 1):
        name = f"task-{case['task']}/" + ("solve" if not i else f"solve-retry-{i}")
        shard = safe_path(folder, name)
        _, ctl, rows = sealed_rows(shard, collection["stages"][name]["seal"])
        original = {row["arm"]: row for row in rows}
        for arm, history in scope["original_readers"].items():
            require(arm in original and len(history) == last + 1, "Missing original retry history")
            row, binding = original[arm], scope["original_bindings"][arm]
            result, saved = row["result"], history[i]
            require(row["status"] == "error" and result["official_success"] is None
                    and result["error_code"] == "HTTP_503" and row["index"] == binding["index"] == saved["index"]
                    and (row["run_id"], result["session_id"], result["log_sha256"], result["first_worker_request_sha256"])
                    == (saved["run"], saved["session"], saved["log"], saved["first_worker"])
                    and saved["first_worker"] == history[0]["first_worker"] and saved["first_worker"]
                    and ctl["design"]["delivery_arms"][arm]["sources_sha256"] == binding["sources_sha256"]
                    and (not i or ctl["design"]["interrupted_readers"][arm] == binding),
                    "Supplement retry lineage differs from its sealed original attempt")
            require(saved["error"]["role"] == role, "Different original provider role")
            _verify_failure(shard, row, saved["error"])
            seen.update((row["run_id"], result["session_id"]))
            latest[arm] = (i, row["run_id"], "HTTP_503")
    return latest, seen


def _settled(workspace, shard, intent, scope, registration, case, receipt, seen):
    close, ctl, (row,) = sealed_rows(shard, receipt["seal"])
    result = row.get("result") or {}
    arm, grant = intent["condition"], Decimal(intent["grant_usd"])
    binding = scope["original_bindings"][arm]
    profile, design = ctl["profile"], ctl["design"]
    cost = shard_accounting(shard, grant)
    success, run, session = result.get("official_success"), row["run_id"], result.get("session_id")
    missing = None if success is not None else result.get("error_code") or row.get("reason") or row["status"]
    wire = result.get("first_worker_request_sha256")
    require(profile["name"] == scope["plan_id"] and profile["registration_sha256"] == registration
            and profile["case"] == case["task"] and profile["stage"] == design["stage"] == row["phase"] == "solve"
            and Decimal(str(profile["guards"]["max_usd"])) == grant and ctl["implementation"] == scope["implementation"]
            and row["task_id"] == row["source_task_id"] == case["task_id"] and row["arm"] == arm
            and row["index"] == binding["index"] and row["repetition"] == 1 and design["interrupted_readers"] == {arm: binding}
            and design["delivery_arms"][arm]["sources_sha256"] == binding["sources_sha256"]
            and (not wire or wire == scope["original_readers"][arm][0]["first_worker"])
            and (type(success) is bool) == (result.get("grading_status") == "graded") and (success is None or wire)
            and run not in seen and (not session or session not in seen), "Different native reader, grant, bank or grade")
    expected = dict(condition=arm, task=case["task_id"], attempt=intent["attempt"], parent_run=intent["parent_run"],
                    run_id=run, session_id=session, folder=str(shard.relative_to(workspace)), seal=cost["seal"],
                    log_sha256=result.get("log_sha256"), success=success, missing_reason=missing,
                    reported_usd=cost["reported"], held_usd=cost["held"], requests=cost["http"],
                    first_worker_sha256=wire, ended=close["ended_at"])
    require(all(receipt[k] == v for k, v in expected.items()), "Supplement receipt differs from its sealed reader")
    if missing == "HTTP_503":
        _verify_failure(shard, row, receipt["error"])
    else:
        require(receipt["error"] is None, "Unexpected provider failure receipt")
    if expected["log_sha256"] and missing != "HTTP_503":
        asset_bytes(safe_path(shard, row["directory"]), dict(path="log.jsonl", sha256=expected["log_sha256"]))
    seen.add(run)
    if session:
        seen.add(session)
    return Attempt(intent["attempt"], shard, row, cost, receipt, intent)


def reader_supplements(workspace, root, plan, record):
    """Return verified attempts and outstanding grants without modifying the original batch or any child."""
    workspace, root = Path(workspace), Path(root)
    folder = workspace / "runs" / plan["id"]
    supplements = []
    owners = set()
    registration = digest(canonical(record))
    for path in sorted(folder.glob("reader-batches/*/supplements/*")):
        directory = safe_path(folder, path.relative_to(folder))
        marker = directory / "registration.json"
        if not marker.is_file():
            require(directory.is_dir() and not any(p.is_dir() or p.name == "result.json" for p in directory.iterdir()),
                    "Supplement evidence has no registration")
            continue
        scope, allowance, case, collection, role = _authority(root, directory, plan, record)
        require(scope["batch_id"] not in owners, "Multiple supplements claim one reader allowance")
        owners.add(scope["batch_id"])
        latest, seen = _previous(folder, plan, scope, case, collection, role)
        checksum = digest(marker.read_bytes())
        final = safe_path(directory, "result.json")
        end = read(final) if final.is_file() else None
        attempts, refs, pending, pending_grant = [], [], None, Decimal(0)
        reported = held = Decimal(0)
        children = sorted(p for p in directory.iterdir() if p.is_dir())
        for i, child in enumerate(children, 1):
            require(child == safe_path(directory, f"attempt-{i:04}"), "Unexpected or noncontiguous recovery attempt")
            dispatch = safe_path(child, "dispatch.json")
            entries = {p.name for p in child.iterdir() if p.is_dir() or p.name in {"dispatch.json", "receipt.json"}}
            if "dispatch.json" not in entries:
                require(i == len(children) and not entries, "A native reader lost its dispatch")
                break
            intent = read(dispatch)
            arm = intent["condition"]
            require(arm in latest, "A recovery dispatch named an unauthorized condition")
            ordinal, previous, reason = latest[arm]
            available = (allowance - reported - held).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            require(reason == "HTTP_503" and intent["index"] == i and intent["registration_sha256"] == checksum
                    and intent["task"] == case["task_id"] and intent["parent_run"] == previous
                    and intent["attempt"] == ordinal + 1 and Decimal(intent["grant_usd"]) == available > 0,
                    "Supplement dispatch differs from its slot lineage or remaining allowance")
            shard = safe_path(child, f"task-{case['task']}/solve")
            receipt = safe_path(child, "receipt.json")
            if not receipt.is_file():
                require(i == len(children), "A later attempt has no settled predecessor")
                pending, pending_grant = shard, available
                break
            raw = receipt.read_bytes()
            view = read_json(raw)
            attempt = _settled(workspace, shard, intent, scope, registration, case, view, seen)
            attempts.append(attempt)
            refs.append(dict(path=str(receipt.relative_to(workspace)), sha256=digest(raw)))
            latest[arm] = (ordinal + 1, view["run_id"], view["missing_reason"])
            reported += Decimal(attempt.cost["reported"])
            held += Decimal(attempt.cost["held"])
        remaining = allowance - reported - held
        require(remaining >= pending_grant, "Recovered readers exceeded their budget")
        if end is not None:
            require(end["format"] == "agent-memory-reader-supplement-result/v1" and end["registration_sha256"] == checksum
                    and pending is None and len(refs) == len(children) and end["attempts"] == refs
                    and end["effective"] == {arm: state[1] for arm, state in latest.items()}
                    and end["requests"] == sum(a.cost["http"] for a in attempts)
                    and Decimal(end["reported_usd"]) == reported and Decimal(end["held_usd"]) == held
                    and all(state[2] != "HTTP_503" for state in latest.values())
                    and end["complete"] == all(state[2] is None for state in latest.values()),
                    "Supplement completion differs from its native receipts")
            remaining = Decimal(0)
        supplements.append(Supplement(scope["batch_id"], case["task"], attempts, pending, pending_grant, remaining,
                                      directory, scope, end))
    return supplements

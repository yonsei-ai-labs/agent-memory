"""Run declared stages in funded waves, sealing each shard's spending before the next wave is granted."""

from concurrent.futures import ProcessPoolExecutor
from decimal import ROUND_DOWN, Decimal
import fcntl
from multiprocessing import get_context
import os

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.records import fixed, read, sealed_rows, shard_seal


def shard_accounting(folder, grant, *, check_deferral=False):
    """Spending of one sealed shard: observed charges plus held reservations, never above its grant."""
    end, _, rows = sealed_rows(folder)
    known = sum((Decimal(v["reported_usd"]) for r in rows for v in r.get("result", {}).get("billing_by_role", {}).values()),
                Decimal(0))
    pending = Decimal(end.get("reserved_usd", "0"))
    unknown = any(r.get("result", {}).get("unknown_cost") and not r["result"].get("unpriced_reservation") for r in rows)
    total = Decimal(grant) if unknown else known + pending
    if total > Decimal(grant) or total < known:
        raise ContractError("Committed shard spending exceeds its allocated grant")
    entry = dict(reported=str(known), held=str(total - known), spent=str(total), http=end["http_calls"],
                 seal=shard_seal(folder))
    if check_deferral:
        funding_stops = {"spend_limit", "unknown_cost", "unknown_cost_stop"}
        entry["budget_deferred"] = end.get("stop_reason") in funding_stops or any(
            r.get("reason") in funding_stops
            or ((r.get("result") or {}).get("error_code") or "").lower().removeprefix("batch_") in funding_stops
            for r in rows
        )
    return entry


def _process_pool(size):
    return ProcessPoolExecutor(max_workers=size, mp_context=get_context("spawn"))


def verify_stage_entries(root, stages):
    for name, entry in stages.items():
        folder = safe_path(root, name)
        if entry.get("status") == "not_started":
            if folder.exists():
                raise ContractError("A checkpointed excluded shard has execution evidence")
        else:
            if not (folder / "completion.json").is_file():
                raise ContractError("A completed shard is missing from its stage checkpoint")
            sealed_rows(folder, entry["seal"])


def verify_stage_checkpoints(root, registration_sha):
    """A checkpointed job must never be sent again, even if its shard was removed."""
    for stage in ("source", "write"):
        checkpoint = root / f"through-{stage}.json"
        if not checkpoint.exists():
            continue
        saved = read(checkpoint)
        if saved["registration_sha256"] != registration_sha:
            raise ContractError("Stage checkpoint belongs to another study registration")
        verify_stage_entries(root, saved["stages"])


def run_stages(root, registration, groups, shard, *, total_usd_limit, max_concurrent, prior_covered_usd="0",
               concurrency_by_stage=None, admission=None, verify_sealed=None, record_unfunded_shards=True, pool_factory=None,
               progress=None, through_stage=None, register_continuation=None, shard_root=None, before_dispatch=None):
    """Run (label, stage, cases) groups in order. Groups may be produced lazily after earlier groups finish.

    ``shard`` is (function, common): the function receives (common, case, stage, grant) in a worker process.
    A shard folder that already holds a completion record is never sent again.
    A continuation registers its scope under this lock, after verifying the original records and before dispatch.
    A bounded batch can keep its controller here and its task shards at a separately leased shard_root.
    Its wave receipts bind each funded shard before the next grant can be opened.
    """
    function, common = shard
    detached = shard_root is not None
    shards = shard_root if detached else root
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        registration_path = root / "registration.json"
        if register_continuation is not None and not registration_path.is_file():
            raise ContractError("A continuation needs the existing study registration")
        fixed(registration_path, registration)
        limit = Decimal(str(total_usd_limit))
        total = Decimal(str(prior_covered_usd))
        seals = {}
        deferred = False
        registration_sha = digest(registration_path.read_bytes())
        verify_stage_checkpoints(root, registration_sha)
        completed = root / "completion.json"
        if detached and completed.is_file():
            saved = read(completed)
            if saved["registration_sha256"] != registration_sha:
                raise ContractError("Batch completion belongs to a different registration")
            verify_stage_entries(shards, saved["stages"])
        if register_continuation is not None:
            register_continuation()
        with (pool_factory or _process_pool)(max_concurrent) as pool:
            for label, stage, ordered in groups:
                width = (concurrency_by_stage or {}).get(stage, max_concurrent)
                for begin in range(0, len(ordered), width):
                    wave = ordered[begin:begin + width]
                    grant = ((limit - total) / len(wave)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
                    if grant < 0 or (grant == 0 and not record_unfunded_shards):
                        raise ContractError("Study budget exhausted")
                    wave_id = f"{label}-wave-{begin // width + 1}"
                    grant_path = root / f"{wave_id}.json"
                    wave_names = ["task-" + c["task"] + "/" + stage for c in wave]
                    if detached:
                        receipt = root / f"{wave_id}.settled.json"
                        if not grant_path.exists() and (receipt.exists() or any((shards / n).exists() for n in wave_names)):
                            raise ContractError("Existing reader evidence has no grant from this batch")
                        if receipt.is_file():
                            saved = read(receipt)
                            if saved["registration_sha256"] != registration_sha or set(saved["stages"]) != set(wave_names):
                                raise ContractError("The settled wave differs from the registered assignments")
                            verify_stage_entries(shards, saved["stages"])
                        elif completed.is_file():
                            raise ContractError("A completed batch lost its settled wave receipt")
                    fixed(grant_path,
                          dict(tasks=[c["task"] for c in wave], per_shard_grant=str(grant), prior_covered=str(total)))
                    admitted = []
                    for case in wave:
                        name = "task-" + case["task"] + "/" + stage
                        reason = "spend_limit" if grant == 0 else (admission(shards, stage, case) if admission else None)
                        if reason is not None:
                            deferred |= reason == "spend_limit"
                            if (shards / name).exists():
                                raise ContractError("An excluded or unfunded shard already has an execution directory")
                            seals[name] = dict(status="not_started", reason=reason, reported="0", held="0", spent="0",
                                               http=0)
                            if detached and "interrupted_readers" in case:
                                seals[name]["arms"] = list(case["interrupted_readers"])
                        elif not (shards / name / "completion.json").is_file():
                            admitted.append(case)
                        elif verify_sealed is not None:
                            verify_sealed(case, stage, str(grant))
                    if detached and receipt.exists() and admitted:
                        raise ContractError("A previously settled reader wave would submit a new request")
                    if admitted and before_dispatch is not None:
                        before_dispatch()
                    list(pool.map(function, [(common, case, stage, str(grant)) for case in admitted]))
                    for case in wave:
                        name = "task-" + case["task"] + "/" + stage
                        if name in seals and seals[name].get("status") == "not_started":
                            continue
                        entry = shard_accounting(shards / name, grant, check_deferral=detached or through_stage is not None)
                        deferred |= entry.pop("budget_deferred", False)
                        total += Decimal(entry["spent"])
                        seals[name] = entry
                    if detached:
                        fixed(receipt, dict(registration_sha256=registration_sha, stages={n: seals[n] for n in wave_names}))
                    if progress:
                        progress(label, begin + len(wave), len(ordered), total)
                if stage in {"source", "write"}:
                    checkpoint = root / f"through-{stage}.json"
                    partial = dict(registration_sha256=registration_sha, stages=seals, total_covered_usd=str(total))
                    if checkpoint.exists() or (through_stage == stage and not deferred and not (root / "completion.json").exists()):
                        fixed(checkpoint, partial)
                    if through_stage is not None and (deferred or stage == through_stage):
                        return dict(partial, completed_through=None if deferred else stage, budget_deferred=deferred)
        if through_stage is not None:
            raise ContractError("Requested cohort stage was absent from the registered schedule")
        completion = dict(registration_sha256=registration_sha, stages=seals,
                          total_covered_usd=str(total))
        if detached:
            completion["budget_deferred"] = deferred
        fixed(root / "completion.json", completion)
        return completion
    finally:
        os.close(descriptor)

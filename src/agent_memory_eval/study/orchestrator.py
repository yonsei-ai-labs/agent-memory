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
        entry["budget_deferred"] = any(
            r.get("reason") in funding_stops
            or ((r.get("result") or {}).get("error_code") or "").lower().removeprefix("batch_") in funding_stops
            for r in rows
        )
    return entry


def _process_pool(size):
    return ProcessPoolExecutor(max_workers=size, mp_context=get_context("spawn"))


def verify_stage_checkpoints(root, registration_sha):
    """A checkpointed job must never be sent again, even if its shard was removed."""
    for stage in ("source", "write"):
        checkpoint = root / f"through-{stage}.json"
        if not checkpoint.exists():
            continue
        saved = read(checkpoint)
        if saved["registration_sha256"] != registration_sha:
            raise ContractError("Stage checkpoint belongs to another study registration")
        for name, entry in saved["stages"].items():
            folder = safe_path(root, name)
            if entry.get("status") == "not_started":
                if folder.exists():
                    raise ContractError("A checkpointed excluded shard has execution evidence")
            else:
                if not (folder / "completion.json").is_file():
                    raise ContractError("A completed shard is missing from its stage checkpoint")
                sealed_rows(folder, entry["seal"])


def run_stages(root, registration, groups, shard, *, total_usd_limit, max_concurrent, prior_covered_usd="0",
               concurrency_by_stage=None, admission=None, verify_sealed=None, record_unfunded_shards=True, pool_factory=None,
               progress=None, through_stage=None):
    """Run (label, stage, cases) groups in order. Groups may be produced lazily after earlier groups finish.

    ``shard`` is (function, common): the function receives (common, case, stage, grant) in a worker process.
    A shard folder that already holds a completion record is never sent again.
    """
    function, common = shard
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fixed(root / "registration.json", registration)
        limit = Decimal(str(total_usd_limit))
        total = Decimal(str(prior_covered_usd))
        seals = {}
        deferred = False
        registration_sha = digest((root / "registration.json").read_bytes())
        verify_stage_checkpoints(root, registration_sha)
        with (pool_factory or _process_pool)(max_concurrent) as pool:
            for label, stage, ordered in groups:
                width = (concurrency_by_stage or {}).get(stage, max_concurrent)
                for begin in range(0, len(ordered), width):
                    wave = ordered[begin:begin + width]
                    grant = ((limit - total) / len(wave)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
                    if grant < 0 or (grant == 0 and not record_unfunded_shards):
                        raise ContractError("Study budget exhausted")
                    fixed(root / f"{label}-wave-{begin // width + 1}.json",
                          dict(tasks=[c["task"] for c in wave], per_shard_grant=str(grant), prior_covered=str(total)))
                    admitted = []
                    for case in wave:
                        name = "task-" + case["task"] + "/" + stage
                        reason = "spend_limit" if grant == 0 else (admission(root, stage, case) if admission else None)
                        if reason is not None:
                            deferred |= reason == "spend_limit"
                            if (root / name).exists():
                                raise ContractError("An excluded or unfunded shard already has an execution directory")
                            seals[name] = dict(status="not_started", reason=reason, reported="0", held="0", spent="0",
                                               http=0)
                        elif not (root / name / "completion.json").is_file():
                            admitted.append(case)
                        elif verify_sealed is not None:
                            verify_sealed(case, stage, str(grant))
                    list(pool.map(function, [(common, case, stage, str(grant)) for case in admitted]))
                    for case in wave:
                        name = "task-" + case["task"] + "/" + stage
                        if name in seals and seals[name].get("status") == "not_started":
                            continue
                        entry = shard_accounting(root / name, grant, check_deferral=through_stage is not None)
                        deferred |= entry.pop("budget_deferred", False)
                        total += Decimal(entry["spent"])
                        seals[name] = entry
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
        fixed(root / "completion.json", completion)
        return completion
    finally:
        os.close(descriptor)

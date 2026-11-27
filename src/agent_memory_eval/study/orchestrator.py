"""Run declared stages in funded waves, sealing each shard's spending before the next wave is granted."""

from concurrent.futures import ProcessPoolExecutor
from decimal import ROUND_DOWN, Decimal
import fcntl
from multiprocessing import get_context
import os

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.study.records import fixed, sealed_rows, shard_seal


def shard_accounting(folder, grant):
    """Spending of one sealed shard: observed charges plus held reservations, never above its grant."""
    end, _, rows = sealed_rows(folder)
    known = sum((Decimal(v["reported_usd"]) for r in rows for v in r.get("result", {}).get("billing_by_role", {}).values()),
                Decimal(0))
    pending = Decimal(end.get("reserved_usd", "0"))
    unknown = any(r.get("result", {}).get("unknown_cost") and not r["result"].get("unpriced_reservation") for r in rows)
    total = Decimal(grant) if unknown else known + pending
    if total > Decimal(grant) or total < known:
        raise ContractError("Committed shard spending exceeds its allocated grant")
    return dict(reported=str(known), held=str(total - known), spent=str(total), http=end["http_calls"],
                seal=shard_seal(folder))


def _process_pool(size):
    return ProcessPoolExecutor(max_workers=size, mp_context=get_context("spawn"))


def run_stages(root, registration, groups, shard, *, total_usd_limit, max_concurrent, prior_covered_usd="0",
               concurrency_by_stage=None, admission=None, record_unfunded_shards=True, pool_factory=None,
               progress=None):
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
                            if (root / name).exists():
                                raise ContractError("An excluded or unfunded shard already has an execution directory")
                            seals[name] = dict(status="not_started", reason=reason, reported="0", held="0", spent="0",
                                               http=0)
                        elif not (root / name / "completion.json").is_file():
                            admitted.append(case)
                    list(pool.map(function, [(common, case, stage, str(grant)) for case in admitted]))
                    for case in wave:
                        name = "task-" + case["task"] + "/" + stage
                        if name in seals and seals[name].get("status") == "not_started":
                            continue
                        entry = shard_accounting(root / name, grant)
                        total += Decimal(entry["spent"])
                        seals[name] = entry
                    if progress:
                        progress(label, begin + len(wave), len(ordered), total)
        completion = dict(registration_sha256=digest((root / "registration.json").read_bytes()), stages=seals,
                          total_covered_usd=str(total))
        fixed(root / "completion.json", completion)
        return completion
    finally:
        os.close(descriptor)

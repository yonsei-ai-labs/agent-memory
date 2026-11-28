"""Continue a source-only cohort with its released first passes and a separate writer-only grant."""

import csv
from decimal import Decimal
from io import StringIO

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.assets import asset_bytes, asset_text, publish_json
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.cohort import is_cohort
from agent_memory_eval.study.followup_budget import approval_amount
from agent_memory_eval.study.plan import plan_digest
from agent_memory_eval.study.records import read, sealed_rows, source_from


def file_ref(root, path):
    relative = path.resolve().relative_to(root.resolve())
    return dict(path=str(relative), sha256=digest(safe_path(root, relative).read_bytes()))


def source_covered(first, added):
    keys = ("reported_usd", "held_usd")
    return sum(Decimal(first["cost"][key]) + Decimal((added or {}).get("added_" + key, "0")) for key in keys)


def writer_sources(plan, workspace, root, release_ref):
    release = read_json(asset_bytes(root, release_ref))
    require(release["format"] == "agent-memory-cohort-writer-sources/v1" and is_cohort(plan)
            and plan["first_pass"]["draws"] == 1, "Expected a release of single-draw own/mixed first passes")
    summary = read_json(asset_bytes(root, release["first_pass_report"]))
    supplement = read_json(asset_bytes(root, release["supplement"]))
    require(supplement["parent_summary_sha256"] == release["first_pass_report"]["sha256"],
            "Supplement belongs to a different first-pass release")
    domains = [key for key, item in summary["benchmarks"].items() if item["plan"] == plan["id"]]
    require(len(domains) == 1, "This plan must appear exactly once in the first-pass release")
    domain, = domains
    first = summary["benchmarks"][domain]
    folder = workspace / "runs" / plan["id"]
    registration = read_json(asset_bytes(folder, {"path": "registration.json", "sha256": first["registration_sha256"]}))
    checkpoint = read_json(asset_bytes(folder, {"path": "through-source.json", "sha256": first["source_checkpoint_sha256"]}))
    record = registration["preflight"]
    require(plan_digest(plan) == first["plan_sha256"] == record["plan_sha256"]
            and registration["approval"]["through_stage"] == "source"
            and registration["approval"]["resolved_sha256"] == digest(canonical(record)),
            "Writer inputs require the sealed source-only registration of this plan")
    added = supplement["by_benchmark"].get(domain)
    index = {}
    recovery_checkpoints = []
    if added:
        indexed = read_json(asset_bytes(workspace, dict(path=added["effective_index"], sha256=added["effective_index_sha256"])))
        require(indexed["parent_index_sha256"] == first["effective_sources_sha256"]
                and indexed["checkpoint_sha256"] == added["source_checkpoint_sha256"],
                "The effective source index has a different recovery history")
        previous = read_json(asset_bytes(folder, dict(path="source-http503-recovery/effective-sources.json",
                                                      sha256=indexed["parent_index_sha256"])))
        require(previous["original_checkpoint_sha256"] == first["source_checkpoint_sha256"],
                "Recovery did not start at the registered source checkpoint")
        recovery_checkpoints = [dict(path=p["directory"] + "/through-source.json", sha256=p["checkpoint_sha256"])
                                for p in previous["passes"]]
        last_checkpoint = safe_path(workspace, added["effective_index"]).with_name("through-source.json")
        recovery_checkpoints.append(dict(path=str(last_checkpoint.relative_to(workspace)), sha256=indexed["checkpoint_sha256"]))
        for checkpoint_ref in recovery_checkpoints:
            asset_bytes(workspace, checkpoint_ref)
        index = {row["task"]: row for row in indexed["sources"]}
        require(len(index) == len(indexed["sources"]) and set(index) == set(plan["benchmark"]["tasks"]),
                "Effective index must cover each registered first pass exactly once")
    raw_rows = csv.DictReader(StringIO(asset_text(root, release["writer_inputs"])))
    rows = [r for r in raw_rows if r["benchmark"] == domain]
    tasks = set(plan["benchmark"]["tasks"])
    require(len(rows) == len(tasks) == record["assigned"]["source"]
            and {r["task"] for r in rows} == tasks and len({r["final_run"] for r in rows}) == len(rows),
            "Every registered task must have one distinct final first pass")
    bfcl = record["adapter"] == "bfcl-relevance"
    case_ids = {} if bfcl else {c["task_id"]: c["task"] for c in record["cases"] if "source" in c["scheduled_stages"]}
    sources = {}
    for r in rows:
        task = r["task"]
        success = {"success": True, "failure": False, "missing": None}[r["final_result"]]
        entry = dict(run_id=r["final_run"], success=success, seal=r["source_seal_sha256"],
                     public_context_sha256=r["public_writer_context_sha256"] or None)
        require((entry["public_context_sha256"] is None) == (success is None),
                "Only graded sources can provide a public writer context")
        if bfcl:
            sources[task] = entry
            continue
        case = case_ids[task]
        name = f"task-{case}/source"
        if index:
            selection = index[task]
            require(all(selection[k] == entry[k] for k in ("seal", "run_id", "success"))
                    and selection.get("public_context_sha256", entry["public_context_sha256"]) == entry["public_context_sha256"],
                    "Effective source index and published writer inputs disagree")
            path = safe_path(workspace, selection["folder"])
            require(path.is_relative_to(workspace / "runs"), "First-pass shard is outside the recordings")
        else:
            require(checkpoint["stages"][name]["seal"] == entry["seal"], "Writer input differs from the source checkpoint")
            path = folder / name
        entry["folder"] = str(path.relative_to(workspace))
        sources[case] = entry
    forms = list(plan["writing"]["representations"])
    assignments = len(sources) * len(forms)
    require(assignments == record["assigned"]["write"], "Writer schedule differs from the original plan")
    meta = dict(plan_id=plan["id"], plan_sha256=first["plan_sha256"], source_registration_sha256=first["registration_sha256"],
                source_checkpoint_sha256=first["source_checkpoint_sha256"], source_release=release_ref,
                source_recovery_checkpoints=recovery_checkpoints, effective_sources_sha256=digest(canonical(sources)),
                registered_sources=len(rows),
                registered_writers=assignments, eligible_writers=sum(v["success"] is not None for v in sources.values()) * len(forms),
                prior_source_covered_usd=str(source_covered(first, added)))
    return registration, sources, meta


def active_writer_sources(workspace, folder, plan, *, root=ROOT):
    marker = read(folder / "write-continuation.json")
    _, sources, meta = writer_sources(plan, workspace, root, marker["source_release"])
    approval_ref, cap = writer_grant(root, marker["source_release"], plan, meta)
    require(marker["format"] == "agent-memory-cohort-writer-continuation/v1" and marker["stage"] == "write"
            and marker["approval_ref"] == approval_ref and marker["max_usd"] == str(cap)
            and all(marker.get(k) == v for k, v in meta.items()), "Changed writer approval or first-pass binding")
    return sources, meta


def effective_tau_sources(workspace, folder, plan):
    return active_writer_sources(workspace, folder, plan)[0] if (folder / "write-continuation.json").is_file() else {}


def verify_tau_sources(workspace, record, sources):
    registration_sha = digest(canonical(record))
    for case in record["cases"]:
        if "source" not in case["scheduled_stages"]:
            continue
        entry = sources[case["task"]]
        folder = safe_path(workspace, entry["folder"])
        _, control, (row,) = sealed_rows(folder, entry["seal"])
        require(control["profile"]["registration_sha256"] == registration_sha
                and control["profile"]["case"] == case["task"] and row["run_id"] == entry["run_id"]
                and row["arm"] == "no_memory" and row["task_id"] == case["task_id"],
                "A selected first pass belongs to another allocation")
        source = source_from(folder, case["task_id"])
        require((source is None) == (entry["success"] is None) and (source is None or
                (source.source_run_id, source.official_success, source.public_context_sha256) ==
                (entry["run_id"], entry["success"], entry["public_context_sha256"])),
                "Released writer inputs differ from the sealed experience or outcome")


def grant_path(root, release_ref):
    release = read_json(asset_bytes(root, release_ref))
    require(release["format"] == "agent-memory-cohort-writer-sources/v1", "Unknown writer release")
    return safe_path(root / "artifacts" / "approvals", release["id"] + ".json")


def approved_caps(root, release_ref, grant):
    require(grant.get("format") == "agent-memory-cohort-writer-approval/v1" and grant["stage"] == "write"
            and grant["source_release"] == release_ref and isinstance(grant["user_authorization"], str)
            and grant["user_authorization"].strip(), "Explicit writer-only approval for this release is required")
    release = read_json(asset_bytes(root, release_ref))
    summary = read_json(asset_bytes(root, release["first_pass_report"]))
    supplement = read_json(asset_bytes(root, release["supplement"]))
    caps = {key: approval_amount(value) for key, value in grant["plan_caps_usd"].items()}
    require(set(caps) == {v["plan"] for v in summary["benchmarks"].values()}
            and sum(caps.values()) <= approval_amount(grant["max_usd"]), "Writer sublimits exceed the pooled approval")
    for domain, first in summary["benchmarks"].items():
        prior = source_covered(first, supplement["by_benchmark"].get(domain))
        require(caps[first["plan"]] + prior <= Decimal(first["cap_usd"]), "A writer sublimit exceeds its study ceiling")
    return caps


def authorize_writers(root, release_path, max_usd, quote, per_plan):
    release_ref = file_ref(root, release_path)
    caps = dict(item.split("=", 1) for item in per_plan)
    require(len(caps) == len(per_plan), "Supply one writer sublimit per plan")
    grant = dict(format="agent-memory-cohort-writer-approval/v1", stage="write", source_release=release_ref,
                 max_usd=max_usd, plan_caps_usd=caps, user_authorization=quote)
    approved_caps(root, release_ref, grant)
    path = grant_path(root, release_ref)
    try:
        publish_json(path, grant)
    except FileExistsError:
        require(read(path) == grant, "A different writer grant already exists for this release")
    return dict(approval=str(path.relative_to(root)), max_usd=max_usd, plan_caps_usd=caps)


def writer_grant(root, release_ref, plan, meta):
    path = grant_path(root, release_ref)
    require(path.is_file(), "This source release needs a separate writer-only spending approval")
    approval_ref = file_ref(root, path)
    cap = approved_caps(root, release_ref, read_json(asset_bytes(root, approval_ref)))[plan["id"]]
    require(cap + Decimal(meta["prior_source_covered_usd"]) <= Decimal(plan["budget"]["max_usd"]),
            "First-pass spending and writer sublimit exceed the registered plan ceiling")
    return approval_ref, cap


def writer_continuation(root, plan, meta):
    from agent_memory_eval.reproduction.freeze import implementation_identity
    approval_ref, cap = writer_grant(root, meta["source_release"], plan, meta)
    return dict(format="agent-memory-cohort-writer-continuation/v1", stage="write", **meta,
                approval_ref=approval_ref, max_usd=str(cap), implementation=implementation_identity(root))


def verify_continuation(plan, workspace, root, continuation, registration, sources):
    original, released, meta = writer_sources(plan, workspace, root, continuation["source_release"])
    require((registration, sources) == (original, released) and continuation == writer_continuation(root, plan, meta),
            "Writer scope differs from the released sources, authorized cap or frozen implementation")
    return Decimal(continuation["max_usd"])


def writer_command(plan, workspace, root, adapter, release_path, *, execute, progress=None, checkout=None):
    release_ref = file_ref(root, release_path)
    registration, sources, meta = writer_sources(plan, workspace, root, release_ref)
    continuation = writer_continuation(root, plan, meta) if execute else None
    bfcl = plan["benchmark"]["adapter"] == "bfcl-relevance"
    resolved = adapter.resolve(plan, workspace, root, **(dict(checkout=checkout) if bfcl else {}))
    require(resolved["record"] == registration["preflight"], "Resolved writer schedule changed since the first pass")
    if not execute:
        if bfcl:
            from agent_memory_eval.study.adapters.bfcl_transfer import verify_writer_sources
            verify_writer_sources(plan, workspace / "runs" / plan["id"], resolved, sources)
        else:
            verify_tau_sources(workspace, resolved["record"], sources)
        return meta
    kwargs = dict(resolved=resolved, registration=registration, continuation=continuation, sources=sources, progress=progress)
    if bfcl:
        return adapter.run_writers(plan, workspace, root, **kwargs)
    from agent_memory_eval.study.adapters.tau_writer_stage import run_writers
    return run_writers(adapter, plan, workspace, root, **kwargs)

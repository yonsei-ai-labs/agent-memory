"""Run an approved set of readers from the completed writing stage of an own/mixed study."""

import csv
import re
import subprocess
from decimal import Decimal
from io import StringIO
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.batch_recovery import stage_lease
from agent_memory_eval.recording.assets import asset_bytes, asset_text, publish_json
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.study.cohort import is_cohort, pool_sources
from agent_memory_eval.study.followup_budget import approval_amount
from agent_memory_eval.study.plan import conditions, plan_digest
from agent_memory_eval.study.records import read
from agent_memory_eval.study.registration_cost import registration_cost
from agent_memory_eval.study.writer_stage import active_writer_sources, file_ref


RUNTIME_PATHS = ("src", "scripts", "schemas", "pyproject.toml", "uv.lock")


def _publish(path, value):
    try:
        publish_json(path, value)
    except FileExistsError:
        require(read(path) == value, f"A different {path.name} is already sealed")


def _spec(root, path):
    ref = file_ref(root, path)
    spec = read_json(asset_bytes(root, ref))
    require(spec["format"] == "agent-memory-cohort-reader-batch/v1"
            and re.fullmatch(r"[a-z0-9][a-z0-9-]*", spec["id"]) and spec["plans"],
            "Expected a named cohort reader batch")
    limit = approval_amount(spec["max_usd"])
    total = Decimal(0)
    if "runtime_commit" in spec:
        require(re.fullmatch(r"[0-9a-f]{40}", spec["runtime_commit"]) is not None, "Expected a frozen reader commit")
    for name, part in spec["plans"].items():
        plan = read_json(asset_bytes(root, part["plan"]))
        tasks = part["tasks"]
        require(name == plan["id"] and is_cohort(plan) and isinstance(tasks, list) and tasks
                and len(tasks) == len(set(tasks)) and set(tasks) <= set(plan["benchmark"]["tasks"]),
                "Reader tasks must be a nonempty, unique subset of their original cohort")
        total += approval_amount(part["max_usd"])
        if "operations" in part:
            ops = part["operations"]
            require("runtime_commit" in spec and set(ops) == {"max_concurrent_tasks", "transient_backoff_seconds"}
                    and type(ops["max_concurrent_tasks"]) is int and ops["max_concurrent_tasks"] >= 1
                    and (plan["benchmark"]["adapter"] != "bfcl-relevance" or ops["max_concurrent_tasks"] == 1),
                    "Invalid reader concurrency or unfrozen operations")
            waits = ops["transient_backoff_seconds"]
            require(isinstance(waits, list) and waits and all(type(w) is int and w > 0 for w in waits)
                    and waits == sorted(waits), "Register positive nondecreasing provider backoffs")
        if "closed_reader_batches" in part:
            require("runtime_commit" in spec and isinstance(part["closed_reader_batches"], dict)
                    and all(re.fullmatch(r"[a-z0-9][a-z0-9-]*", k) and k != spec["id"]
                            for k in part["closed_reader_batches"]), "Invalid prior reader batch registry")
    require(total <= limit, "Reader plan limits exceed the batch ceiling")
    if "action_review" in spec:
        asset_bytes(root, spec["action_review"])
    return ref, spec


def _approval(spec, ref, quote):
    require(isinstance(quote, str) and quote.strip(), "A reader batch needs its spending authorization")
    return dict(format="agent-memory-cohort-reader-approval/v1", stage="solve", batch_ref=ref,
                max_usd=spec["max_usd"], plan_caps_usd={k: v["max_usd"] for k, v in spec["plans"].items()},
                user_authorization=quote)


def authorize_readers(root, path, max_usd, quote, plan_id):
    ref, spec = _spec(root, path)
    require(plan_id in spec["plans"] and approval_amount(max_usd) == approval_amount(spec["max_usd"]),
            "Authorize the listed plans and the exact pooled reader limit")
    grant = _approval(spec, ref, quote)
    dest = safe_path(root / "artifacts/approvals", spec["id"] + ".json")
    _publish(dest, grant)
    return dict(approval=file_ref(root, dest), max_usd=grant["max_usd"])


def reader_input_digest(plan, tasks, lookup):
    """Compare the published index and the native writer snapshots using the same ordered fields."""
    views = {}
    for target in tasks:
        ordered = pool_sources(plan, target)
        views[target] = {form: [(t, lookup(t, form)) for t in ordered] for form in plan["writing"]["representations"]}
    return digest(canonical(views))


def _reader_inputs(plan, domain, tasks, sources, index):
    """Bind the stored versions and native opening, including unavailable banks."""
    writers = {}
    for row in index:
        if row["benchmark"] != domain:
            continue
        task, form = row["task"], row["form"]
        key = (task, form)
        source = sources[task if plan["benchmark"]["adapter"] == "bfcl-relevance" else task + "__source-1"]
        require(key not in writers and row["source_run"] == source["run_id"]
                and (row["public_context_sha256"] or None) == source["public_context_sha256"],
                "Released writer and final first-pass records differ")
        if row["status"] == "stored":
            require(source["success"] is not None and row["source_success"] == str(source["success"])
                    and all(row[k] for k in ("writer_run", "snapshot", "body_sha256", "title_sha256")),
                    "Stored writer is missing its source, version or outcome")
            writers[key] = {k: row[k] for k in ("source_run", "writer_run", "snapshot", "title_sha256", "body_sha256")}
        else:
            require(not row["writer_run"] and not row["snapshot"], "An unavailable writer has a stored version")
            writers[key] = None
    forms = plan["writing"]["representations"]
    require(set(writers) == {(t, f) for t in plan["benchmark"]["tasks"] for f in forms},
            "The writer release must account for every original task and form")
    eligible = assigned = 0
    for task in tasks:
        ordered = pool_sources(plan, task)
        for form, _ in conditions(plan):
            eligible += plan["repeats"] * (int(writers[task, form] is not None)
                                          + int(all(writers[t, form] is not None for t in ordered)))
            assigned += 2 * plan["repeats"]
    return dict(assigned_readers=assigned, eligible_readers=eligible, unavailable_readers=assigned - eligible,
                reader_inputs_sha256=reader_input_digest(plan, tasks, lambda task, form: writers[task, form]))


def _context(plan, workspace, root, adapter, path, checkout):
    ref, spec = _spec(root, path)
    require(plan["id"] in spec["plans"], "This plan has no targets in the reader batch")
    part = spec["plans"][plan["id"]]
    require(read_json(asset_bytes(root, part["plan"])) == plan, "Different reader plan")
    folder = workspace / "runs" / plan["id"]
    summary = read_json(asset_bytes(root, spec["writer_results"]))
    require(summary["format"] == "agent-memory-cohort-writer-results/v1", "Expected a released cohort writer report")
    bench = summary["per_benchmark"][part["benchmark"]]
    for name, key in (("through-write.json", "writer_checkpoint_sha256"),
                      ("write-continuation.json", "writer_scope_sha256")):
        asset_bytes(folder, dict(path=name, sha256=bench[key]))
    sources, meta = active_writer_sources(workspace, folder, plan, root=root)
    require(meta["source_release"]["sha256"] == bench["source_release_sha256"]
            and meta["effective_sources_sha256"] == bench["effective_sources_sha256"],
            "The reader and writer stages must use the same recovered first passes")
    registration = read(folder / "registration.json")
    native = dict(checkout=checkout) if plan["benchmark"]["adapter"] == "bfcl-relevance" else {}
    resolved = adapter.resolve(plan, workspace, root, **native)
    require(resolved["record"] == registration["preflight"], "The original study schedule changed")
    release_dir = safe_path(root, spec["writer_results"]["path"]).parent
    index = csv.DictReader(StringIO(asset_text(release_dir, summary["writer_outcomes"])))
    inputs = _reader_inputs(plan, part["benchmark"], part["tasks"], sources, index)
    covered = Decimal(meta["prior_source_covered_usd"]) + Decimal(bench["cost"]["reported_usd"]) + Decimal(bench["cost"]["held_usd"])
    scope = dict(format="agent-memory-cohort-reader-continuation/v1", stage="solve", id=spec["id"],
                 batch_ref=ref, plan_id=plan["id"], plan_sha256=plan_digest(plan), tasks=part["tasks"],
                 max_usd=part["max_usd"], prior_source_writer_covered_usd=str(covered),
                 cumulative_max_usd=part.get("cumulative_max_usd", plan["budget"]["max_usd"]),
                 source_registration_sha256=meta["source_registration_sha256"],
                 effective_sources_sha256=meta["effective_sources_sha256"], writer_results=spec["writer_results"],
                 writer_checkpoint_sha256=bench["writer_checkpoint_sha256"], **inputs)
    if "runtime_commit" in spec:
        scope["runtime_commit"] = spec["runtime_commit"]
    for key in ("operations", "closed_reader_batches"):
        if key in part:
            scope[key] = part[key]
    return spec, scope, registration, resolved, sources


def _check_runtime(root, old, new):
    require(subprocess.run(["git", "-C", root, "diff", "--quiet", old, new, "--", *RUNTIME_PATHS]).returncode == 0,
            "Resume with the frozen reader implementation")


def _compatible_code(root, previous, current):
    if previous == current:
        return
    old, new = previous["implementation_commit"], current["implementation_commit"]
    require(previous["lock_ref"] == current["lock_ref"], "Resume with the frozen reader implementation")
    _check_runtime(root, old, new)


def _prior_scope(root, plan, other):
    spec = read_json(asset_bytes(root, other["batch_ref"]))
    part = spec["plans"][plan["id"]]
    grant = read_json(asset_bytes(root, other["approval_ref"]))
    require(spec["id"] == other["id"] and spec["writer_results"] == other["writer_results"]
            and part["tasks"] == other["tasks"] and part["max_usd"] == other["max_usd"]
            and other.get("runtime_commit") == spec.get("runtime_commit")
            and all(other.get(key) == part.get(key) for key in ("operations", "closed_reader_batches"))
            and other["cumulative_max_usd"] == part.get("cumulative_max_usd", plan["budget"]["max_usd"])
            and grant == _approval(spec, other["batch_ref"], grant["user_authorization"]),
            "Prior reader scope differs from its original batch or authorization")


def _closed_predecessor(folder, refs, other, scope):
    """Bind a disjoint predecessor and every supplemental result before starting a new frozen runtime."""
    require(set(refs) == {"registration_sha256", "result_sha256", "supplements"}, "Invalid predecessor receipt")
    asset_bytes(folder, dict(path="registration.json", sha256=refs["registration_sha256"]))
    result = read_json(asset_bytes(folder, dict(path="result.json", sha256=refs["result_sha256"])))
    require(result["complete"] and not result["budget_deferred"] and result["settled_readers"] == other["eligible_readers"]
            and all(other[k] == scope[k] for k in ("plan_sha256", "writer_checkpoint_sha256",
                        "source_registration_sha256", "effective_sources_sha256", "writer_results")),
            "Predecessor has open work or different original sources")
    supplements = {p.parent.name for p in folder.glob("supplements/*/registration.json")}
    require(supplements == set(refs["supplements"]), "Predecessor supplement set changed")
    for name in supplements:
        prefix = f"supplements/{name}/"
        receipt = read_json(asset_bytes(folder, dict(path=prefix + "result.json", sha256=refs["supplements"][name])))
        asset_bytes(folder, dict(path=prefix + "registration.json", sha256=receipt["registration_sha256"]))
        require(receipt["complete"], "Predecessor supplement is still open")


def _register(root, workspace, folder, plan, directory, scope, identity, *, publish=True):
    """Allocate disjoint native slots under the controller lock, before the first request."""
    previous = []
    pins = scope.get("closed_reader_batches", {})
    seen_pins = set()
    marker_path = directory / "registration.json"
    if "runtime_commit" in scope:
        _check_runtime(root, scope["runtime_commit"], identity["implementation_commit"])
    for marker in sorted(directory.parent.glob("*/registration.json")):
        other = read(marker)
        require(other["id"] == marker.parent.name and other["plan_sha256"] == scope["plan_sha256"]
                and other["writer_results"] == scope["writer_results"], "Different study in the reader batch registry")
        if marker.parent == directory:
            _compatible_code(root, other["implementation"], identity)
            continue
        _prior_scope(root, plan, other)
        require(not set(other["tasks"]).intersection(scope["tasks"]), "Reader batches overlap an original target")
        if other["id"] in pins:
            _closed_predecessor(marker.parent, pins[other["id"]], other, scope)
            seen_pins.add(other["id"])
        elif not marker_path.is_file():
            _compatible_code(root, other["implementation"], identity)
        previous.append((other, marker.parent))
    require(seen_pins == set(pins), "Missing a registered predecessor")
    marker = marker_path
    if marker.is_file():
        saved = read(marker)
        require(saved == dict(scope, implementation=saved["implementation"]), "The registered reader batch changed")
        return saved
    require(not directory.exists() or not any(directory.iterdir()), "Reader evidence exists without a registered scope")
    cost = registration_cost(plan, workspace, root=root)
    solve = cost["stages"]["solve"]
    previous_spent = Decimal(solve["reported_usd"]) + Decimal(solve["held_usd"])
    if not previous:
        require(not solve["requests"] and not previous_spent and not cost.get("unsealed_shards"),
                "Unregistered reader work already exists")
    else:
        settled, open_limits = Decimal(0), Decimal(0)
        for other, location in previous:
            receipt = location / "result.json"
            if receipt.is_file():
                final = read(receipt)
                charge = Decimal(final["reported_usd"]) + Decimal(final["held_usd"])
                require(final["complete"] and not final["budget_deferred"] and
                        final["settled_readers"] == other["eligible_readers"] and 0 <= charge <= Decimal(other["max_usd"]),
                        "Invalid prior reader batch receipt")
                settled += charge
                extra = cost.get("reader_supplements", {}).get(other["id"])
                if extra:
                    sealed = Decimal(extra["reported_usd"]) + Decimal(extra["held_usd"]) - Decimal(extra["live_grant_usd"])
                    remaining = Decimal(extra["remaining_authorized_usd"])
                    require(charge + sealed + remaining <= Decimal(other["max_usd"]),
                            "Original and recovered readers exceed their shared batch ceiling")
                    settled += sealed
                    open_limits += remaining
            else:
                open_limits += Decimal(other["max_usd"])
        if open_limits:
            require(settled <= previous_spent <= settled + open_limits, "Previous batch expenses exceed their grants")
            previous_spent = settled + open_limits
        else:
            require(not cost.get("unsealed_shards") and settled == previous_spent,
                    "Sealed reader totals disagree with the native expense records")
    required = Decimal(scope["prior_source_writer_covered_usd"]) + previous_spent + Decimal(scope["max_usd"])
    require(required <= approval_amount(scope["cumulative_max_usd"]),
            "This batch exceeds its cumulative plan ceiling including prior spending and reservations")
    value = dict(scope, implementation=identity)
    if publish:
        directory.mkdir(parents=True, exist_ok=True)
        _publish(marker, value)
    return value


def reader_command(plan, workspace, root, adapter, path, *, execute=False, checkout=None, progress=None):
    workspace, root = Path(workspace).resolve(), Path(root).resolve()
    spec, scope, registration, resolved, sources = _context(plan, workspace, root, adapter, path, checkout)
    if not execute and "runtime_commit" not in scope:
        return scope
    approval_path = safe_path(root / "artifacts/approvals", spec["id"] + ".json")
    if execute or approval_path.is_file():
        grant = read(approval_path)
        require(grant == _approval(spec, scope["batch_ref"], grant["user_authorization"]), "Different reader authorization")
        scope["approval_ref"] = file_ref(root, approval_path)
    identity = implementation_identity(root)
    folder = workspace / "runs" / plan["id"]
    directory = safe_path(folder, "reader-batches/" + scope["id"])
    if not execute:
        _register(root, workspace, folder, plan, directory, scope, identity, publish=False)
        return scope
    if plan["benchmark"]["adapter"] == "bfcl-relevance":
        from agent_memory_eval.study.adapters.bfcl_reader_stage import run_readers
    else:
        from agent_memory_eval.study.adapters.tau_reader_stage import run_readers
    with stage_lease(folder):
        batch = _register(root, workspace, folder, plan, directory, scope, identity)
        result = run_readers(adapter, plan, workspace, root, resolved=resolved, registration=registration,
                             sources=sources, batch=batch, directory=directory, progress=progress)
        require(result["assigned_readers"] == scope["assigned_readers"] and result["eligible_readers"] == scope["eligible_readers"]
                and Decimal(result["reported_usd"]) + Decimal(result["held_usd"]) <= Decimal(scope["max_usd"]),
                "Reader receipt differs from its schedule or spending limit")
        if result["complete"]:
            require(not result["budget_deferred"] and result["settled_readers"] == result["eligible_readers"],
                    "Incomplete reader allocations cannot close the batch")
            _publish(directory / "result.json", result)
        return result

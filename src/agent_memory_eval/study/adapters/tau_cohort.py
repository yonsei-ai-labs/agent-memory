"""Bind a cohort reader to the immutable output of each scheduled first pass and writer."""

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.analysis.selection import worker_choices
from agent_memory_eval.contracts.core import MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import required_form_bank
from agent_memory_eval.experiments.forms_solve import resolve_retrieval_pool
from agent_memory_eval.experiments.matched_trial import role_settings, source_receipt
from agent_memory_eval.memory.context import memory_system_suffix, snapshot_message
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.memory.tools import memory_tools
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.runner.task_start import TaskStart
from agent_memory_eval.study.cohort_evidence import item_exposure
from agent_memory_eval.study.records import first_pass_shard, sealed_rows, source_from


def cohort_catalog(workspace, folder, cfg, cases, stages):
    """Resolve every writer once after the whole writing stage, including the unavailable slots."""
    from agent_memory_eval.study.adapters.tau_banking import checked_input_stop, study_design, study_jobs
    instruction = asset_text(workspace, cfg["writing"]["common_instruction_ref"]).rstrip("\r\n")
    catalog = {}
    runs, sessions, snapshots = set(), set(), set()
    for case in cases:
        if "donor" in case:
            continue
        tid = case["task_id"]
        require(tid not in catalog and case["scheduled_stages"] == ["source", "write"]
                and "imported_first_pass" not in case, "A cohort source must be a unique new first pass")
        prefix = "task-" + case["task"]
        banks, missing = {}, {}
        catalog[tid] = dict(banks=banks, missing=missing)
        source = None
        for stage in ("source", "write"):
            name = f"{prefix}/{stage}"
            shard = folder / name
            entry = stages.get(name, {})
            if stage == "source":
                shard, entry = first_pass_shard(workspace, folder, case, cfg.get("effective_sources"), entry=entry)
            if not (shard / "completion.json").is_file():
                missing.update(dict.fromkeys(case["representations"], stage + "_" + (entry.get("reason") or "not_started")))
                break
            close, ctl, rows = sealed_rows(shard, entry.get("seal"))
            jobs = study_jobs(case, stage)
            require(ctl["profile"]["registration_sha256"] == cfg["registration_sha256"]
                    and ctl["profile"]["case"] == case["task"] and ctl["profile"]["stage"] == stage
                    and ctl["design"] == study_design(cfg, case, stage, source) and len(rows) == len(jobs)
                    and all(all(r[k] == v for k, v in job.items()) for r, job in zip(rows, jobs)),
                    "A cohort source or writer belongs to another registered allocation")
            for row in rows:
                require(row["run_id"] not in runs, "A cohort source or writer run was reused")
                runs.add(row["run_id"])
                session = (row.get("result") or {}).get("session_id")
                if session is not None:
                    require(session not in sessions, "A cohort source or writer session was reused")
                    sessions.add(session)
            if stage == "source":
                row, = rows
                source = source_from(shard, tid, binding=entry)
                if source is None:
                    if cfg.get("effective_sources"):
                        checked_input_stop(shard, case, close, rows, ctl["profile"], ctl["design"], ctl["implementation"],
                                           ctl["model_catalog_entries"], expected_seal=entry["seal"])
                    reason = (row.get("result") or {}).get("error_code") or row.get("reason") or "ungraded"
                    missing.update(dict.fromkeys(case["representations"], "source_" + reason))
                    break
                source_receipt(safe_path(shard, row["directory"]), source,
                               dict(case, registration_sha256=cfg["registration_sha256"]))
            else:
                for row in rows:
                    form = row["arm"]
                    if row["status"] != "completed":
                        reason = (row.get("result") or {}).get("error_code") or row.get("reason") or row["status"]
                        missing[form] = "writer_" + reason
                        continue
                    bank = required_form_bank(shard, row, form, 1, source, cfg["writer_feedback_level"],
                                              instruction + "\n" + cfg["writing"]["lines"][form])
                    require(bank["snapshot_id"] not in snapshots, "Cohort writers shared a starting memory")
                    snapshots.add(bank["snapshot_id"])
                    banks[form] = dict(bank, journal_sha256=row["result"]["log_sha256"])
    return catalog


def bind_cohort_pools(design, plan, case, catalog):
    pools, unavailable = {}, {}
    for arm, delivery in design["delivery_arms"].items():
        form, tids = delivery["writer"], delivery["source_tasks"]
        missing = [f"{tid}:{catalog[tid]['missing'][form]}" for tid in tids if form in catalog[tid]["missing"]]
        if missing:
            require("interrupted_readers" not in case, "A previously dispatched cohort pool is unavailable for its retry")
            pools[arm] = None
            unavailable[arm] = "pool_source_unavailable:" + ",".join(missing)
        else:
            pool = pools[arm] = [catalog[tid]["banks"][form] for tid in tids]
            delivery["sources_sha256"] = digest(canonical(pool))
            if "interrupted_readers" in case:
                original = case["interrupted_readers"][arm]
                own, = [bank for bank in pool if bank["task_id"] == case["task_id"]]
                require((delivery["sources_sha256"], own["run_id"], own["snapshot_id"]) ==
                        (original["sources_sha256"], original["writer"], original["snapshot"]),
                        "A retry would receive different cohort sources")
    plan.update(retrieval_pools=pools, unavailable_pools=unavailable)


def verify_reader_pool(log, row, transfer):
    """Compare the sealed reader snapshot and first worker wire with the assigned writers and opening."""
    pool, delivery = transfer["pool"], transfer["delivery"]
    require(pool and row["source_task_id"] == row["task_id"], "Dispatched cohort reader has no verified source pool")
    own, = [b for b in pool if b["task_id"] == row["task_id"]]
    items, lineage = resolve_retrieval_pool(delivery, pool, row["task_id"], row["repetition"],
                                            own["public_context_sha256"], {delivery["writer"]})
    assignment = log.state["assignments"][log.run]
    policy = MemoryPolicy(**assignment["policy"])
    require(assignment["arm"] == row["arm"] and assignment["task_id"] == own["task_id"]
            and assignment["fixed_bank_delivery"] == delivery and policy.read_mode == delivery["read_mode"]
            and policy.write_timing == "none" and log.start["source_run_ids"] == [b["run_id"] for b in pool]
            and log.blobs.get(assignment["ordered_writer_sources_ref"]) == lineage and log.items == items
            and snapshot_items(log.blobs, log.state, row["result"]["snapshot_id"]) == items
            and assignment["input_items_sha256"] == digest(canonical(items)),
            "Reader's immutable cohort pool differs from its registered writer versions")
    starts = [e["payload"] for e in log.of_type("business.transition")]
    worker = next((r for r in log.requests if r["actor_role"] == "worker"), None)
    if worker is None:
        return
    control, = [e for e in starts if e.get("origin") == "matched_trial.native_control"]
    receipt = log.blobs.get(control["receipt_ref"])
    opening, = [e for e in starts if e.get("origin") == "task_start.first_decision"]
    require(opening["source_run_id"] == receipt["source_run_id"] == own["source_run_id"]
            and opening["source_log_sha256"] == receipt["source_log_sha256"] == own["source_log_sha256"]
            and receipt["registration_sha256"] == transfer["registration"]
            and control["control_ref"]["sha256"] == receipt["native_sha256"] == transfer["native_sha256"],
            "Reader native control or opening differs from the bank's own first pass")
    wire = role_settings(log.blobs.get(control["control_ref"]))["worker"]
    messages = read_json(TaskStart.from_record(log.blobs.get(opening["task_start_ref"])).public_input)["messages"]
    descriptions = transfer["descriptions"] if policy.read_mode == "on_demand" else None
    tools = memory_tools(policy.tools("solve"), read_descriptions=descriptions)
    if tools:
        wire["tools"] = [*wire.get("tools", []), *tools]
    if descriptions is None:
        messages[0]["content"] += memory_system_suffix(policy, transfer["instruction"])
    if policy.preloads:
        messages.insert(1, snapshot_message(items))
    require(canonical(dict(wire, messages=messages)) == canonical(log.blobs.get(worker["request_ref"])),
            "Recorded first worker request differs outside the assigned cohort memory and tool wording")


def reader_item_evidence(log, pool, decision):
    trace = worker_choices(log.folder, run_id=log.run, evidence=(log.scan, log.state, None))["choices"]
    items = [dict(source_task=b["task_id"], source_run=b["source_run_id"], writer_run=b["run_id"],
                  snapshot=b["snapshot_id"], item_id=i["id"], version_id=i["version"]) for b in pool for i in b["items"]]
    cutoff = decision.get("request")
    if cutoff is None and decision.get("correct") is not None:
        cutoff = next((s["request_id"] for s in reversed(trace) if s["status"] == "response"), None)
    evidence = item_exposure(items, trace, log.events.values(), cutoff, contents=log.items, blobs=log.blobs)
    steps = [dict(request=s["request_id"], status=s["status"], delivery=s["delivery"],
                  visible=[{k: f[k] for k in ("item_id", "version_id", "field")} for f in s["visible_memory"]],
                  reads=s["reads"], business_calls=[c["function"] for c in (s["response"] or {}).get("tool_calls", []) or []
                                                    if not c["function"]["name"].startswith("memory_")])
             for s in trace]
    return evidence, steps

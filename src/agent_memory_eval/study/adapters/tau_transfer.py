"""Transfer frozen τ² writer banks between native tasks, sharing only the target's public opening."""

from copy import deepcopy
from pathlib import Path

from agent_memory_eval.adapters.banking_memory_cohort import observation
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions, require
from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.experiments.forms_write import read_form_bank
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.study.adapters.tau_banking import Tau2Study, outcome_fields, unstarted_stages
from agent_memory_eval.study.plan import plan_digest
from agent_memory_eval.study.records import read, sealed_rows
from agent_memory_eval.study.transfer import allocation_rows, reader_arms


class TauTransfer(Tau2Study):
    cross_task = True

    def __init__(self, name):
        self.name = name

    def _configure(self, plan, workspace, root):
        setup, cfg, conn, policies, forms = super()._configure(plan, workspace, root)
        cfg["transfer_tools"] = {p["target"]: p["decision"]["tools"] for p in plan["transfer"]["pairs"]}
        return setup, cfg, conn, policies, forms

    def _reused(self, workspace, plan, name, case):
        if any(p["target"] == case["task_id"] for p in plan["transfer"]["pairs"]):
            return {}  # A target opening is collected independently of earlier scores.
        return super()._reused(workspace, plan, name, case)

    def resolve(self, plan, workspace, root):
        native_plan = deepcopy(plan)
        pinned = native_plan["first_pass"].pop("required_sources_sha256", None)
        resolved = super().resolve(native_plan, workspace, root)
        cfg, record = resolved["cfg"], resolved["record"]
        pairs = plan["transfer"]["pairs"]
        source_tasks = {p["source"] for p in pairs}
        cases = resolved["cases"]
        lookup = {(c["task_id"], c["source_draw"]): c for c in cases}
        donors = [c for c in cases if c["task_id"] in source_tasks]
        if pinned:
            require(all("imported_first_pass" in c for c in donors), "Missing a pinned transfer donor")
            identities = [
                [c["task"], c["imported_first_pass"]["run_id"], c["imported_first_pass"]["log_sha256"]]
                for c in donors
            ]
            require(digest(canonical(identities)) == pinned, "The transfer donors differ from the registered cohort")
        for case in cases:
            case["scheduled_stages"] = [
                s
                for s in case["scheduled_stages"]
                if s == "source" or s == "write" and case["task_id"] in source_tasks
            ]
        readers = []
        for pair in pairs:
            for draw in range(1, plan["first_pass"]["draws"] + 1):
                donor, opening = (lookup[pair[k], draw] for k in ("source", "target"))
                relation = pair["customer_relation"]
                same_customer = donor["customer"] == opening["customer"]
                require(
                    not any(c["customer"].startswith("unresolved-") for c in (donor, opening))
                    and same_customer == (relation == "same_customer"),
                    "Unverified transfer customer relationship",
                )
                contents, writers, modes = {}, {}, {}
                for name in donor["arms"]:
                    for bank, arm in zip(("real", "empty"), reader_arms(name)):
                        contents[arm] = bank
                        writers[arm] = donor["arm_writer"][name]
                        modes[arm] = donor["arm_mode"][name]
                        if name in cfg.get("query_tools_by_arm", {}):
                            cfg["query_tools_by_arm"][arm] = cfg["query_tools_by_arm"][name]
                readers.append(
                    dict(
                        opening,
                        task=f"{donor['task']}--to--{opening['task_id']}",
                        donor=donor,
                        opening=opening,
                        decision=pair["decision"],
                        customer_relation=relation,
                        arms=list(contents),
                        arm_writer=writers,
                        arm_mode=modes,
                        contents=contents,
                        scheduled_stages=cfg["solve_stages"],
                    )
                )
        cases.extend(readers)
        record.update(
            format="agent-memory-transfer-resolved/v1",
            plan_sha256=plan_digest(plan),
            assigned=dict(
                source=sum("source" in c["scheduled_stages"] for c in donors),
                reused_first_pass=sum("imported_first_pass" in c for c in donors),
                target_openings=sum(
                    "source" in c["scheduled_stages"] for c in cases if c["task_id"] not in source_tasks
                ),
                reused_target_openings=sum(
                    "imported_first_pass" in c for c in lookup.values() if c["task_id"] not in source_tasks
                ),
                write=sum(len(c["representations"]) for c in donors),
                solve=sum(len(c["arms"]) for c in readers) * plan["repeats"],
            ),
        )
        record["budget_check"] = self._budget_check(plan, cfg, cases)
        cfg["registration_sha256"] = digest(canonical(record))
        return resolved

    def _admission(self, cfg):
        normal = super()._admission(cfg)

        def admit(root, stage, case):
            if "donor" not in case:
                return normal(root, stage, case)
            donor = case["donor"]
            if "first_pass_missing" in donor:
                return "donor_ungraded"
            reason = normal(root, "write", donor)
            if reason:
                return "donor_" + reason
            folder = root / ("task-" + case["opening"]["task"]) / "source"
            if not (folder / "completion.json").is_file():
                return "opening_not_started"
            _, _, (row,) = sealed_rows(folder)
            result = row.get("result") or {}
            if not result.get("log_sha256"):
                return "opening_unavailable"
            log = RecordedDecisions(safe_path(folder, row["directory"]), row["run_id"], result["log_sha256"])
            if not any(
                e["payload"].get("origin") == "task_start.first_decision" for e in log.of_type("business.transition")
            ):
                return "opening_unavailable"
            if not (root / ("task-" + case["donor"]["task"]) / "write/completion.json").is_file():
                return "writer_not_started"
            return None

        return admit

    def outcomes(self, plan, workspace, root):
        folder = Path(workspace) / "runs" / plan["id"]
        saved = folder / "resolved-plan.json"
        if not saved.exists():
            return allocation_rows(plan, [])
        record = read(saved)
        require(record["plan_sha256"] == plan_digest(plan), "Transfer recording belongs to another plan")
        if (folder / "registration.json").exists():
            require(read(folder / "registration.json")["preflight"] == record, "Changed transfer registration")
        attempts, skipped = [], unstarted_stages(folder)
        written_memories = {}
        for case in (c for c in record["cases"] if "donor" in c):
            donor = case["donor"]
            write_dir = folder / ("task-" + donor["task"]) / "write"
            banks = {}
            if (write_dir / "completion.json").is_file():
                for w in sealed_rows(write_dir)[2]:
                    if w["status"] == "completed":
                        bank = read_form_bank(
                            safe_path(write_dir, w["directory"]), expected_log_sha256=w["result"]["log_sha256"]
                        )
                        form = w["arm"]
                        banks[form] = bank
                        written_memories[donor["task_id"], donor["source_draw"], form] = dict(
                            form=form, source_run=bank["source_run_id"], writer_run=bank["run_id"],
                            snapshot=bank["snapshot_id"], feedback_success=bank.get("result_feedback", {}).get("success"),
                            items=bank["items"],
                        )
            task_dir = folder / ("task-" + case["task"])
            for n, base in enumerate(case["scheduled_stages"], 1):
                for k in range(len(plan["retry"]["wait_seconds"]) + 1):
                    stage = base if k == 0 else f"{base}-retry-{k}"
                    shard = task_dir / stage
                    reason = skipped.get(f"{task_dir.name}/{stage}")
                    if reason:
                        rows = [dict(arm=a, status="not_started", reason=reason) for a in case["arms"]]
                    elif (shard / "completion.json").is_file():
                        _, ctl, rows = sealed_rows(shard)
                        require(
                            ctl["profile"]["registration_sha256"] == digest(canonical(record)),
                            "Target batch belongs to another registration",
                        )
                    else:
                        continue
                    for row in rows:
                        arm = row["arm"]
                        if row.get("result"):
                            require(
                                row["task_id"] == case["task_id"] and row["source_task_id"] == donor["task_id"],
                                "Target outcome belongs to another transfer pair",
                            )
                        bank = banks.get(case["arm_writer"][arm])
                        observed = observation(
                            shard, row, writer=bank, transfer=dict(rule=case["decision"], bank=case["contents"][arm])
                        )
                        status, success, missing, _ = outcome_fields(observed)
                        attempts.append(
                            dict(
                                source=donor["task_id"],
                                task=case["task_id"],
                                draw=donor["source_draw"],
                                repeat=n,
                                origin=plan["id"],
                                condition=arm,
                                attempt=k,
                                status=status,
                                success=success,
                                missing_reason=missing,
                                decision=observed.get("decision"),
                                termination_reason=observed.pop("termination_reason", None),
                                official_components=observed.pop("official_components", None),
                                effort_from_first_worker=observed.pop("effort_from_first_worker", None),
                                reads=observed["reads"],
                                billing=(row.get("result") or {}).get("billing_by_role", {}),
                                evidence=observed,
                                writer_run=bank["run_id"] if bank else None,
                                donor_snapshot=bank["snapshot_id"] if bank else None,
                            )
                        )
        return allocation_rows(plan, attempts, written_memories=written_memories)

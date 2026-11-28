"""BFCL single-decision questions with a pinned local model, driven by a declared study plan."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import BlobStore, read_experiment
from agent_memory_eval.study.plan import LEGACY_FORMAT, arm_name, conditions, content_text, model_profile, plan_digest, require_declared_models
from agent_memory_eval.study.records import fixed

NATIVE_ONLY = ("selection", "assigned", "purpose", "representation_pairs", "policy_writer_pairs", "policy_registration")


def _implementation(root):
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


class BfclRelevance:
    name = "bfcl-relevance"
    execution_mode = "local"

    def compile(self, plan, root):
        """Native BFCL profile for exactly the declared tasks, representations, readers and conditions."""
        require("transfer" not in plan, "Select the hosted adapter for a transfer plan")
        require("required_sources_sha256" not in plan["first_pass"], "The local BFCL adapter reuses matching generations only")
        setup = read_json(asset_text(root, plan["benchmark"]["setup"]).encode())
        declared = model_profile(plan, root)
        if declared is not None:
            worker = declared["endpoint_terms"]["worker"]
            require(worker["model"] == setup["model"] and worker.get("provider") == "local-transformers"
                    and declared["expected_revisions"]["worker"] == setup["model_revision"],
                    f"BFCL's local adapter runs {setup['model']}; the declared worker is {worker['model']}. "
                    "A matching transport and checkpoint are required.")
        category = {task: cat for cat, ids in setup["tasks"].items() for task in ids}
        tasks = {}
        for task in plan["benchmark"]["tasks"]:
            require(task in category, f"Task {task} is not in the benchmark setup")
            tasks.setdefault(category[task], []).append(task)
        require(plan["first_pass"]["draws"] == 1 and plan["repeats"] == 1,
                "The deterministic local model gives one first pass and one reader per slot")
        pairs = conditions(plan)
        readers = plan.get("reading", {})
        writing = plan.get("writing")
        used_reps = list(dict.fromkeys(rep for rep, _ in pairs))
        used_readers = list(dict.fromkeys(reader for _, reader in pairs))
        cfg = deepcopy(setup)
        for key in NATIVE_ONLY:
            cfg.pop(key, None)
        cfg.update(
            name=plan["id"], tasks=tasks,
            writer_instruction=content_text(root, writing["instruction"]) if writing else setup["writer_instruction"],
            writer_feedback=writing["feedback"] if writing else setup.get("writer_feedback", "result_only"),
            forms={rep: content_text(root, writing["representations"][rep]) for rep in used_reps},
            delivery=[readers[name]["read_mode"] for name in used_readers],
            memory_notice_by_delivery={readers[name]["read_mode"]: content_text(root, readers[name]["notice"])
                                       if "notice" in readers[name] else "" for name in used_readers},
            conditions=[[rep, readers[reader]["read_mode"]] for rep, reader in pairs],
            reader_admission="sealed_bank",
        )
        for name in used_readers:
            reader = readers[name]
            if "tool_descriptions" in reader:
                cfg["read_tool_descriptions"] = reader["tool_descriptions"]
            elif reader["read_mode"] == "on_demand" and plan["format"] != LEGACY_FORMAT:
                cfg.pop("read_tool_descriptions", None)
        return cfg

    def _check_checkpoint(self, cfg, checkpoint):
        require(Path(checkpoint).name == cfg["model_revision"], "Use the registered model checkpoint")
        for name, expected in cfg["checkpoint_sha256"].items():
            with (Path(checkpoint) / name).open("rb") as stream:
                require(hashlib.file_digest(stream, "sha256").hexdigest() == expected, f"Changed checkpoint file {name}")

    def _load_reusable(self, recording, client):
        """Queue verified generations of identical earlier requests from another local recording."""
        from agent_memory_eval.analysis.requests import audit_requests
        require(read_json((recording / "runtime.json").read_bytes()) == client.runtime, "Different local runtime")
        for folder in sorted(p for p in recording.iterdir() if (p / "log.jsonl").is_file()):
            evidence = read_experiment(folder)
            audit_requests(folder, evidence=evidence)
            scan = evidence[0]
            require(not scan["incomplete_runs"] and not scan["pending_requests"], "Reuse needs closed sessions")
            events = list(scan["events"].values())
            starts = {e["payload"]["request_id"]: e["payload"] for e in events if e["type"] == "model.request_started"}
            blobs = BlobStore(folder)
            for e in events:
                if e["type"] != "model.request_finished" or e["payload"]["status"] != "response":
                    continue
                f = e["payload"]
                raw = blobs.get(f["response_ref"])
                if "reused_local_generation" in raw:
                    continue
                gen = raw["local_generation"]
                req = starts[f["request_id"]]["request_ref"]
                wire = blobs.get(req)
                require(raw["model"] == wire["model"] == client.model["id"] and raw["provider"] == "local-transformers"
                        and digest(gen["rendered_input"].encode()) == gen["input_sha256"], "Unverified generation")
                origin = dict(recording=recording.name, task=folder.name, run=e["run_id"], request_id=f["request_id"],
                              request_sha256=req["sha256"], response_sha256=f["response_ref"]["sha256"])
                client.saved[client.wire_key(wire)].append((gen, origin))

    def run(self, plan, workspace, root, *, checkout, model_python, checkpoint, client_factory=None):
        from agent_memory_eval.runner.bfcl_relevance import audit_task, collect_task, load_inputs
        require_declared_models(plan)
        cfg = self.compile(plan, root)
        run_root = Path(workspace) / "runs" / plan["id"]
        run_root.mkdir(parents=True, exist_ok=True)
        fixed(run_root / "profile.json", cfg)
        cases, is_empty = load_inputs(Path(checkout), cfg)
        self._check_checkpoint(cfg, checkpoint)
        registration = dict(format="agent-memory-study-run/v1", adapter=self.name, plan_sha256=plan_digest(plan),
                            profile_sha256=digest((run_root / "profile.json").read_bytes()),
                            implementation=_implementation(root), tasks=[row["id"] for _, row in cases],
                            reuse=[r["recording"] for r in plan["first_pass"].get("reuse", [])])
        fixed(run_root / "registration.json", registration)
        if client_factory is None:
            from agent_memory_eval.models.local_qwen import LocalQwen
            client_factory = LocalQwen
        client = client_factory(model_python, checkpoint, cfg["model"], cfg["generation"]["seed"],
                                stop_at_chat_end=cfg["generation"].get("stop_at_chat_end", False))
        try:
            fixed(run_root / "runtime.json", client.runtime)
            for ref in plan["first_pass"].get("reuse", []):
                self._load_reusable(Path(workspace) / "runs" / ref["recording"], client)
            for case in cases:
                folder = run_root / case[1]["id"]
                if folder.exists():
                    audit_task(run_root, cfg, case, is_empty)  # A closed task is kept; a broken one stops the run.
                    continue
                collect_task(run_root, cfg, case, client, is_empty)
        finally:
            client.close()
        return registration

    def _rows(self, run_root, cfg, cases, is_empty, origin, label):
        from agent_memory_eval.memory.preparation import snapshot_items
        from agent_memory_eval.runner.bfcl_relevance import audit_task
        modes = label if isinstance(label, dict) else None
        rows = []
        for case in cases:
            task = case[1]["id"]
            require(modes is not None or (run_root / task / "log.jsonl").is_file(),
                    f"Reference evidence is missing for BFCL task {task}")
            audited = audit_task(run_root, cfg, case, is_empty, allow_partial=modes is not None)["allocations"]
            state = read_experiment(run_root / task)[1] if any(rec.get("snapshot") for rec in audited) else None
            blobs = BlobStore(run_root / task)
            for rec in audited:
                if rec["phase"] == "write":
                    continue
                if rec["phase"] == "source":
                    condition = "no_memory"
                elif modes is not None:
                    condition = arm_name(rec["form"], modes[rec["mode"]])
                else:
                    condition = f"{origin}/{rec['form']}__{rec['mode']}"
                graded = rec["success"] is not None
                measured = "prompt_tokens" in rec
                rows.append(dict(
                    task=task, source=task, condition=condition, origin=origin, attempt=0,
                    status="graded" if graded else "missing", success=rec["success"],
                    missing_reason=None if graded else rec["status"],
                    tokens=rec["prompt_tokens"] + rec["completion_tokens"] if measured else None,
                    business_calls=int(rec["any_call"]) if rec.get("any_call") is not None else None,
                    reads_list=rec.get("reads", {}).get("memory_list", 0), reads_get=rec.get("reads", {}).get("memory_get", 0),
                    preloaded=rec["mode"] == "preload" and "body" in rec.get("fields", []), cost_usd="0" if measured else None,
                    first_decision=None if rec.get("any_call") is None else ("call" if rec["any_call"] else "no_call"),
                ))
                items = snapshot_items(blobs, state, rec["snapshot"]) if rec["phase"] == "solve" and rec.get("snapshot") else []
                rows[-1].update(memory_title=items[0]["title"] if items else None, memory_body=items[0]["body"] if items else None)
        return rows

    def outcomes(self, plan, workspace, root, *, checkout):
        """Outcome rows for the plan's run, then for each reference recording in the order declared."""
        from agent_memory_eval.runner.bfcl_relevance import load_inputs
        run_root = Path(workspace) / "runs" / plan["id"]
        cfg = self.compile(plan, root)
        require(checkout is not None, "Analyzing BFCL needs its pinned checkout")
        cases, is_empty = load_inputs(Path(checkout), cfg)
        rows = []
        if (run_root / "profile.json").is_file():
            profile_bytes = (run_root / "profile.json").read_bytes()
            require(read_json(profile_bytes) == cfg, "The recorded profile differs from the plan")
            if (run_root / "registration.json").is_file():
                registration = read_json((run_root / "registration.json").read_bytes())
                require(registration["plan_sha256"] == plan_digest(plan), "The run was registered with another plan")
                require(digest(profile_bytes) == registration["profile_sha256"], "The recorded profile hash differs")
            else:
                require(not any(run_root.glob("*/log.jsonl")), "Local task evidence lacks its BFCL registration")
            modes = {r["read_mode"]: name for name, r in plan.get("reading", {}).items()}
            rows = self._rows(run_root, cfg, cases, is_empty, plan["id"], modes)
        wanted = set(plan["benchmark"]["tasks"])
        for ref in plan.get("references", []):
            require("setup" in ref, "A BFCL reference names the profile it was recorded with")
            ref_cfg = read_json(asset_text(root, ref["setup"]).encode())
            ref_root = Path(workspace) / "runs" / ref["recording"]
            registered = read_json((ref_root / "registration.json").read_bytes())
            require(registered["profile_sha256"] == ref["setup"]["sha256"], "Reference was recorded with another profile")
            ref_cases, _ = load_inputs(Path(checkout), ref_cfg)
            chosen = [c for c in ref_cases if c[1]["id"] in wanted]
            require({c[1]["id"] for c in chosen} == wanted, "Reference does not cover the plan's tasks")
            rows += self._rows(ref_root, ref_cfg, chosen, is_empty, ref["recording"], None)
        return rows

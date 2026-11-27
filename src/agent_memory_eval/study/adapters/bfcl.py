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
from agent_memory_eval.study.plan import arm_name, conditions, content_text, plan_digest
from agent_memory_eval.study.records import fixed

NATIVE_ONLY = ("selection", "assigned", "purpose", "representation_pairs", "policy_writer_pairs", "policy_registration")


def _implementation(root):
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


class BfclRelevance:
    name = "bfcl-relevance"

    def compile(self, plan, root):
        """Native BFCL profile for exactly the declared tasks, representations, readers and conditions."""
        setup = read_json(asset_text(root, plan["benchmark"]["setup"]).encode())
        category = {task: cat for cat, ids in setup["tasks"].items() for task in ids}
        tasks = {}
        for task in plan["benchmark"]["tasks"]:
            require(task in category, f"Task {task} is not in the benchmark setup")
            tasks.setdefault(category[task], []).append(task)
        require(plan["first_pass"]["draws"] == 1 and plan["repeats"] == 1,
                "The deterministic local model gives one first pass and one reader per slot")
        pairs = conditions(plan)
        readers = plan["reading"]
        used_reps = list(dict.fromkeys(rep for rep, _ in pairs))
        used_readers = list(dict.fromkeys(reader for _, reader in pairs))
        cfg = deepcopy(setup)
        for key in NATIVE_ONLY:
            cfg.pop(key, None)
        cfg.update(
            name=plan["id"], tasks=tasks,
            writer_instruction=content_text(root, plan["writing"]["instruction"]),
            writer_feedback=plan["writing"]["feedback"],
            forms={rep: content_text(root, plan["writing"]["representations"][rep]) for rep in used_reps},
            delivery=[readers[name]["read_mode"] for name in used_readers],
            memory_notice_by_delivery={readers[name]["read_mode"]: content_text(root, readers[name]["notice"])
                                       if "notice" in readers[name] else "" for name in used_readers},
            conditions=[[rep, readers[reader]["read_mode"]] for rep, reader in pairs],
            reader_admission="sealed_bank",
        )
        for name in used_readers:
            if "tool_descriptions" in readers[name]:
                cfg["read_tool_descriptions"] = readers[name]["tool_descriptions"]
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
        from agent_memory_eval.runner.bfcl_relevance import audit_task
        modes = label if isinstance(label, dict) else None
        rows = []
        for case in cases:
            task = case[1]["id"]
            for rec in audit_task(run_root, cfg, case, is_empty)["allocations"]:
                if rec["phase"] == "write":
                    continue
                if rec["phase"] == "source":
                    condition = "no_memory"
                elif modes is not None:
                    condition = arm_name(rec["form"], modes[rec["mode"]])
                else:
                    condition = f"{origin}/{rec['form']}__{rec['mode']}"
                graded = rec["success"] is not None
                started = rec["status"] != "not_started"
                rows.append(dict(
                    task=task, source=task, condition=condition, origin=origin, attempt=0,
                    status="graded" if graded else "missing", success=rec["success"],
                    missing_reason=None if graded else rec["status"],
                    tokens=rec["prompt_tokens"] + rec["completion_tokens"] if started else None,
                    business_calls=int(rec["any_call"]) if rec.get("any_call") is not None else None,
                    reads_list=rec.get("reads", {}).get("memory_list", 0), reads_get=rec.get("reads", {}).get("memory_get", 0),
                    preloaded=rec["mode"] == "preload" and "body" in rec.get("fields", []), cost_usd="0" if started else None,
                ))
        return rows

    def outcomes(self, plan, workspace, root, *, checkout):
        """Outcome rows for the plan's run, then for each reference recording in the order declared."""
        from agent_memory_eval.runner.bfcl_relevance import load_inputs
        run_root = Path(workspace) / "runs" / plan["id"]
        cfg = read_json((run_root / "profile.json").read_bytes())
        require(cfg == self.compile(plan, root), "The recorded profile differs from the plan")
        cases, is_empty = load_inputs(Path(checkout), cfg)
        modes = {r["read_mode"]: name for name, r in plan["reading"].items()}
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

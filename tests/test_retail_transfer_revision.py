import json
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_openrouter import Reply, response

from agent_memory_eval.contracts.core import ContractError, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.memory.context import past_memory_instruction
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.journal import BlobStore
from scripts import run_retail_transfer_revision as revision
from scripts.analyze_retail_related_transfer import audit_target
from scripts.analyze_retail_transfer_revision import (
    DOMAIN,
    LEGACY,
    audited_writers,
    solve_summary,
    terminal_disposition,
)


class RetailRevisionTests(unittest.TestCase):
    def test_missing_scores_and_shared_controls_are_paired_with_their_own_round(self):
        plans = {"37": {"notices": {"solve1": DOMAIN, "solve2": LEGACY}}}
        cells = []
        for stage in revision.SOLVES:
            for sid, arm, successes in [
                ("36", "fact_explicit", (True, False)),
                ("38", "fact_explicit", (None, True)),
                ("36", "fact_original", (False, True)),
                ("38", "fact_original", (False, True)),
                (None, "empty_preload", (True, False)),
                (None, "no_memory", (False, True)),
            ]:
                cells.append(
                    {
                        "task": "37",
                        "customer": "one",
                        "source": sid,
                        "arm": arm,
                        "stage": stage,
                        "run": str(len(cells)),
                        "stratum": "same" if sid else None,
                        "official": successes[stage == "solve2"],
                    }
                )
        out = solve_summary(cells, plans)
        self.assertEqual(out["contrasts"]["revised/no_memory"]["counts"], {"gain": 1, "missing": 1})
        self.assertEqual(out["contrasts"]["revised/no_memory"]["all_pair_bounds"], [0.5, 1.0])
        self.assertEqual(out["contrasts"]["notice/explicit"]["counts"], {"gain": 1, "missing": 1})
        self.assertEqual(out["contrasts"]["no_memory/notice_order"]["counts"], {"loss": 1})
        self.assertEqual(terminal_disposition([], "STORED"), "other_empty_reply")
        self.assertEqual(terminal_disposition([{"body": "note"}], "SKIP"), "stored_other_reply")

    def test_counterbalance_is_independent_of_row_order_and_reverses_on_each_task(self):
        rows = [{"task": str(i)} for i in range(24)]
        versions = ["legacy-id-v1", "domain-auth-v2"]
        revision.assign_notices("retest", rows, versions)
        shuffled = [{"task": r["task"]} for r in reversed(rows)]
        revision.assign_notices("retest", shuffled, versions)
        self.assertEqual(rows, shuffled[::-1])
        self.assertEqual(Counter(r["notice_by_stage"]["solve1"] for r in rows), dict.fromkeys(versions, 12))
        self.assertTrue(all(set(r["notice_by_stage"].values()) == set(versions) for r in rows))
        self.assertEqual(
            past_memory_instruction("x"), past_memory_instruction("x", notice_version=versions[0])
        )
        self.assertNotIn("Obtain the user ID", past_memory_instruction("x", notice_version=versions[1]))
        with self.assertRaises(ContractError):
            past_memory_instruction("x", notice_version="unregistered")

    @unittest.skipUnless(
        (ROOT / "runs/tau2-retail-full-forms-01/completion.json").exists(), "Needs original retail logs"
    )
    def test_fresh_writers_skips_and_both_notices_reach_native_sessions(self):
        with patch("socket.socket.connect", side_effect=AssertionError("Offline transport")):
            cfg, base, conn, rows, tasks, pre = revision.prepare(ROOT)
            self.assertEqual((pre["writer_allocations"], pre["solve_allocations"]), (176, 608))
            cfg["writing_lines"] = {k: v for k, v in cfg["writing_lines"].items() if k.startswith("episode_")}
            cfg["transfer_spec"] = {"forms": list(cfg["writing_lines"])}
            by_task = {r["task"]: r for r in rows}
            row = by_task["37"]
            source = ROOT / cfg["donors"]["36"]["source_folder"]
            _, original_ctl, (source_slot,) = revision.retail.sealed_rows(source)
            models = original_ctl["model_catalog_entries"]
            blobs = BlobStore(source / source_slot["directory"])
            state = blobs.get(
                json.loads((blobs.root / "log.jsonl").read_bytes().splitlines()[-1])["state_ref"]
            )
            judge = blobs.get(state["assignments"][source_slot["run_id"]]["native_grade_limits"]["model_ref"])
            endpoints = dict(models, grader=judge)
            roles = {v["id"]: k for k, v in endpoints.items()}
            counts = Counter()

            def opener(req, timeout):
                wire = json.loads(req.data)
                role = roles[wire["model"]]
                counts[role] += 1
                entity = endpoints[role]
                raw = response(cost=0)
                raw.update(
                    id=f"offline-revision-{sum(counts.values())}",
                    model=entity["canonical_slug"] or entity["id"],
                    provider=entity["selected_endpoint"]["provider_name"],
                )
                msgs = wire["messages"]
                if msgs[0]["content"].startswith("Review the past public experience"):
                    is_new = "just SKIP" in msgs[0]["content"]
                    if is_new and msgs[-1]["role"] == "tool":
                        answer = "STORED"
                    elif (
                        is_new
                        and digest(msgs[1]["content"].encode())
                        == cfg["donors"]["36"]["public_context_sha256"]
                    ):
                        raw["choices"][0].update(
                            finish_reason="tool_calls",
                            message={
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "write-one",
                                        "type": "function",
                                        "function": {
                                            "name": "memory_store",
                                            "arguments": json.dumps(
                                                {
                                                    "title": "Earlier case",
                                                    "body": "Recheck the current limit.",
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                        )
                        return Reply(raw)
                    else:
                        answer = "SKIP" if is_new else "I reviewed this case."
                elif role == "grader":
                    answer = json.dumps(
                        {
                            "results": [
                                {"expectedOutcome": a, "metExpectation": False, "reasoning": "Offline test."}
                                for a in tasks["37"]["evaluation_criteria"]["nl_assertions"]
                            ]
                        }
                    )
                elif role == "user_simulator":
                    answer = "Help with my order." if counts[role] % 2 else "###STOP###"
                else:
                    answer = "Please confirm your account email."
                raw["choices"][0]["message"]["content"] = answer
                return Reply(raw)

            original_init = OpenRouterClient.__init__

            def fake_client(client, *args, **kwargs):
                original_init(client, *args, **dict(kwargs, opener=opener))

            with (
                TemporaryDirectory(dir=ROOT / "runs") as directory,
                patch.dict("os.environ", OPENROUTER_API_KEY="offline-fixture"),
                patch.object(OpenRouterClient, "__init__", fake_client),
            ):
                root = Path(directory)
                cfg["run_directory"] = root.name
                identity = {"implementation_commit": "offline"}
                reg = {
                    "implementation": identity,
                    "preflight": pre,
                    "endpoint_terms": revision.retail.endpoint_terms(endpoints),
                }
                seals = {}
                for task, stage in [("36", "write"), ("38", "write"), ("37", "solve1"), ("37", "solve2")]:
                    revision.revision_stage(
                        (
                            ROOT,
                            root,
                            cfg,
                            base,
                            conn,
                            by_task[task],
                            tasks[task],
                            stage,
                            identity,
                            models,
                            judge,
                            "2",
                        )
                    )
                    name = f"task-{task}/{stage}"
                    seals[name] = revision.retail.accounting(root / name, "2")
                writes = [
                    c
                    for tid in row["pairs"]
                    for c in audited_writers(ROOT, root, cfg, base, by_task[tid], reg, {"stages": seals})[0]
                ]
                self.assertEqual(
                    Counter(c["disposition"] for c in writes),
                    {"other_empty_reply": 2, "stored_with_marker": 1, "explicit_skip": 1},
                )
                cfg["donors"] = {
                    sid: dict(cfg["donors"][sid], banks=revision.new_banks(ROOT, root, cfg, sid))
                    for sid in row["pairs"]
                }
                self.assertEqual(cfg["donors"]["38"]["banks"]["episode_explicit"]["status"], "empty")
                cells, _ = audit_target(
                    (
                        ROOT,
                        dict(cfg, stages=list(revision.SOLVES)),
                        base,
                        row,
                        tasks["37"],
                        reg,
                        {"stages": seals},
                    )
                )
                self.assertEqual(len(cells), 12)
                exposed = [c for c in cells if c["body_exposed"]]
                self.assertEqual([(c["source"], c["arm"]) for c in exposed], [("36", "episode_explicit")] * 2)
                self.assertEqual(
                    {c["preload_notice_version"] for c in exposed}, {"legacy-id-v1", "domain-auth-v2"}
                )
                self.assertEqual(counts["grader"], 12)

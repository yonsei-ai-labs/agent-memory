import json
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_openrouter import Reply, response

from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.journal import BlobStore
from scripts import audit_retail_transfer_writing as writing_audit
from scripts import run_retail_related_transfer as study
from scripts.analyze_recheck_rounds import mutation_key
from scripts.analyze_retail_related_transfer import (
    audit_target,
    compare_actions,
    summarize,
)


class RetailRelatedTransferTests(unittest.TestCase):
    def test_writing_audit_rejects_an_old_note_or_foreign_customer_turn_in_pure_control(
        self,
    ):
        system = {"role": "system", "content": writing_audit.RETAIL_RULE}
        customer = {"role": "user", "content": "I need to return this order."}
        cell = {
            "arm": "no_memory",
            "run": "control",
            "worker_responses": 1,
            "body_exposed": False,
        }
        simulator = (
            "user_simulator",
            {},
            {"message": {"content": customer["content"]}},
            None,
        )
        for extra in (
            [],
            [writing_audit.snapshot_message([{"body": "a past note"}])],
            [{"role": "user", "content": "another customer's message"}],
        ):
            worker = (
                "worker",
                {"messages": [system, *extra, customer]},
                {"message": {"content": "Hello"}},
                None,
            )
            with (
                self.subTest(extra=extra),
                patch.object(
                    writing_audit, "role_calls", return_value=[simulator, worker]
                ),
            ):
                if extra:
                    with self.assertRaisesRegex(ContractError, "Customer history"):
                        writing_audit.inspect_delivery(
                            None, cell, [], system["content"]
                        )
                else:
                    self.assertEqual(
                        writing_audit.inspect_delivery(
                            None, cell, [], system["content"]
                        )["worker_replies"],
                        1,
                    )

    def test_all_same_order_donors_and_only_new_target_tasks(self):
        def task(*actions):
            return {
                "evaluation_criteria": {
                    "actions": [
                        {
                            "name": "return_delivered_order_items",
                            "arguments": {"order_id": order, "item_ids": items},
                        }
                        for order, items in actions
                    ]
                }
            }

        db = {"orders": {"A": {"user_id": "x"}, "B": {"user_id": "x"}, "C": {"user_id": "y"}}, "users": {}}
        tasks = {
            "1": task(("A", ["2", "1"])),
            "2": task(("A", ["3"])),
            "3": task(("B", ["1"])),
            "4": task(("A", ["1", "2"])),
            "5": task(("C", ["1"])),
            "6": task(("A", ["1", "2"]), ("B", ["4"])),
        }
        rows = study.select_pairs(tasks, db, ["1", "2", "3"], ["1", "4", "5", "6"])
        self.assertEqual([r["task"] for r in rows], ["4", "6"])
        self.assertEqual(rows[0]["customer"], digest(b"x"))
        self.assertEqual(
            {s: v["stratum"] for s, v in rows[0]["pairs"].items()}, {"1": "same", "2": "changed"}
        )
        self.assertEqual(rows[1]["pairs"]["1"]["stratum"], "partial")

    def test_registered_sources_and_shared_controls_per_target_round(self):
        pre = study.retail.read(ROOT / study.PREFLIGHT)
        spec = study.retail.read(ROOT / study.PROFILE)
        self.assertEqual(pre["protocol_sha256"], digest((ROOT / study.PROFILE).read_bytes()))
        rows = pre["targets"]
        self.assertEqual((len(rows), len({r["customer"] for r in rows}), len(pre["donors"])), (24, 17, 22))
        self.assertEqual(
            Counter(p["stratum"] for r in rows for p in r["pairs"].values()),
            {"same": 4, "partial": 7, "changed": 21},
        )
        self.assertEqual(sum(len(s) for r in rows for s in r["stages"].values()), pre["allocations"])
        self.assertEqual(pre["allocations"], 352)
        for row in rows:
            desired = {(s, f) for s in row["pairs"] for f in spec["forms"]} | {
                (None, c) for c in spec["controls"]
            }
            for jobs in row["stages"].values():
                self.assertEqual(len(jobs), len(desired))
                self.assertEqual({(j.get("source_task_id"), j["arm"]) for j in jobs}, desired)
                self.assertTrue(all(j["task_id"] == row["task"] for j in jobs))

    def test_stale_actions_require_excess_successful_executions(self):
        def write(order, success):
            return {"name": "cancel_pending_order", "args": {"order_id": order}, "success": success}

        a, b = (mutation_key("cancel_pending_order", {"order_id": x}) for x in ("A", "B"))
        required, previous, observed = Counter({a: 1}), Counter({a: 2, b: 1}), Counter({b: 2})
        result = compare_actions(
            {"writes": [write("A", True), write("B", False)]}, required, previous, observed
        )
        self.assertEqual(
            (result["required_matched"], result["extra"], result["old_reference_only"]), (1, 0, 0)
        )
        excess = compare_actions(
            {"writes": [write("A", True), write("A", True), write("B", True)]}, required, previous, observed
        )
        self.assertEqual((excess["old_reference_only"], excess["old_observed_only"]), (2, 1))
        self.assertIsNone(compare_actions({}, required, previous, observed))

    def test_two_donors_share_one_control_and_missing_outcomes_keep_their_bounds(self):
        template = {"task": "3", "customer": "x", "stage": "solve1", "writes": []}
        inputs = [
            ("1", "fact_original", True, "same"),
            ("2", "fact_original", None, "changed"),
            (None, "empty_preload", True, None),
            (None, "no_memory", False, None),
        ]
        cells = [
            dict(template, source=s, arm=a, official=v, stratum=g, run=str(i))
            for i, (s, a, v, g) in enumerate(inputs)
        ]
        tasks = {i: {"evaluation_criteria": {"actions": []}} for i in ("1", "2", "3")}
        pre = {"database_replay": {"observed": {i: {"executed": []} for i in ("1", "2")}}}
        out = summarize(cells, pre, tasks, ["fact_original"], ["empty_preload", "no_memory"])
        self.assertEqual(len(out["pairs"]), 2)
        self.assertEqual(out["by_arm"]["empty_preload"]["assigned"], 1)
        control = out["paired_effects"]["all"]["all"]
        self.assertEqual(control["empty_preload"]["counts"], {"both_pass": 1, "missing": 1})
        self.assertEqual(control["empty_preload"]["all_pair_bounds"], [-0.5, 0.0])
        self.assertEqual(control["no_memory"]["all_pair_bounds"], [0.5, 1.0])

    @unittest.skipUnless(
        (ROOT / "runs/tau2-retail-full-forms-01/completion.json").exists(),
        "Needs the archived source episodes",
    )
    def test_two_real_donor_snapshots_and_controls_reach_native_target_and_regrade(self):
        with patch("socket.socket.connect", side_effect=AssertionError("offline test")):
            cfg, base, conn, rows, tasks, pre = study.prepare(ROOT)
            row = next(r for r in rows if r["task"] == "37")
            self.assertEqual(
                {s: p["source_reference_db_matches"] for s, p in row["pairs"].items()},
                {"36": True, "38": False},
            )
            hist = ROOT / cfg["donors"]["36"]["source_folder"]
            _, control, (old,) = study.retail.sealed_rows(hist)
            models = control["model_catalog_entries"]
            blobs = BlobStore(hist / old["directory"])
            tail = read_json((hist / old["directory"] / "log.jsonl").read_bytes().splitlines()[-1])
            g = blobs.get(tail["state_ref"])["assignments"][old["run_id"]]["native_grade_limits"]
            judge = blobs.get(g["model_ref"])
            endpoints = dict(models, grader=judge)
            actor_for = {v["id"]: k for k, v in endpoints.items()}
            calls = Counter()

            def opener(req, timeout):
                wire = json.loads(req.data)
                actor = actor_for[wire["model"]]
                calls[actor] += 1
                entity = endpoints[actor]
                raw = response(cost=0.0)
                raw.update(
                    id=f"offline-transfer-{sum(calls.values())}",
                    model=entity["canonical_slug"] or entity["id"],
                    provider=entity["selected_endpoint"]["provider_name"],
                )
                if actor == "grader":
                    message = json.dumps(
                        {
                            "results": [
                                {"expectedOutcome": a, "metExpectation": False, "reasoning": "Offline test."}
                                for a in tasks["37"]["evaluation_criteria"]["nl_assertions"]
                            ]
                        }
                    )
                elif actor == "user_simulator":
                    message = "Help with my retail order." if calls[actor] % 2 else "###STOP###"
                else:
                    message = "Please confirm the order number."
                raw["choices"][0]["message"]["content"] = message
                return Reply(raw)

            original = OpenRouterClient.__init__

            def offline_client(client, *args, **kwargs):
                original(client, *args, **dict(kwargs, opener=opener))

            with (
                TemporaryDirectory(dir=ROOT / "runs") as cache,
                patch.dict("os.environ", {"OPENROUTER_API_KEY": "transfer-offline-credential"}),
                patch.object(OpenRouterClient, "__init__", offline_client),
            ):
                root = Path(cache)
                cfg.update(stages=["solve1"], run_directory=root.name)
                identity = {"implementation_commit": "offline"}
                reg = {
                    "implementation": identity,
                    "preflight": pre,
                    "endpoint_terms": study.retail.endpoint_terms(endpoints),
                }
                study.transfer_stage(
                    (ROOT, root, cfg, base, conn, row, tasks["37"], "solve1", identity, models, judge, "2")
                )
                seal = study.retail.accounting(root / "task-37/solve1", "2")
                cells, ids = audit_target(
                    (ROOT, cfg, base, row, tasks["37"], reg, {"stages": {"task-37/solve1": seal}})
                )
                self.assertEqual(len(cells), 10)
                self.assertTrue(all(c["official"] is False for c in cells))
                self.assertEqual(len(ids), sum(calls.values()))
                self.assertEqual(calls["grader"], 10)
                self.assertTrue(all(c["body_exposed"] == bool(c["item_count"]) for c in cells))


if __name__ == "__main__":
    unittest.main()

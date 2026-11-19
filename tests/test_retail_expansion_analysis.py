import unittest

from agent_memory_eval.contracts.core import ContractError
from scripts.analyze_full_retail import opening_hashes
from scripts.analyze_retail_sample_expansion import (
    ROUNDS,
    direction_repeats,
    join_trials,
    paired_effect,
    repeated_outcomes,
)


class RetailExpansionAnalysisTests(unittest.TestCase):
    def test_repeats_and_customers_have_distinct_weights_and_missing_denominators(self):
        def pair(task, customer, state):
            return {"task": task, "customer": customer, "state": state}

        rows = [pair("x", "A", "gain")] * 3 + [
            pair("y", "A", "loss"),
            pair("z", "B", "loss"),
            pair("z", "B", "missing"),
        ]
        result = paired_effect(rows)
        self.assertAlmostEqual(result["task_difference"], -1 / 3)
        self.assertEqual(result["customer_difference"], -0.5)
        self.assertEqual(result["scored_tasks"], 3)
        self.assertEqual(result["counts"], {"gain": 3, "loss": 2, "missing": 1})
        self.assertEqual(
            repeated_outcomes([{"official": False}, {"official": None}]), "incomplete"
        )

    def test_repeat_choices_do_not_create_new_sources_or_controls(self):
        rows = [{"task": "1", "customer": "customer", "cohort": "test"}]
        cfg = {"forms": ["fact"], "controls": ["no_memory", "empty_preload"]}
        cells = [
            {
                "task": "1",
                "stage": "source",
                "arm": "no_memory",
                "run": "s",
                "official": False,
            },
            {
                "task": "1",
                "stage": "write",
                "arm": "fact_original",
                "run": "w",
                "snapshot": "sealed-empty",
            },
        ]
        for arm in ("fact_original", "no_memory", "empty_preload"):
            for r in ROUNDS:
                bank = arm == "fact_original"
                outcome = bank == (r != "solve2")
                cells.append(
                    {
                        "task": "1",
                        "arm": arm,
                        "stage": r,
                        "run": arm + r,
                        "official": outcome,
                        "grading": "graded",
                        "writer_run": "w" if bank else None,
                        "snapshot": "sealed-empty" if bank else None,
                        "opening": {
                            "simulator_request": "common",
                            "worker_input_without_opening": arm + r[:5],
                        },
                    }
                )
        _, pairs, hashes = join_trials(rows, cells, cfg)
        self.assertEqual(len(pairs), 6)
        self.assertEqual(hashes["verified_task_arms"], 3)
        self.assertEqual(
            direction_repeats(pairs)["test/fact_original/no_memory/gain"], {"1_of_2": 1}
        )
        cells[-1]["opening"]["simulator_request"] = "different persona"
        with self.assertRaises(ContractError):
            join_trials(rows, cells, cfg)
        cells[-1]["opening"]["simulator_request"] = "common"
        cells[2]["snapshot"] = "swapped"
        with self.assertRaises(ContractError):
            join_trials(rows, cells, cfg)

    def test_worker_wire_sensitivity_to_customer_wording_and_policy_changes(self):
        def make(policy, opening):
            msgs = [
                {"role": "system", "content": policy},
                {"role": "user", "content": opening},
            ]
            return opening_hashes(
                [
                    ("user_simulator", {"messages": []}, None, "response"),
                    ("worker", {"seed": 12, "messages": msgs}, None, "response"),
                ]
            )

        a, b, c = make("same", "hello"), make("same", "hi"), make("changed", "hello")
        self.assertEqual(
            a["worker_input_without_opening"], b["worker_input_without_opening"]
        )
        self.assertNotEqual(
            a["worker_input_without_opening"], c["worker_input_without_opening"]
        )
        self.assertNotEqual(a["worker_request"], b["worker_request"])
        self.assertIsNone(opening_hashes([])["simulator_request"])

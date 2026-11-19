import unittest
from copy import deepcopy
from dataclasses import replace

from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.replay_comparison import preflight
from agent_memory_eval.experiments.same_customer_decisions import PROFILE, STUDY, classify, public_twins, schedule


class SameCustomerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = read_json((ROOT / PROFILE).read_bytes())

    def test_crossed_treatment_order_and_writer_counts(self):
        slots = schedule(self.cfg)
        self.assertEqual(len(slots), 96)
        for case in self.cfg["cases"]:
            first, second = ([r["arm"] for r in slots if (r["case"], r["repeat"]) == (case, i)] for i in (1, 2))
            self.assertEqual(first, second[::-1])
            self.assertEqual(len(set(first)), 12)
        for block in range(1, 17):
            rows = [r for r in slots if r["batch"] == block]
            self.assertEqual(sum(r["form"] in ("bound", "unbound") for r in rows), 1)

    def test_past_relation_requires_returned_source_evidence(self):
        anchor = self.cfg["anchors"]["17"]
        bank = dict(run_id="test", items=[dict(body=anchor["source_excerpt"])])
        with self.assertRaisesRegex(ContractError, "public tool"):
            public_twins(anchor, bank, {"messages": [dict(role="user", content="FQ8APE")]})

    def test_block_preflight_rejects_different_evidence_for_one_arm(self):
        slots = [dict(case="case", arm="arm", batch=i) for i in (1, 2)]

        def stage(journal, workspace, cfg, rows):
            return dict(cells={"case": {"arm": 1}}, wires={("case", "arm"): 1}, evidence={"arm": rows[0]["batch"]})

        study = replace(STUDY, stage=stage, schedule=lambda cfg: deepcopy(slots))
        with self.assertRaisesRegex(ContractError, "across isolated journals"):
            preflight(ROOT, {}, study)

    def test_old_read_is_distinct_from_a_write_or_correct_target(self):
        rule = self.cfg["cases"]["different_unspecified"]
        message = {
            "tool_calls": [
                {"function": {"name": "get_reservation_details", "arguments": '{"reservation_id":"JMO1MG"}'}}
            ]
        }
        scored = classify("different_unspecified", rule, message)
        self.assertTrue(scored["old_in_batch"])
        self.assertFalse(scored["direct"])
        self.assertEqual(scored["writes"], [])

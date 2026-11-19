from pathlib import Path
from types import SimpleNamespace
import unittest

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.experiments.forms_solve import model_free_basis
from scripts.run_recheck_rounds import classify, design_for, prepare


class RecheckRoundsTest(unittest.TestCase):
    def test_retail_native_grader_cannot_request_language_judgment(self):
        criteria = SimpleNamespace(reward_basis=["DB", "NL_ASSERTION"], nl_assertions=None, communicate_info=[])
        self.assertTrue(model_free_basis("retail", criteria))
        criteria.nl_assertions = ["Was the worker helpful?"]
        self.assertFalse(model_free_basis("retail", criteria))
        self.assertFalse(model_free_basis("airline", criteria))

    def test_masked_writing_gate_accepts_both_directions_and_preserves_missing(self):
        cfg = dict(forms=["fact", "lesson", "episode"], conditions=["original", "recheck"])
        rows = [dict(task="1")]
        index = {("1", f"{f}_{v}"): f + v for f in cfg["forms"] for v in cfg["conditions"]}
        text = dict(title="sample", body="Confirm the current order status.")
        packet = dict(
            factoriginal=[],
            factrecheck=[text],
            lessonoriginal=[text],
            lessonrecheck=[],
            episodeoriginal=None,
            episoderecheck=[text],
        )
        labels = dict(
            packet_sha256=digest(canonical(packet)),
            quotes={k: text["body"] if v else None for k, v in packet.items()},
        )
        result = classify(packet, index, cfg, rows, labels)
        self.assertEqual(result["counts"], dict(gain=1, loss=1, both=0, neither=0, missing=1))
        self.assertTrue(result["content_changed"])
        labels["quotes"]["factrecheck"] = "I invented this example."
        with self.assertRaises(ContractError):
            classify(packet, index, cfg, rows, labels)

    def test_frozen_rounds_keep_customer_prompt_and_solver_pairing(self):
        workspace = Path(__file__).resolve().parents[1]
        if not (workspace / "data/tau2-bench/data/tau2/domains/retail/tasks.json").exists():
            self.skipTest("Native retail checkout is not installed")
        cfg, base, _, _, rounds, _ = prepare(workspace)
        self.assertEqual(len({r["customer"] for group in rounds.values() for r in group}), 12)
        for cohort, rows in rounds.items():
            row = rows[0]
            first, _ = design_for(cfg, base, cohort, row, "write", None)
            for f in cfg["forms"]:
                lines = first["writing"]["lines"]
                self.assertEqual(lines[f + "_recheck"], lines[f + "_original"] + "\n" + cfg["suffix"])
            for n in (1, 2):
                design, jobs = design_for(cfg, base, cohort, row, "solve" + str(n), None)
                self.assertEqual(design["model"]["seed_pairs"][0]["native_and_solver"], cfg["solver_seeds"][n - 1])
                self.assertEqual({j["arm"] for j in jobs}, set(lines) | set(cfg["controls"]))
                self.assertEqual(len(jobs), 10)

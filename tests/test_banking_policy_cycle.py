from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.experiments.clause_contrast import clause_variants, next_action, preload_at_decision
from scripts.analyze_banking_policy_cycle import dev_summary
from scripts.run_banking_policy_cycle import POLICY
from scripts import run_banking_policy_cycle as cycle
from scripts.run_fresh_matched import jobs_for


class BankingPolicyCycleTests(unittest.TestCase):
    note = dict(
        id="m_origin",
        version="v_origin",
        title="Referral",
        body="Earlier approval used TOOL_OK. Some calls went to the office. End.",
    )
    target = "Earlier approval used TOOL_OK."
    background = "Some calls went to the office."
    base = dict(
        messages=[dict(role="system", content="policy"), dict(role="user", content="question")],
        tools=[
            dict(
                type="function",
                function=dict(
                    name="route",
                    parameters=dict(
                        type="object", properties={"reason": {"type": "string"}}, required=["reason"]
                    ),
                ),
            )
        ],
        seed=0,
    )

    def test_only_body_changes_and_unedited_item_is_preserved(self):
        before = deepcopy(self.note)
        variants, edits = clause_variants(self.note, self.target, self.background, "TOOL_OK")
        wires = [preload_at_decision(self.base, POLICY, "optional", v) for v in variants.values()]
        for arm, v in variants.items():
            self.assertEqual(v["id"], variants["original"]["id"])
            self.assertNotEqual(v["version"], self.note["version"])
            self.assertEqual(v["title"], self.note["title"])
            if arm != "original":
                edit = edits[arm]
                self.assertEqual(
                    v["body"][: edit["start"]] + edit["removed"] + v["body"][edit["start"] :],
                    self.note["body"],
                )
        self.assertEqual(self.note, before)
        self.assertEqual(len(self.base["messages"]), 2)
        for w in wires:
            self.assertEqual(w["messages"][0], wires[0]["messages"][0])
            self.assertEqual(w["messages"][2:], self.base["messages"][1:])
            self.assertEqual(w["tools"], self.base["tools"])
        self.assertNotIn("TOOL_OK", variants["omit_target"]["body"])
        self.assertIn("TOOL_OK", variants["omit_background"]["body"])

    def test_leaking_or_overlapping_excerpts_refused(self):
        for item, other in [
            (dict(self.note, title="TOOL_OK"), self.background),
            (dict(self.note, body=self.note["body"] + " TOOL_OK"), self.background),
            (self.note, self.target),
            (self.note, "End."),
        ]:
            with self.assertRaises(ContractError):
                clause_variants(item, self.target, other, "TOOL_OK")

    def test_declared_action_needs_a_real_single_valid_call(self):
        def score(calls, text=None):
            return next_action(
                {"message": {"content": text, "tool_calls": calls}},
                self.base["tools"],
                "route",
                {"reason": "chosen"},
            )

        def call(args):
            return {"function": {"name": "route", "arguments": json.dumps(args)}}

        self.assertEqual(score([], '{"reason":"chosen"}'), "text")
        self.assertEqual(score([call({})]), "invalid_arguments")
        self.assertEqual(score([call({"reason": "another"})]), "other_arguments")
        self.assertEqual(score([call({"reason": "chosen"})]), "target_action")
        self.assertEqual(score([call({"reason": "chosen"})] * 2), "multiple_calls")

    def test_missing_trials_remain_in_block_and_preload_only_is_respected(self):
        arms = ["original", "omit_target", "omit_background"]
        rows = [
            dict(case="point", form="fact", repeat=1, arm=arm, behavior=i)
            for arm, i in zip(arms, ["target_action", "unobserved", "search"])
        ]
        result = dev_summary(rows, arms)
        self.assertEqual(result["original_vs_target"]["missing"], 1)
        self.assertEqual(result["original_vs_background"]["gain"], 1)
        with self.assertRaises(ContractError):
            dev_summary(rows[1:], arms)
        case = dict(task="a", task_id="a", reader_modes=["preload"])
        self.assertEqual(len(jobs_for(case, "write")), 5)
        self.assertEqual(
            {r["arm"] for r in jobs_for(case, "solve")},
            {x + "__preload" for x in ["episode", "free", "fact", "procedure", "lesson"]},
        )
        with self.assertRaises(ContractError):
            jobs_for(dict(case, reader_modes=["preload", "preload"]), "solve")

    def test_validation_writer_change_is_local_to_its_policy(self):
        cfg = {"writing": {"common_instruction_ref": {"path": "base"}, "lines": {"fact": "facts"}}}
        old = deepcopy(cfg)
        case = {"writer_instruction": {"path": "candidate"}, "reader_modes": ["preload"]}
        args = (None, None, cfg, {}, {}, case, {}, "solve", {}, {}, None, "0.1")
        with patch.object(cycle.bank.matched, "stage_job") as execute:
            cycle.policy_stage_job(args)
            used = execute.call_args.args[0][2]
        self.assertEqual(used["writing"], dict(old["writing"], common_instruction_ref={"path": "candidate"}))
        self.assertEqual(cfg, old)

    def test_worker_and_customer_revisions_must_remain_fixed(self):
        models = {r: {"canonical_slug": r + "-1"} for r in ("worker", "user_simulator")}
        cfg = {"expected_model_revisions": {r: m["canonical_slug"] for r, m in models.items()}}
        with patch.object(cycle.bank.matched, "study_models", return_value=(models, None)):
            self.assertEqual(cycle.registered_models(cfg, {}), (models, None))
            models["user_simulator"]["canonical_slug"] = "updated"
            with self.assertRaises(ContractError):
                cycle.registered_models(cfg, {})

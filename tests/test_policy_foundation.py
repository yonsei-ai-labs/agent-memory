import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.policy_evidence import (
    PolicyEvidence,
    TextFields,
    banking_cost,
    candidates,
    current_fields,
    stored_outcome_check,
)
from agent_memory_eval.analysis.trajectory import allocation
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.runner.policy_round import compile_round, register, registered, verify_native_registration
from agent_memory_eval.recording.assets import asset_text


PLAN = ROOT / "protocols/diagnostics/portable-writer-round-v1.json"


class EvidenceTests(unittest.TestCase):
    def test_stored_marker_is_compared_to_the_feedback_bit(self):
        labels = dict(success="Task passed.", failure="Task failed.")
        for bit, items, expected in [
            (False, [dict(body="Task failed.\nThe tool returned successfully.")], "matches_feedback"),
            (False, [dict(body="Task passed.")], "opposes_feedback"),
            (True, [dict(body="My summary\nTask passed.")], "no_marker_on_first_line"),
            (False, [], "no_stored_item"), (False, None, "unobserved_write"),
            (None, [dict(body="Task failed.")], "unassessable"),
        ]:
            self.assertEqual(stored_outcome_check(labels, bit, items), expected)

    def test_typed_matches_and_candidate_boundaries(self):
        for value, expected in (
            ("EcoCard", "exact"),
            ("Eco Card", "normalized_only"),
            ("ecocard", "normalized_only"),
            ("EcoCardPlus", "absent"),
            ("pre-EcoCard", "absent"),
        ):
            self.assertEqual(TextFields([value]).match("EcoCard"), expected)
        self.assertEqual(TextFields(["AB12", "ab-12", "AB-123"]).match("AB-12"), "absent")
        self.assertEqual(TextFields(["Eco", "Card"]).match("EcoCard"), "absent")
        msg = dict(
            content="Try EcoCard",
            tool_calls=[
                dict(id="private_transport_123", function=dict(name="lookup", arguments='{"city":"Seoul"}'))
            ],
        )
        self.assertEqual(candidates([msg]), {"EcoCard", "lookup", "Seoul"})

    def test_projected_history_keeps_only_bound_public_fields(self):
        from types import SimpleNamespace

        blobs = {
            "manifest": {
                "bindings": [
                    dict(pointer=f"/messages/0/{path}", source_id="own")
                    for path in (
                        "role",
                        "content",
                        "tool_calls/0/id",
                        "tool_calls/0/function/name",
                        "tool_calls/0/function/arguments",
                    )
                ]
                + [dict(pointer="/messages/1", source_id="bank")]
            },
            "catalog": {"own": dict(kind="self_authored"), "bank": dict(kind="memory_return")},
        }
        log = SimpleNamespace(blobs=blobs, state={"provenance": {"q": {"sources_ref": "catalog"}}})
        wire = {
            "messages": [
                dict(
                    role="assistant",
                    content="Prior reply",
                    tool_calls=[dict(id="receipt", function={"name": "memory_get", "arguments": "{}"})],
                ),
                dict(role="tool", content="A past note"),
            ]
        }
        self.assertEqual(
            list(current_fields(log, dict(input_manifest_ref="manifest", request_id="q"), wire)),
            ["Prior reply", "memory_get", "{}"],
        )

    def test_all_allocations_and_joint_success_efficiency(self):
        source = dict(allocation("q", "c", "source", "no_memory"), run="s", status="graded", official_success=True)

        def reader(policy, run, grade):
            return dict(
                allocation("q", "c", "solve", policy, form="fact", delivery="preload", draw=1, repeat=1),
                source_run="s",
                run=run,
                status="graded" if grade is not None else "not_started",
                official_success=grade,
                decision=dict(calls=[]) if grade is not None else None,
            )

        a, b = reader("existing", "r", True), reader("candidate", None, None)
        study = dict(
            benchmark="example",
            scope="sources_and_readers",
            bindings={},
            observations_sha256="a" * 64,
            observations=[source, a, b],
            same_input_repetitions={},
        )
        collector = PolicyEvidence()
        collector.readers = {"s": dict(business_calls=3), "r": dict(business_calls=0)}
        result = collector.report(study)
        by = {r["condition"]: r for r in result["by_condition"]}
        self.assertEqual(by["existing"]["first_decision"]["no_call"]["success"], 1)
        self.assertEqual(by["candidate"]["first_decision"]["unobserved"]["ungraded"], 1)
        effort = by["existing"]["versus_first_pass"]["both_success"]["metrics"]
        self.assertEqual((effort["business_calls"]["difference"], effort["tokens"]["missing"]), ("-3", 1))
        self.assertEqual(result["policy_comparisons"][0]["outcomes"]["missing"], 1)
        self.assertEqual(len(result["observations"]), 3)

    def test_stage_costs_are_not_double_counted(self):
        a = dict(stages={"s": dict(seal="a", reported="0.1", held="0.02")}, total_covered_usd="0.12")
        b = dict(stages={"w": dict(seal="b", reported="0.2", held="0")}, total_covered_usd="0.32")
        self.assertEqual(banking_cost(a, b), dict(confirmed_usd="0.3", held_usd="0.02", covered_usd="0.32"))
        with self.assertRaises(ContractError):
            banking_cost(a, dict(b, total_covered_usd="1"))


class RegistrationTests(unittest.TestCase):
    def test_v2_selects_backends_and_freezes_the_first_pass_comparison(self):
        self.plan.update(format="agent-memory-policy-round/v2",
                         evaluation=dict(reference="own_no_memory_first_pass", policy_axes=["form"]))
        del self.plan["policy"]["candidate_instruction"]
        cfg = self.plan["benchmarks"].pop("bfcl-relevance")
        first = register(self.root, self.plan)
        root, _, profiles = registered(self.root, self.plan)
        self.assertEqual(set(profiles), {"banking-opening"})
        ref = profiles["banking-opening"]["candidate_instruction"]
        self.assertTrue(asset_text(self.root, ref).endswith(asset_text(self.root, self.plan["policy"]["clause"])))
        self.assertEqual(register(self.root, self.plan), first)
        with self.assertRaises(ContractError):
            registered(self.root, dict(self.plan, evaluation=dict(self.plan["evaluation"], policy_axes=["condition"])))
        (root / "candidate-writer.txt").write_text("changed")
        with self.assertRaises(ContractError):
            registered(self.root, self.plan)
        self.plan["benchmarks"] = {"bfcl-relevance": cfg}
        self.assertEqual(set(compile_round(self.root, self.plan)[1]), {"bfcl-relevance"})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = json.loads(PLAN.read_bytes())
        shutil.copytree(ROOT / "protocols", self.root / "protocols")
        for evidence in self.plan["evidence"]:
            path = Path(evidence["artifact"]["path"])
            (self.root / path.parent).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, self.root / path)
        self.groups = patch(
            "agent_memory_eval.runner.policy_round.known_customer_groups",
            side_effect=lambda domain, dev, tasks: {
                t: dict(identified=True, unresolved_development=[], overlapping_development=[t] if t in dev else [])
                for t in tasks
            },
        ).start()
        self.addCleanup(patch.stopall)

    def test_effective_writer_and_reader_policies_are_shared(self):
        receipt, profiles = compile_round(self.root, self.plan)
        bank = profiles["banking-opening"]
        bfcl = profiles["bfcl-relevance"]
        writing = json.loads(asset_text(self.root, bank["required_writer"]))
        for form, line in writing["lines"].items():
            for arm, label in bfcl["policy_writer_pairs"][form].items():
                ref = bank["base_instruction" if arm == "reference" else "candidate_instruction"]
                self.assertEqual(
                    asset_text(self.root, ref).rstrip() + "\n" + line,
                    bfcl["writer_instruction"] + "\n" + bfcl["forms"][label],
                )
        self.assertEqual(bfcl["delivery"], [bank["reader"]])
        self.assertEqual(bfcl["memory_notice"], asset_text(self.root, receipt["reader_instruction"]))
        self.assertEqual(bank["policy_registration"], bfcl["policy_registration"])
        self.assertEqual(self.plan["policy"]["reader"], "preload")
        self.plan["policy"]["reader"] = "on_demand"
        other, new = compile_round(self.root, self.plan)
        self.assertNotEqual(other["policy_sha256"], receipt["policy_sha256"])
        self.assertEqual(new["banking-opening"]["reader"], "on_demand")

    def test_changed_criteria_and_changed_native_profile_are_rejected(self):
        first = register(self.root, self.plan)
        self.assertEqual(first, register(self.root, self.plan))
        with self.assertRaises(ContractError):
            registered(self.root, dict(self.plan, criterion="changed"))
        root, _, profiles = registered(self.root, self.plan)
        verify_native_registration(root / "bfcl-relevance.json", profiles["bfcl-relevance"], "bfcl-relevance")
        with self.assertRaises(ContractError):
            verify_native_registration(
                root / "bfcl-relevance.json", dict(profiles["bfcl-relevance"], forms={}), "bfcl-relevance"
            )
        (root / "bfcl-relevance.json").write_text("{}")
        with self.assertRaises(ContractError):
            registered(self.root, self.plan)

    def test_overlapping_evidence_and_late_registration_are_rejected(self):
        with self.assertRaisesRegex(ContractError, "confirmation customers"):
            compile_round(self.root, dict(self.plan, purpose="confirmation"))
        self.groups.side_effect = lambda domain, dev, tasks: {}
        with self.assertRaisesRegex(ContractError, "question families"):
            compile_round(self.root, dict(self.plan, purpose="confirmation"))
        _, profiles = compile_round(self.root, self.plan)
        (self.root / "runs" / profiles["bfcl-relevance"]["name"]).mkdir(parents=True)
        with self.assertRaisesRegex(ContractError, "predates"):
            register(self.root, self.plan)

    def test_confirmation_inherits_questions_from_the_previous_decision(self):
        self.plan["evidence"] = []
        bank_settings = self.plan["benchmarks"]["banking-opening"]
        bank_settings["development_tasks"] = []
        settings = self.plan["benchmarks"]["bfcl-relevance"]
        tasks = list(settings["question_families"])
        settings["development_tasks"] = []
        self.plan["purpose"] = "confirmation"
        compile_round(self.root, self.plan)
        prior = dict(
            format="agent-memory-policy-decision/v1",
            cohorts={
                "banking-opening": dict(evaluated_tasks=[], development_tasks=[]),
                "bfcl-relevance": dict(evaluated_tasks=[tasks[0]], development_tasks=[]),
            },
        )
        path = self.root / "prior.json"
        path.write_bytes(canonical(prior))
        self.plan["previous_decision"] = dict(path=path.name, sha256=digest(path.read_bytes()))
        with self.assertRaisesRegex(ContractError, "question families"):
            compile_round(self.root, self.plan)

import gzip
import json
import unittest
from pathlib import Path

from agent_memory_eval.adapters.first_pass_archives import banking_rows
from agent_memory_eval.analysis.decision_attribution import action_reuse_counts, projected_actions
from agent_memory_eval.analysis.first_pass import compare_first_pass
from agent_memory_eval.analysis.outcome_report import first_pass_archive
from agent_memory_eval.analysis.trajectory import allocation
from agent_memory_eval.contracts.core import ContractError


ARCHIVE = Path(__file__).resolve().parents[1] / "artifacts/analyses"
BANK = ARCHIVE / "20261127-banking-memory-cohort"
BFCL = ARCHIVE / "20261127-bfcl-memory-cohort/native.json.gz"


def source(run, success):
    return dict(allocation("t", "customer", "source", "no_memory"), run=run, official_success=success, status="graded")


def reader(s, condition, success, *, form="lesson", delivery="read"):
    row = allocation(s["task"], s["cluster"], "solve", condition, form=form, delivery=delivery, draw=1, repeat=1)
    row.update(source_run=s["run"], run=f"{s['run']}-{condition}-{form}-{delivery}",
               status="graded" if success is not None else "ungraded", official_success=success)
    return row


class FirstPassTests(unittest.TestCase):
    def test_arbitrary_policies_same_task_multiple_sources_and_missing_arms(self):
        a, b = source("s1", False), source("s2", True)
        rows = [a, b, reader(a, "A", True), reader(a, "B", True), reader(a, "C", False),
                reader(b, "A", False), reader(b, "B", None)]
        d = compare_first_pass(rows, policy_axes=["condition"], cluster_unit="customer")
        self.assertEqual(d["no_memory_first_pass"], dict(assigned=2, graded=2, successes=1, missing=0))
        self.assertEqual((d["common"]["assigned_slots"], d["common"]["graded_slots"]), (2, 1))
        ar, br, cr = d["policies"]
        self.assertEqual((ar["versus_first_pass"]["gain"], ar["versus_first_pass"]["loss"]), (1, 1))
        self.assertEqual(ar["common_versus_first_pass"]["difference"], 1.0)
        self.assertEqual(ar["clustered"]["observed_clusters"], 1)
        self.assertEqual((br["versus_first_pass"]["missing"], cr["outcomes"]["assigned"]), (1, 1))
        self.assertIsNone(d["slots"][1]["readers"][2])

    def test_select_memory_form_as_policy_without_rewriting_scoring(self):
        s = source("s", True)
        a = reader(s, "rule", True, form="new_form")
        b = reader(s, "rule", True, form="trace")
        s["decision_effort"] = dict(tokens=40, requests=3, usd="0.2")
        a["decision_effort"] = dict(tokens=20, requests=2, usd="0.1")
        b["decision_effort"] = dict(tokens=None, requests=1, usd=None)
        a["decision"] = dict(request_sha256="wire", memory=[dict(field="body")])
        d = compare_first_pass([s, a, b], policy_axes=["form"], cluster_unit="customer")
        self.assertEqual(d["common"]["policy_successes"], [1, 1])
        m = d["all_success_effort"]["metrics"]
        self.assertEqual(m["requests"]["totals"], [3, 2, 1])
        self.assertEqual(m["tokens"], dict(covered_slots=0, totals=[None, None, None]))
        self.assertEqual(d["slots"][0]["readers"][0]["decision_memory_fields"], ["body"])
        self.assertIsNone(d["slots"][0]["readers"][1]["decision_memory_fields"])

    def test_broken_pairing_fails_closed(self):
        s = source("s", False)
        a = reader(s, "rule", True)
        for changed in (dict(a, source_run="another"), dict(a, cluster="another"),
                        dict(a, status="ungraded"), dict(a, official_success=1)):
            with self.subTest(changed=changed), self.assertRaises(ContractError):
                compare_first_pass([s, changed], cluster_unit="customer")
        a.update(writer_run="w", snapshot="snap")
        b = dict(reader(s, "rule", True, delivery="inject"), writer_run="w2", snapshot="other")
        with self.assertRaisesRegex(ContractError, "different memories"):
            compare_first_pass([s, a, b], cluster_unit="customer")
        with self.assertRaisesRegex(ContractError, "Duplicate policy"):
            compare_first_pass([s, a, dict(a, run="another")], cluster_unit="customer")

    def test_two_native_archives_same_measurement(self):
        for path, recovery, raw, slots, source_wins, auto, lookup in (
            (BANK / "initial.json.gz", BANK / "recovery.json.gz", (45, 8), 222, 39, 36, 30),
            (BFCL, None, (42, 37), 197, 172, 165, 152),
        ):
            with self.subTest(path=path):
                d = first_pass_archive(path, recovery=recovery, details=False, policy_axes=["delivery"])
                self.assertEqual((d["no_memory_first_pass"]["graded"], d["no_memory_first_pass"]["successes"]), raw)
                self.assertEqual((d["common"]["graded_slots"], d["common"]["first_pass_successes"]), (slots, source_wins))
                self.assertEqual(d["common"]["policy_successes"], [lookup, auto])
                self.assertEqual(d["policies"][0]["first_decisions_with_body"], 0)
                self.assertEqual(len(d["policies"][1]["by_form"]), 5)
                m = d["all_success_effort"]["metrics"]["tokens"]
                if recovery:
                    self.assertEqual(m["covered_slots"], 0)  # Source setup needs its journal.
                else:
                    self.assertEqual(m["totals"], [92077, 130069, 144335])

    def test_recovery_must_belong_to_original_journal_and_allocation(self):
        archive = json.loads(gzip.decompress((BANK / "initial.json.gz").read_bytes()))
        recovery = json.loads(gzip.decompress((BANK / "recovery.json.gz").read_bytes()))
        original = recovery["observations"][0]
        for bad in (dict(original, original_journal="different"),
                    dict(original, reader=dict(original["reader"], form="other"))):
            with self.assertRaises(ContractError):
                banking_rows(archive, recovery=dict(recovery, observations=[bad]))
        with self.assertRaises(ContractError):
            banking_rows(archive, recovery=dict(recovery, observations=[original, original]))

    def test_recorded_action_reuse_by_source_grade(self):
        archive = json.loads(gzip.decompress((ARCHIVE / "20261127-source-action-and-feedback.json.gz").read_bytes()))
        rows = archive["observations"]
        traces = archive["ordered_projected_calls"]
        self.assertEqual(action_reuse_counts(rows, traces), archive["summary"])
        self.assertEqual(len(rows), 447 + 87)
        transfer = projected_actions([("lookup", {}), ("transfer_to_human_agents", {
            "reason": "account_ownership_dispute", "summary": "wording can vary"})], archive["worker_action_arguments"])
        for r in rows:
            if r["task"] == "task_004" and r["group"] == "preload":
                self.assertIn(transfer[0], traces[r["run"]])
        with self.assertRaises(ContractError):
            action_reuse_counts([dict(rows[0], source_signature="wrong")], traces)


if __name__ == "__main__":
    unittest.main()

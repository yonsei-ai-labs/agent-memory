import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator

from agent_memory_eval.adapters.banking_trajectories import _opening_keys, _searches, _stopped_readers, search_documents
from agent_memory_eval.analysis.recorded_decisions import RecordedDecisions
from agent_memory_eval.analysis.trajectory import (allocation, assess_opportunity, compact_report, cue, decision_path,
                                                   source_opportunities, trajectory_report)
from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.batch import sealed_rows


SHA = "c" * 64
BINDING = dict(protocol_sha256=SHA, source=dict(recording="source-recording", registration_sha256=SHA))


def source(task="q", cluster="customer", *, previous=("document",), current=(), status="graded"):
    return dict(allocation(task, cluster, "source", "no_memory"), run=task + "-s", status=status,
                official_success=False if status == "graded" else None, reason="completed" if status == "graded" else "interrupted",
                source_cues={"document": cue(previous, current)})


def reader(repeat, *, choice="open", status="graded"):
    row = allocation("q", "customer", "solve", "policy", form="fact", delivery="preload", draw=1, repeat=repeat)
    row.update(source_run="q-s", reason="no_receipt")
    if status != "not_started":
        call = dict(id=f"call-{repeat}", name=choice, arguments={"number": 1})
        actual = dict(request_id="reader-request", request_sha256=SHA, manifest_sha256=SHA, response_sha256=SHA,
                      calls=[call], memory=[dict(item="a", version="v", field="body")])
        row.update(status=status, run=f"reader-{repeat}", official_success=status == "graded" or None,
                   decision=actual, first_input_sha256=SHA, snapshot="bank", writer_run="writer", progress=dict(opened=1))
    return row


def report(rows):
    return trajectory_report("example", BINDING, rows, metrics={"opened": "Opened"}, cues={"document": "Document"},
                             sources_only=False)


def decision_log(replies, *, returns=None, spans=()):
    requests, finishes, blobs = [], {}, {}
    for i, calls in enumerate(replies):
        request = dict(actor_role="worker", request_id=f"r{i}", request_ref={"sha256": str(i) * 64},
                       input_manifest_ref={"sha256": SHA})
        requests.append(request)
        funcs = [dict(id=f"c{i}-{j}", function={"name": name, "arguments": "{}"}) for j, name in enumerate(calls)]
        finishes[f"r{i}"] = dict(status="response", delivery_status="response_received", response_ref={"sha256": f"reply{i}"})
        blobs[f"reply{i}"] = {"choices": [dict(finish_reason="stop", message={"tool_calls": funcs})]}
        blobs[str(i) * 64] = {"messages": []}
    return SimpleNamespace(requests=requests, finishes=finishes, blobs=SimpleNamespace(get=lambda ref: blobs[ref["sha256"]]),
                           visible=Mock(return_value=spans), returns=returns or {})


class TrajectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = Draft202012Validator(json.loads((ROOT / "schemas/trajectory-outcomes-v1.schema.json").read_bytes()))

    def test_ungraded_observed_effect_and_unstarted_are_kept_separate(self):
        rows = [source(), reader(1), reader(2, status="ungraded"), reader(3, status="not_started")]
        result = report(rows)
        self.schema.validate(result)
        self.schema.validate(compact_report(result))
        self.assertEqual(result["observations_sha256"], digest(canonical(rows)))
        condition = next(c for c in result["by_condition"] if c["phase"] == "solve")
        self.assertEqual((condition["assigned"], condition["started"], condition["graded"], condition["official_successes"]),
                         (3, 2, 1, 1))
        cells = condition["progress"]
        self.assertEqual(cells["ungraded"]["metrics"]["opened"], dict(total=1, nonzero=1, observed=1))
        self.assertEqual(cells["not_started"]["metrics"]["opened"], dict(total=None, nonzero=None, observed=0))
        broken = deepcopy(rows)
        broken[2]["official_success"] = False
        with self.assertRaisesRegex(ContractError, "grade"):
            report(broken)
        self.assertFalse(self.schema.is_valid(dict(result, observations=broken)))
        with self.assertRaisesRegex(ContractError, "Unstarted"):
            report([*rows[:-1], dict(rows[-1], progress={"opened": 0})])

    def test_same_bank_and_same_first_request_compares_actions_without_transport_ids(self):
        a, b, c = reader(1), reader(2), reader(3)
        def groups(*trials):
            return report([source(), *trials])["same_input_repetitions"]["groups"]
        self.assertEqual(groups(a, b, c)["changed_functions_or_arguments"], 0)
        b["decision"]["calls"][0]["arguments"]["number"] = 2
        self.assertEqual(groups(a, b, c)["changed_functions_or_arguments"], 1)
        self.assertEqual(groups(a, b, c)["changed_function_sequence"], 0)
        self.assertEqual(groups(a, b, dict(c, snapshot="other"))["different_inputs_or_banks"], 1)
        self.assertEqual(groups(a, b, dict(c, first_input_sha256="0"*64))["comparable"], 0)
        self.assertEqual(groups(a, b, reader(3, status="not_started"))["missing_decisions"], 1)
        with self.assertRaisesRegex(ContractError, "Duplicate"):
            groups(a, a)
        with self.assertRaisesRegex(ContractError, "does not belong"):
            groups(dict(a, source_run="other-source"))

    def test_source_check_counts_distinct_graded_clusters_and_known_opportunities(self):
        rows = [source(), source("q2", current=["document"]), source("q3", "c3", status="ungraded"),
                source("q4", "c4", current=None), source("q5", "c5", previous=None)]
        opportunities = source_opportunities(rows, {"document": "doc"})
        counts = opportunities["document"]
        self.assertEqual(counts["with_source_only_evidence"], 2)
        self.assertEqual(counts["graded_sources"]["with_source_evidence"], 3)
        rule = dict(cue="document", minimum_clusters=2, evidence="source_only")
        self.assertEqual(assess_opportunity(rule, opportunities),
                         dict(**rule, observed_clusters=1, eligible_graded_sources=4, unknown_graded_sources=2, met=False))
        self.assertTrue(assess_opportunity(dict(rule, evidence="source"), opportunities)["met"])
        self.assertIsNone(assess_opportunity(None, opportunities))
        for change in ({"cue": "hidden"}, {"minimum_clusters": True}, {"evidence": "reader_success"}):
            with self.assertRaises(ContractError):
                assess_opportunity(dict(rule, **change), opportunities)

    def test_first_business_input_counts_returned_receipts_and_verified_fields_separately(self):
        replies = [("memory_list",), ("memory_get",), ("check",)]
        returns = {"c0-0": ("list", {}), "c1-0": ("get", {})}
        log = decision_log(replies, returns=returns, spans=[dict(item_id="a", version_id="v", field="title")])
        path = decision_path(log)
        self.assertEqual(path["read_returns"], {"memory_list": 1, "memory_get": 1})
        self.assertEqual(path["decision"]["calls"], [dict(id="c2-0", name="check", arguments={})])
        self.assertEqual(path["decision"]["memory"], [dict(item="a", version="v", field="title")])
        self.assertEqual(log.visible.call_args.args[0]["request_id"], "r2")
        no_return = decision_path(decision_log([("memory_get",)]))
        self.assertEqual(no_return["reads_before_decision"], {"memory_get": 1})
        self.assertEqual(no_return["read_returns"], {})
        self.assertIsNone(no_return["decision"])
        mixed = decision_log([("memory_get", "check")])
        self.assertIsNone(decision_path(mixed)["decision"])
        with self.assertRaisesRegex(ContractError, "operation"):
            decision_path(decision_log(replies, returns={"c0-0": ("get", {})}))

    def test_native_search_counts_headers_and_linked_public_results(self):
        receipt = "1. Title\n   ID: doc_A\n   Score: 1.0000\n   Content: mentions doc_B"
        self.assertEqual(search_documents(receipt), ["doc_A"])
        self.assertEqual(search_documents("No matches found for pattern: A"), [])
        self.assertIsNone(search_documents("Error: doc_A"))
        self.assertIsNone(search_documents(receipt.replace("1. Title", "2. Title")))
        messages = [dict(role="assistant", tool_calls=[dict(id="a", name="grep", arguments={"pattern": "A"})]),
                    dict(role="tool", id="a", requestor="user", content=receipt),
                    dict(role="tool", id="a", requestor="assistant", content=receipt)]
        _, observed = _searches(messages)
        self.assertEqual(observed, {"a": dict(documents=["doc_A"], sha256=digest(receipt.encode()))})
        with self.assertRaisesRegex(ContractError, "Duplicate"):
            _searches(messages + [messages[-1]])

    def test_same_public_outcome_matches_across_call_ids_and_unobserved_returns_are_unknown(self):
        product = {"user_id": "customer", "account_type": "checking", "account_class": "Blue"}
        receipt = dict(call_id="earlier-id", arguments=product, outcome="opened", account_id="account")
        self.assertEqual(_opening_keys([receipt]), _opening_keys([dict(receipt, call_id="new-id")]))
        self.assertEqual(_opening_keys([receipt], [dict(product, account_class="Red")]), [])
        self.assertIsNone(_opening_keys([dict(receipt, outcome="unobserved")], [product]))

    def test_signed_batch_rejects_a_missing_nested_checksum(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            for name in ("allocations.json", "control.json"):
                (p / name).write_bytes(b"{}")
            close = canonical(dict(control_sha256=None, allocation_sha256=digest(b"{}")))
            (p / "completion.json").write_bytes(close)
            with self.assertRaises(ContractError):
                sealed_rows(p, digest(close))

    def test_archived_summaries_retain_real_progress_and_call_presence(self):
        directory = ROOT / "artifacts/analyses"
        bank = json.loads((directory / "20261126-banking-trajectory-summary.json").read_bytes())
        bfcl = json.loads((directory / "20261126-bfcl-trajectory-summary.json").read_bytes())
        for result in (bank, bfcl):
            self.schema.validate(result)
            self.assertNotIn("observations", result)
            self.assertIsNone(result["source_requirement"])
        self.assertEqual(bank["observation_count"], 1159)
        self.assertEqual(sum(c["started"] for c in bank["by_condition"]), 679)
        readers = [c for c in bank["by_condition"] if c["phase"] == "solve"]
        for status, expected in (("graded", 1), ("ungraded", 13)):
            self.assertEqual(sum(c["progress"][status]["metrics"]["target_accounts"]["total"] for c in readers), expected)
        self.assertEqual(bank["same_input_repetitions"]["metrics"]["first_search_hit"], dict(observed_groups=198, changed_groups=49))
        self.assertEqual(bank["source_opportunities"]["target_opening_return"]["with_source_evidence"], 0)
        self.assertEqual(bank["source_opportunities"]["required_document"]["graded_sources"]["with_source_only_evidence"], 11)
        cells = {c["condition"]: (c["decisions_with_body"], c["decisions_with_function"], c["official_successes"])
                 for c in bfcl["by_condition"]}
        self.assertEqual(cells, dict(no_memory=(0, 5, 7), preload=(40, 23, 33), on_demand=(0, 25, 35)))

    @unittest.skipUnless((ROOT / "runs/bfcl-read-initiation-list_only-01").is_dir(), "Optional local BFCL control recording")
    def test_recorded_bfcl_list_first_controls_have_body_and_title_in_the_following_decision(self):
        hashes = json.loads((ROOT / "artifacts/analyses/20261126-bfcl-read-path/initiation-list_only.json").read_bytes())
        for task, seal in hashes["control_journals_sha256"].items():
            log = RecordedDecisions(ROOT / "runs/bfcl-read-initiation-list_only-01" / task,
                                    task + "-fact-list-only-on_demand", seal)
            observed = decision_path(log)
            self.assertEqual(observed["reads_before_decision"], {"memory_list": 1, "memory_get": 1})
            self.assertEqual(observed["read_returns"], observed["reads_before_decision"])
            self.assertEqual({s["field"] for s in observed["decision"]["memory"]}, {"title", "body"})

    def test_source_requirement_stops_before_creating_or_sending_validation(self):
        from scripts import run_banking_policy_cycle as cycle
        rule = dict(cue="opening_return", minimum_clusters=1, evidence="source")
        spec = dict(source_study="absent", validation={"writer_policies": ["existing", "candidate"]},
                    source_opportunity_requirement=rule)
        with tempfile.TemporaryDirectory() as folder, patch.object(cycle.bank.matched, "run_study") as dispatch:
            for check in (None, {"assessment": dict(rule, met=False)}):
                with self.assertRaisesRegex(ContractError, "was not met"):
                    cycle.run_paired_validation(Path(folder), "validation", spec, {}, 0, {}, {}, [], {}, {},
                                                source_check=check)
            dispatch.assert_not_called()
            self.assertFalse(list(Path(folder).iterdir()))

    def test_whole_study_stop_is_durable_and_keeps_all_registered_reader_slots(self):
        from scripts.run_banking_policy_cycle import record_source_admission
        rule = dict(cue="document", minimum_clusters=2, evidence="source_only")
        sources = [source(), source("q2", "customer2", previous=[])]
        measured = trajectory_report("example", BINDING, sources, metrics={}, cues={"document": "doc"},
                                     sources_only=True, requirement=rule)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "validation"
            receipt = record_source_admission(folder, measured)
            schema = Draft202012Validator(json.loads((ROOT / "schemas/source-opportunity-v1.schema.json").read_bytes()))
            schema.validate(receipt)
            self.assertFalse(schema.is_valid(dict(receipt, decision="continue")))
            self.assertEqual((receipt["decision"], receipt["reason"]), ("stop", "source_evidence_below_minimum"))
            self.assertEqual(record_source_admission(folder, measured), receipt)
            self.assertEqual(json.loads((folder / "source-opportunity.json").read_bytes()), receipt)
            yes = dict(measured, source_requirement=dict(measured["source_requirement"], minimum_clusters=1, met=True))
            with self.assertRaisesRegex(ContractError, "Existing artifact"):
                record_source_admission(folder, yes)
            self.assertEqual(record_source_admission(Path(tmp) / "next-study", yes)["decision"], "continue")
            other = Path(tmp) / "already-started"
            other.mkdir()
            (other / "registration.json").write_text("{}")
            with self.assertRaisesRegex(ContractError, "no source admission"):
                record_source_admission(other, measured)
            self.assertFalse((other / "source-opportunity.json").exists())
        spec = dict(reader="preload", repetitions=dict(writer_draws=2, readers_per_bank=3),
                    validation=dict(writer_policies=["existing", "candidate"], solve=120))
        slots = _stopped_readers(spec, {r["task"]: r for r in sources}, receipt["reason"])
        result = report(sources + slots)
        self.assertEqual(len(slots), 120)
        self.assertTrue(all(r["status"] == "not_started" and r["official_success"] is None for r in slots))
        self.assertEqual(sum(c["started"] for c in result["by_condition"] if c["phase"] == "solve"), 0)

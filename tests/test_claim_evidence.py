"""Check source, version, timing and counterfactual boundaries of the offline evidence report."""

import unittest

from agent_memory_eval.analysis.claim_evidence import _citation, _current_citation, _exposed_clause, paired_counts, trace_claim
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.recording.journal import read_experiment
from scripts.analyze_memory_claims import SPEC, airline_groups, analyze, original_source
from test_snapshot_replay import SnapshotReplayFixtureMixin


class ClaimEvidenceTests(SnapshotReplayFixtureMixin, unittest.TestCase):
    def test_public_source_and_exact_version_link_to_specific_reply(self):
        f = self.fixture()
        replay = f.replay()
        events = f.journal.events.values()
        req = next(e["payload"]["request_id"] for e in events if e["run_id"] == replay["run_id"]
                   and e["type"] == "model.request_started")
        store = next(e["payload"] for e in events if e["type"] == "memory.stored")
        args = dict(item=store["item_id"], version=store["version_id"], field="body", clause="Ask for needed baggage",
                    source_citations=[("/experience/messages/0/content", "No bags, please.")],
                    request_id=req, action_citations=[("/content", "How many bags are needed?")])
        result = trace_claim(f.target, f.target, **args)
        self.assertEqual(result["writing"]["feedback"]["level"], "none")
        self.assertEqual(result["writing"]["source_citations"][0]["role"], "user")
        source = original_source(f.source, result["writing"])
        self.assertIs(source["analyst_official_success"], False)
        with self.assertRaises(ContractError):
            original_source(f.source, result["writing"] | {"public_context_sha256": "wrong-transcript"})
        self.assertEqual(result["decision"]["record_kind"], "illustrative")
        self.assertEqual(result["decision"]["writer_lineage"], "writer_owned_snapshot")
        self.assertEqual([s["source"]["kind"] for s in result["decision"]["exposure"]], ["memory_snapshot"])
        for change in (dict(version="v_unrelated"), dict(field="title"), dict(action_citations=[("/reasoning", "PRIVATE")]),
                       dict(source_citations=[("/feedback/success", "False")])):
            with self.subTest(change=change), self.assertRaises(ContractError):
                trace_claim(f.target, f.target, **(args | change))

    def test_current_input_checks_source_even_if_the_role_says_user(self):
        wire = {"messages": [{"role": "user", "content": "Order O-7"}]}
        bindings = [{"pointer": "/messages/0", "source_id": "s1"}]
        for kind in ("memory_snapshot", "memory_return", "self_authored", "template", "benchmark_message"):
            source = {"s1": dict(kind=kind, producer_event_id="e1")}
            with self.subTest(kind=kind):
                args = wire, bindings, source, "/messages/0/content", "O-7"
                if kind == "benchmark_message":
                    self.assertEqual(_current_citation(*args)["producer_event"], "e1")
                else:
                    with self.assertRaises(ContractError):
                        _current_citation(*args)

    def test_citations_require_a_unique_literal_in_a_specific_field(self):
        doc = {"messages": ["공항 A 공항 B"]}
        c = _citation(doc, "/messages/0", "공항 B")
        self.assertEqual(doc["messages"][0][c["char_start"]:c["char_end"]], "공항 B")
        for location, quote in (("/messages/0", "공항"), ("/messages/0", ""), ("/messages/1", "B"),
                                ("/messages/-1", "B"), ("/messages/00", "B"), ("/messages/0", "C")):
            with self.subTest(location=location, quote=quote), self.assertRaises(ContractError):
                _citation(doc, location, quote)

    def test_missing_and_empty_do_not_imply_failure(self):
        self.assertEqual(paired_counts([(False, False), (True, False), (False, True), (None, True), (None, None)]),
                         dict(assigned=5, gain=1, loss=1, both_failure=1, both_success=0, missing=2))
        with self.assertRaises(ContractError):
            paired_counts([("False", False)])
        with self.assertRaises(ContractError):
            airline_groups(dict(airline_scores="task_id,repetition,condition\nT,1,no_memory\n",
                                airline_writing={"annotations": []}))

    def test_worker_clause_offsets_account_for_escaped_quotes_and_newlines(self):
        text = '전날 "A"\n오늘 "B"'
        quote = '오늘 "B"'
        rendered = canonical(text).decode()[1:-1]
        wire = dict(content='{"body":"' + rendered + '"}')
        begin = wire["content"].index(rendered)
        span = dict(source={"kind": "memory_return"}, pointer="/content", char_start=begin,
                    char_end=begin + len(rendered), rendered_sha256=digest(rendered.encode()))
        note = _citation(dict(body=text), "/body", quote)
        exposed = _exposed_clause(wire, span, text, note)
        raw = wire["content"][exposed["clause_char_start"]:exposed["clause_char_end"]]
        self.assertEqual(read_json(('"' + raw + '"').encode()), quote)

    def test_published_aggregate_is_reproducible_without_raw_runs(self):
        report = read_json((ROOT / "artifacts/analyses/20261123-memory-claim-evidence.json").read_bytes())
        summary = analyze(ROOT, cohorts_only=True)
        self.assertEqual(summary, {k: report[k] for k in summary})
        self.assertEqual(summary["airline_second_pass"]["all"]["assigned"], 224)
        self.assertEqual(summary["retail_first_decisions"]["all"]["assigned"], 154)

    def test_read_requested_in_reply_is_not_a_prior_exposure(self):
        case = read_json((ROOT / SPEC).read_bytes())["cases"][0]
        writer, worker = [(ROOT / case[k]["path"]).parent for k in ("writer", "worker")]
        if not (writer / "log.jsonl").exists() or not (worker / "log.jsonl").exists():
            self.skipTest("Archived retail journals are stored outside Git")
        scan, _, _ = read_experiment(worker)
        requests = [e["payload"]["request_id"] for e in scan["events"].values() if e["type"] == "model.request_started"]
        args = {k: case[k] for k in ("item", "version", "field", "clause", "source_citations")}
        before = trace_claim(writer, worker, **args, request_id=requests[0],
                             action_citations=[("/tool_calls/0/function/name", "memory_list")])
        after = trace_claim(writer, worker, **args, request_id=case["request_id"])
        self.assertEqual(before["decision"]["exposure"], [])
        self.assertEqual([s["source"]["kind"] for s in after["decision"]["exposure"]], ["memory_return"])

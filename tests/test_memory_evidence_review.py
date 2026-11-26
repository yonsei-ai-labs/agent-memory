"""Offline regressions for missing grades, blinded semantic evidence and independent review calibration."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jsonschema import Draft202012Validator

from agent_memory_eval.analysis.selection import allocation_exposure, worker_exposure
from agent_memory_eval.analysis.blind_review import blinding_check, compare_sealed_reviews, export_public, public_bundle, seal_review
from agent_memory_eval.analysis.semantic_review import RUBRIC, compare_reviews, judge_messages, review_packet, validate_reviews
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT


def read(path):
    return read_json((ROOT / path).read_bytes())


class SemanticReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = read("tests/fixtures/semantic-memory-review-v1.json")
        self.packet = review_packet(**fixture["evidence"])
        self.key = self.packet["packet_id"]
        self.packets = {self.key: self.packet}
        self.human = fixture["submission"]
        self.model = deepcopy(self.human)
        self.model["reviewer"] = dict(kind="llm", id="judge-A", model="fixture", provider="local", revision="1", settings={})
        row = self.model["reviews"][0]
        row.update(response_id="fixture-reply-1", response_text=canonical(row.pop("ratings")).decode())

    def test_schemas_match_working_human_and_original_model_responses(self):
        schema = Draft202012Validator(read("schemas/semantic-memory-ratings-v1.schema.json"))
        schema.check_schema(schema.schema)
        for submission in (self.human, self.model):
            schema.validate(submission)
            checked = validate_reviews(self.packets, submission)[self.key]
            cite, = checked["ratings"]["source_support"]["citations"]
            text = self.packet["evidence"]["source"]["messages"][0]["content"]
            self.assertEqual(text[cite["char_start"]:cite["char_end"]], cite["quote"])
        self.assertEqual(checked["response_sha256"], digest(self.model["reviews"][0]["response_text"].encode()))
        receipt = Draft202012Validator(read("schemas/form-result-feedback-v2.schema.json"))
        receipt.validate(read("tests/fixtures/semantic-memory-review-v1.json")["receipt"])
        self.assertFalse(receipt.is_valid(dict(outcome_event_id="e", success=0)))
        self.assertFalse(receipt.is_valid(dict(outcome_event_id="e", success=False, reason="private")))

    def test_rejects_unbound_input_tampered_packet_and_duplicate_response(self):
        changed = deepcopy(self.packet)
        changed["evidence"]["claim"] = "extra"
        with self.assertRaisesRegex(ContractError, "changed"):
            judge_messages(changed)
        bad = deepcopy(self.model)
        bad["reviews"][0]["input_sha256"] = "0"*64
        with self.assertRaisesRegex(ContractError, "Different judge input"):
            validate_reviews(self.packets, bad)
        other = review_packet(**dict(self.packet["evidence"], claim="Another claim"))
        self.packets[other["packet_id"]] = other
        self.model["reviews"].append(dict(self.model["reviews"][0], packet_id=other["packet_id"],
                                         input_sha256=digest(canonical(judge_messages(other)))))
        with self.assertRaisesRegex(ContractError, "response ID"):
            validate_reviews(self.packets, self.model)

    def test_false_ambiguous_out_of_section_and_missing_quotes_fail_closed(self):
        changes = [
            {"pointer":"/source/messages/0/content", "quote":"never happened"},
            {"pointer":"/source/messages/0/content", "quote":"e"},
            {"pointer":"/current/messages/0/content", "quote":"Please"},
            {"pointer":"/source/messages/8/content", "quote":"missing"}
        ]
        for citations in [[], *([v] for v in changes)]:
            with self.subTest(citations=citations), self.assertRaises(ContractError):
                bad = deepcopy(self.human)
                bad["reviews"][0]["ratings"]["source_support"]["citations"] = citations
                validate_reviews(self.packets, bad)

    def test_unknown_label_fabricated_parsed_rating_and_unidentified_reviewer_are_rejected(self):
        bad = deepcopy(self.model)
        bad["reviews"][0]["ratings"] = self.human["reviews"][0]["ratings"]
        with self.assertRaises(ContractError):
            validate_reviews(self.packets, bad)
        del bad["reviews"][0]["ratings"]
        del bad["reviewer"]["revision"]
        with self.assertRaises(ContractError):
            validate_reviews(self.packets, bad)
        self.human["reviews"][0]["ratings"]["action_alignment"]["label"] = "caused_success"
        with self.assertRaises(ContractError):
            validate_reviews(self.packets, self.human)

    def test_calibration_retains_abstentions_disagreements_and_unpaired_packets(self):
        other = review_packet(**dict(self.packet["evidence"], action={"text":"wait"}))
        self.packets[other["packet_id"]] = other
        self.human["reviews"][0]["ratings"]["action_alignment"] = dict(label="unclear", citations=[])
        report = compare_reviews(self.packets, self.model, self.human)
        self.assertEqual((report["unique_packets"], report["reviewed"], report["paired"]), (2,1,1))
        axis = report["axes"]["action_alignment"]
        self.assertEqual((axis["both_decisive"], axis["disagreements"]), (0, [self.key]))
        self.assertEqual(axis["confusion"], [dict(rating="consistent", reference="unclear", count=1)])
        self.assertEqual(set(report["axes"]), set(RUBRIC))
        with self.assertRaisesRegex(ContractError, "separately identified"):
            compare_reviews(self.packets, self.human, self.human)

    def test_reviewer_gets_only_public_inputs_and_guesses_are_sealed_before_the_join(self):
        bundle = public_bundle(self.packets)
        with TemporaryDirectory() as folder:
            dest = Path(folder) / "reviewer"
            export_public(dest, self.packets)
            self.assertEqual([p.name for p in dest.iterdir()], ["inputs.json"])
            self.assertEqual(read_json((dest / "inputs.json").read_bytes()), bundle)
            with self.assertRaises(FileExistsError):
                export_public(dest, self.packets)
        sealed = seal_review(bundle, self.human, {self.key: "preload"})
        scored = blinding_check(bundle, sealed, {self.key: {"preload"}})
        self.assertEqual((scored["guessed"], scored["accuracy"]), (1, 1))
        self.assertEqual(blinding_check(bundle, sealed, {self.key: {"preload", "on_demand"}})["shared_conditions"], 1)
        abstained = seal_review(bundle, self.human, {self.key: None})
        self.assertIsNone(blinding_check(bundle, abstained, {self.key: {"preload"}})["accuracy"])
        model = seal_review(bundle, self.model, {self.key: None})
        self.assertEqual(compare_sealed_reviews(bundle, sealed, model)["axes"]["source_support"]["agree"], 1)
        with self.assertRaises(ContractError):
            compare_sealed_reviews(bundle, sealed, sealed)
        duplicate = deepcopy(self.model)
        duplicate["reviewer"]["id"] = "different-id"
        with self.assertRaisesRegex(ContractError, "reuse"):
            compare_sealed_reviews(bundle, model, seal_review(bundle, duplicate, {self.key: None}))
        sealed["condition_guesses"][self.key] = None
        with self.assertRaises(ContractError):
            blinding_check(bundle, sealed, {self.key: {"preload"}})
        with self.assertRaises(ContractError):
            seal_review(bundle, self.human, {})


class ExposureCoverageTests(unittest.TestCase):
    def test_feedback_contrasts_pair_within_task_form_and_delivery_and_keep_missing(self):
        from scripts.analyze_banking_memory import feedback_contrasts

        archived = read("artifacts/analyses/20261124-banking-required-results.json")["trials"]
        changed = deepcopy(archived)
        sample = [r for r in changed if r["arm"].endswith("__preload")]
        lost = next(r for r in sample if r["success"])
        gained = next(r for r in sample if not r["success"])
        missing = next(r for r in sample if not r["success"] and r is not gained)
        lost["success"], gained["success"], missing["success"] = False, True, None
        stats = feedback_contrasts(changed, archived)["by_delivery"]["preload"]
        self.assertEqual((stats["paired"], stats["none_success"], stats["result_only_success"]), (19,8,8))
        self.assertEqual([stats["outcomes"][k] for k in ("gain","loss","missing")], [1,1,1])
        self.assertEqual(stats["joint_success_effort"]["tokens"]["pairs"], 7)
        with self.assertRaises(ContractError):
            feedback_contrasts(changed[:-1], archived)

    def test_feedback_note_annotations_retain_missing_and_require_authored_quotes(self):
        from scripts.review_banking_evidence import note_ratings

        docs = {"n": dict(endpoint="hand off", items=[dict(title="Observed result", body="The case was unresolved.")])}
        mapping = [dict(feedback=k, first_pass_success=False, note_id=n) for k,n in (("none","n"), ("result_only",None))]
        rating = dict(endpoint=dict(label="limited",citations=[dict(pointer="/items/0/body",quote="unresolved")]),
                      overall_result=dict(label="unstated",citations=[]))
        submission = dict(format="banking-feedback-note-ratings/v1", reviewer="fixture", packet_sha256=digest(canonical(docs)),
                          ratings={"n":rating})
        result = note_ratings(docs,mapping,submission)
        self.assertEqual(result["summary"]["none"]["failure"]["endpoint"], {"limited":1})
        self.assertEqual(result["summary"]["result_only"]["failure"]["reviewed"], 0)
        rating["endpoint"]["citations"] = [dict(pointer="/endpoint",quote="hand off")]
        with self.assertRaises(ContractError):
            note_ratings(docs,mapping,submission)

    def test_missing_grade_does_not_erase_visible_input_or_confirm_unobserved_delivery(self):
        def step(status, delivery, fields=(), reads=()):
            return dict(status=status, delivery=delivery, visible_memory=[dict(field=f) for f in fields],
                        reads=[dict(name=n) for n in reads])
        partial = worker_exposure([step("response", "submitted", ["title"], ["memory_get"]),
                                   step("error", "unknown", ["title","body"])])
        never_sent = worker_exposure([step("error", "not_submitted", ["body"])])
        seen = worker_exposure([step("response", "submitted", ["title","body","body"], ["memory_list"])])
        self.assertLess(list(seen["counts"]).index("title_requests"), list(seen["counts"]).index("body_requests"))
        coverage = allocation_exposure([(None, partial), (None, never_sent), (False, seen),
                                        (None, dict(state="not_started")), (None, dict(state="unavailable"))])
        self.assertEqual(coverage["fields"]["body"], dict(confirmed=1, absent=2, unknown=2))
        self.assertEqual(coverage["fields"]["title"], dict(confirmed=2, absent=2, unknown=1))
        self.assertEqual((coverage["graded"], coverage["ungraded"]), (1,4))
        self.assertEqual(coverage["counts"], dict(worker_requests=4, worker_responses=2, not_submitted_requests=1,
                         unresolved_requests=1, title_requests=2, body_requests=1, memory_list=1, memory_get=1))

    def test_held_out_banking_grade_keeps_all_twenty_exposed_allocations(self):
        from scripts.analyze_banking_memory import summarize

        results = read("artifacts/analyses/20261124-banking-required-results.json")
        row = next(t for t in results["trials"] if t["arm"].endswith("__preload"))
        row["success"] = None
        stats = summarize(results["sources"], results["writers"], results["trials"])["by_delivery"]["preload"]
        self.assertEqual((stats["graded"], stats["with_body"], stats["outcomes"]["missing"]), (19,20,1))

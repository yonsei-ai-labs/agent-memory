import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from scripts.analyze_retail_handoff_factors import (
    describe,
    initiator_label,
    note_group,
    note_label,
    outcome_kind,
    paired,
    read_run,
    summarize,
)


def note(title, body):
    return {"id": "m_test", "title": title, "body": body}


class OutcomeKindTests(unittest.TestCase):
    def test_failed_runs_are_split_by_database_verdict_then_handoff_position(self):
        cases = [
            ((True, True, "no_transfer"), "success"),
            ((True, False, "transfer_without_successful_change"), "success"),
            ((False, True, "no_transfer"), "explanation_only"),
            ((False, True, "transfer_after_change"), "explanation_only"),
            ((False, False, "transfer_without_successful_change"), "early_handoff"),
            ((False, False, "transfer_after_change"), "handoff_with_changes"),
            ((False, False, "transfer_before_first_change"), "handoff_with_changes"),
            ((False, False, "no_transfer"), "database_mismatch"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(outcome_kind(*args), expected)

    def test_ungraded_run_is_never_given_a_failure_kind(self):
        with self.assertRaises(ContractError):
            outcome_kind(None, None, "transfer_without_successful_change")

    def test_failed_run_without_database_verdict_is_rejected(self):
        with self.assertRaises(ContractError):
            outcome_kind(False, None, "no_transfer")


def judged(mentions, stance=None, evidence=()):
    return {"mentions_handoff": mentions, "stance": stance, "evidence": list(evidence)}


class NoteGroupTests(unittest.TestCase):
    def test_arm_and_the_judgement_of_the_delivered_note_decide_the_group(self):
        plain = note("Retail return", "Verify delivered status and payment history.")
        handed = note("Order change", "The agent transferred to a human.")
        other = note("Address", "Transferable caution: check the order address.")
        cases = [
            (("no_memory", None, None), "no_memory"),
            (("empty_preload", [], None), "empty_preload"),
            (("lesson_original", [], None), "writer_empty"),
            (("fact_explicit", [plain], None), "note_plain"),
            (
                ("episode_original", [handed], judged(True, "records")),
                "note_escalation",
            ),
            (("lesson_explicit", [other], judged(False)), "note_plain"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(note_group(*args), expected)

    def test_note_delivered_to_a_control_run_is_rejected(self):
        leaked = [note("Past order", "The return was requested.")]
        for arm in ("no_memory", "empty_preload"):
            with self.subTest(arm=arm), self.assertRaises(ContractError):
                note_group(arm, leaked, None)

    def test_note_arm_without_a_preload_record_is_rejected(self):
        with self.assertRaises(ContractError):
            note_group("fact_original", None, None)


class NoteLabelTests(unittest.TestCase):
    HANDED = ({"title": "Order change", "body": "The agent transferred to a human."},)
    PLAIN = ({"title": "Retail return", "body": "Verify the delivered status."},)

    def labels(self, label):
        return {digest(canonical(list(self.HANDED))): label}

    def test_judged_note_returns_its_label(self):
        label = judged(True, "records", ["The agent transferred to a human."])
        self.assertEqual(note_label(list(self.HANDED), self.labels(label)), label)

    def test_note_without_handoff_wording_needs_no_judgement(self):
        self.assertIsNone(note_label(list(self.PLAIN), {}))
        self.assertIsNone(note_label([], {}))

    def test_note_with_handoff_wording_must_have_been_judged(self):
        with self.assertRaises(ContractError):
            note_label(list(self.HANDED), {})

    def test_evidence_that_is_not_in_the_note_is_rejected(self):
        label = judged(True, "recommends", ["Always transfer to a human."])
        with self.assertRaises(ContractError):
            note_label(list(self.HANDED), self.labels(label))

    def test_stance_must_agree_with_the_mention_verdict(self):
        for label in (
            judged(False, "records"),
            judged(True, None, ["The agent transferred to a human."]),
            judged(True, "approves", ["The agent transferred to a human."]),
        ):
            with self.subTest(label=label), self.assertRaises(ContractError):
                note_label(list(self.HANDED), self.labels(label))


def said(role, content, calls=None):
    return {"role": role, "content": content, "tool_calls": calls}


TRANSFER = [
    {"id": "c1", "name": "transfer_to_human_agents", "arguments": {"summary": "s"}}
]


def raised(label, index=None, quote=""):
    return {"label": label, "message_index": index, "quote": quote}


class InitiatorLabelTests(unittest.TestCase):
    MESSAGES = (
        said("assistant", "Hi! How can I help you today?"),
        said("user", "I lost my tablet. Can I talk to someone who can refund it?"),
        said("assistant", '{"message":"I can connect you with a human agent."}'),
        said("assistant", None, TRANSFER),
        said("user", "Thanks, a supervisor is what I wanted."),
    )

    def check(self, label):
        return initiator_label(list(self.MESSAGES), 3, label)

    def test_judgement_is_accepted_when_its_quote_is_in_the_cited_message(self):
        cases = [
            (
                raised("customer", 1, "Can I talk to someone who can refund it?"),
                "customer",
            ),
            (raised("agent", 2, "connect you with a human agent"), "agent"),
            (raised("unannounced"), "unannounced"),
        ]
        for label, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(self.check(label), expected)

    def test_quote_missing_from_the_cited_message_is_rejected(self):
        with self.assertRaises(ContractError):
            self.check(raised("customer", 1, "Please get me a supervisor."))

    def test_evidence_from_the_wrong_speaker_is_rejected(self):
        for label in (
            raised("customer", 2, "connect you with a human agent"),
            raised("agent", 1, "Can I talk to someone"),
        ):
            with self.subTest(label=label), self.assertRaises(ContractError):
                self.check(label)

    def test_evidence_at_or_after_the_handoff_is_rejected(self):
        with self.assertRaises(ContractError):
            self.check(raised("customer", 4, "a supervisor is what I wanted"))

    def test_unknown_or_unsupported_label_is_rejected(self):
        for label in (
            raised("simulator", 1, "Can I talk"),
            raised("unannounced", 1, "x"),
        ):
            with self.subTest(label=label), self.assertRaises(ContractError):
                self.check(label)


def run(customer, task, stage, group, official, study="S"):
    return {
        "study": study,
        "customer": customer,
        "task": task,
        "stage": stage,
        "group": group,
        "official": official,
    }


class PairedTests(unittest.TestCase):
    CELLS = (
        run("c1", "1", "solve1", "note", True),
        run("c1", "1", "solve1", "note", False),
        run("c1", "1", "solve1", "none", False),
        run("c1", "1", "solve2", "note", True),
        run("c1", "1", "solve2", "none", True),
        run("c1", "2", "solve1", "note", False),
        run("c1", "2", "solve1", "none", True),
        run("c2", "3", "solve1", "note", True),
        run("c2", "3", "solve1", "none", False),
        run("c2", "3", "solve2", "note", True),
        run("c2", "3", "solve2", "none", None),
        run("c3", "4", "solve1", "note", True),
        run("c3", "4", "solve2", "none", False),
        run("c4", "9", "solve1", "note", True, study="B"),
        run("c4", "9", "solve1", "none", False, study="C"),
    )

    def test_runs_pair_only_inside_one_study_task_and_repeat_and_customers_weigh_equally(
        self,
    ):
        result = paired(self.CELLS, {"note"}, {"none"}, lambda cell: cell["official"])
        self.assertEqual(
            {
                k: result[k]
                for k in ("pairs", "left_only", "right_only", "both", "neither")
            },
            {"pairs": 5, "left_only": 2, "right_only": 1, "both": 1, "neither": 1},
        )
        self.assertEqual((result["scored_tasks"], result["scored_customers"]), (3, 2))
        # c1: mean of task 1 (+1, 0, 0 -> 1/3) and task 2 (-1) = -1/3; c2: +1.
        self.assertAlmostEqual(result["customer_difference"], 1 / 3)
        self.assertEqual(result["customer_interval"], [-0.333333, 1.0])

    def test_single_customer_has_a_difference_but_no_interval(self):
        cells = [c for c in self.CELLS if c["customer"] == "c2"]
        result = paired(cells, {"note"}, {"none"}, lambda cell: cell["official"])
        self.assertEqual((result["pairs"], result["customer_difference"]), (1, 1))
        self.assertIsNone(result["customer_interval"])

    def test_comparison_without_any_scored_pair_reports_no_estimate(self):
        cells = [c for c in self.CELLS if c["customer"] in ("c3", "c4")]
        result = paired(cells, {"note"}, {"none"}, lambda cell: cell["official"])
        self.assertEqual((result["pairs"], result["scored_customers"]), (0, 0))
        self.assertIsNone(result["customer_difference"])


class ReadRunTests(unittest.TestCase):
    JOURNAL = "runs/study/task-6/solve1/jobs/0001/log.jsonl"
    MESSAGES = (
        {
            "role": "assistant",
            "content": "Hi! How can I help you today?",
            "tool_calls": None,
        },
        {"role": "user", "content": "I want to return a lamp.", "tool_calls": None},
    )

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.workspace = Path(folder.name)
        self.job = self.workspace / self.JOURNAL
        self.job = self.job.parent
        (self.job / "blobs/sha256").mkdir(parents=True)

    def blob(self, value):
        raw = json.dumps(value).encode()
        sha = hashlib.sha256(raw).hexdigest()
        (self.job / "blobs/sha256" / sha).write_bytes(raw)
        return {
            "media_type": "application/json",
            "path": f"blobs/sha256/{sha}",
            "sha256": sha,
        }

    def preload(self, run, items):
        message = {"role": "user", "content": json.dumps({"items": items})}
        return {
            "type": "memory.preloaded",
            "run_id": run,
            "payload": {"bank_id": "b_1", "public_message_ref": self.blob(message)},
        }

    def scored(self, run, report):
        payload = {
            "grading_status": "graded",
            "official_score": 1.0,
            "official_success": True,
        }
        return {
            "type": "outcome.scored",
            "run_id": run,
            "payload": {**payload, "official_artifact_ref": self.blob(report)},
        }

    def cell(self, events, run="r_1"):
        records = [{"commit_seq": 1, "events": []}, {"commit_seq": 2, "events": events}]
        raw = "".join(json.dumps(record) + "\n" for record in records).encode()
        (self.job / "log.jsonl").write_bytes(raw)
        return {
            "run": run,
            "journal": self.JOURNAL,
            "journal_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def test_returns_this_runs_delivered_notes_and_graded_trajectory(self):
        mine = [
            {
                "id": "m_1",
                "title": "Past exchange",
                "body": "The agent transferred the customer.",
            }
        ]
        other = [{"id": "m_2", "title": "Other customer", "body": "Unrelated note."}]
        report = {"task_id": "6", "messages": list(self.MESSAGES)}
        cell = self.cell(
            [
                self.preload("r_other", other),
                self.preload("r_1", mine),
                self.scored("r_other", {"task_id": "7", "messages": []}),
                self.scored("r_1", report),
            ]
        )
        items, trajectory = read_run(self.workspace, cell)
        self.assertEqual(
            items,
            [{"title": "Past exchange", "body": "The agent transferred the customer."}],
        )
        self.assertEqual(trajectory, report)

    def test_ungraded_run_keeps_its_notes_and_has_no_trajectory(self):
        cell = self.cell([self.preload("r_1", [])])
        self.assertEqual(read_run(self.workspace, cell), ([], None))

    def test_run_without_preload_has_no_delivery_record(self):
        cell = self.cell(
            [self.scored("r_1", {"task_id": "6", "messages": list(self.MESSAGES)})]
        )
        items, trajectory = read_run(self.workspace, cell)
        self.assertIsNone(items)
        self.assertEqual(trajectory["messages"], list(self.MESSAGES))

    def test_journal_changed_after_the_official_analysis_is_rejected(self):
        cell = self.cell([self.preload("r_1", [])])
        with (self.job / "log.jsonl").open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaises(ContractError):
            read_run(self.workspace, cell)

    def test_note_blob_that_differs_from_its_recorded_hash_is_rejected(self):
        event = self.preload(
            "r_1", [{"id": "m_1", "title": "Past", "body": "Original."}]
        )
        cell = self.cell([event])
        (self.job / event["payload"]["public_message_ref"]["path"]).write_bytes(b"{}")
        with self.assertRaises(ContractError):
            read_run(self.workspace, cell)


def call(identifier, name, **arguments):
    return {
        "id": identifier,
        "name": name,
        "arguments": arguments,
        "requestor": "assistant",
    }


def reply(identifier, error=False):
    return {"role": "tool", "id": identifier, "content": "{}", "error": error}


EARLY = "transfer_without_successful_change"
AUDITED = {
    "task": "31",
    "stage": "solve2",
    "customer": "c1",
    "arm": "lesson_original",
    "run": "r_1",
    "journal": "runs/study/task-31/solve2/jobs/0007/log.jsonl",
    "official": False,
    "grade": {"db_match": False, "nl": []},
}
TRAJECTORY = {
    "task_id": "31",
    "messages": [
        said("assistant", "Hi! How can I help you today?"),
        said("user", "My tablet was lost after delivery. Can I get a refund?"),
        said("assistant", None, [call("a1", "find_user_id_by_name_zip", zip="1")]),
        reply("a1"),
        said(
            "assistant",
            None,
            [call("a2", "return_delivered_order_items", order_id="#W1")],
        ),
        reply("a2", error=True),
        said(
            "assistant",
            None,
            [call("a3", "transfer_to_human_agents", summary="lost tablet")],
        ),
        reply("a3"),
        said("assistant", '{"message":"YOU ARE BEING TRANSFERRED TO A HUMAN AGENT."}'),
    ],
    "reward_info": {
        "action_checks": [
            {"action": {"name": "find_user_id_by_name_zip"}},
            {"action": {"name": "cancel_pending_order"}},
        ]
    },
}
NOTES = [
    {"title": "Lost delivery", "body": "The agent transferred the customer to a human."}
]
ANNOTATIONS = {
    "notes": {
        digest(canonical(NOTES)): {
            "mentions_handoff": True,
            "stance": "records",
            "evidence": ["The agent transferred the customer to a human."],
        }
    },
    "initiators": {"r_1": {"label": "unannounced", "message_index": None, "quote": ""}},
}


class DescribeTests(unittest.TestCase):
    def test_failed_run_that_only_handed_off_is_an_unannounced_early_handoff(self):
        row = describe("related_transfer", 2, AUDITED, NOTES, TRAJECTORY, ANNOTATIONS)
        self.assertEqual(
            {
                k: row[k]
                for k in (
                    "study",
                    "repeat",
                    "condition",
                    "group",
                    "stance",
                    "position",
                    "kind",
                    "initiator",
                )
            },
            {
                "study": "related_transfer",
                "repeat": 2,
                "condition": "memo",
                "group": "note_escalation",
                "stance": "records",
                "position": "transfer_without_successful_change",
                "kind": "early_handoff",
                "initiator": "unannounced",
            },
        )
        self.assertIs(row["reference_changes"], True)
        self.assertEqual(row["job"], 7)
        self.assertRegex(row["note_sha256"], r"^[0-9a-f]{64}$")

    def test_handoff_without_a_judgement_has_no_initiator(self):
        unjudged = {**ANNOTATIONS, "initiators": {}}
        row = describe("related_transfer", 2, AUDITED, NOTES, TRAJECTORY, unjudged)
        self.assertEqual((row["position"], row["initiator"]), (EARLY, None))

    def test_source_is_the_donor_task_or_the_task_itself(self):
        donor = describe(
            "related_transfer",
            2,
            {**AUDITED, "source": "32"},
            NOTES,
            TRAJECTORY,
            ANNOTATIONS,
        )
        own = describe("same_task", 2, AUDITED, NOTES, TRAJECTORY, ANNOTATIONS)
        self.assertEqual((donor["source"], own["source"]), ("32", "31"))

    def test_control_run_has_no_source_even_when_the_audited_cell_names_none(self):
        for extra in ({}, {"source": None}):
            with self.subTest(extra=extra):
                audited = {**AUDITED, "arm": "no_memory", **extra}
                row = describe("same_task", 1, audited, None, TRAJECTORY, ANNOTATIONS)
                self.assertIsNone(row["source"])

    def test_only_a_successful_change_of_several_items_marks_the_tool_defect(self):
        def trajectory(items, error):
            messages = [
                said(
                    "assistant",
                    None,
                    [
                        call(
                            "b1",
                            "modify_pending_order_items",
                            item_ids=items,
                            new_item_ids=items,
                        )
                    ],
                ),
                reply("b1", error=error),
            ]
            return {**TRAJECTORY, "messages": messages}

        cases = [
            ((["1", "2"], False), True),
            ((["1"], False), False),
            ((["1", "2"], True), False),
        ]
        for (items, error), expected in cases:
            with self.subTest(items=items, error=error):
                row = describe(
                    "same_task",
                    0,
                    AUDITED,
                    NOTES,
                    trajectory(items, error),
                    ANNOTATIONS,
                )
                self.assertIs(row["multi_item_change"], expected)

    def test_successful_change_before_the_handoff_is_not_an_early_handoff(self):
        messages = [dict(m) for m in TRAJECTORY["messages"]]
        messages[5] = reply("a2")
        row = describe(
            "related_transfer",
            2,
            AUDITED,
            NOTES,
            {**TRAJECTORY, "messages": messages},
            ANNOTATIONS,
        )
        self.assertEqual(
            (row["position"], row["kind"]),
            ("transfer_after_change", "handoff_with_changes"),
        )

    def test_ungraded_run_keeps_its_group_without_behavior_or_outcome(self):
        audited = {**AUDITED, "official": None, "grade": None}
        row = describe("related_transfer", 2, audited, [], None, ANNOTATIONS)
        self.assertEqual(
            (row["group"], row["stance"], row["note_sha256"]),
            ("writer_empty", None, None),
        )
        self.assertEqual(
            (row["kind"], row["position"], row["initiator"], row["reference_changes"]),
            (None, None, None, None),
        )

    def test_trajectory_from_another_task_is_rejected(self):
        with self.assertRaises(ContractError):
            describe(
                "related_transfer",
                2,
                AUDITED,
                NOTES,
                {**TRAJECTORY, "task_id": "32"},
                ANNOTATIONS,
            )


def counts(runs, early=0, agent=0, customer=0, successes=0):
    return {
        "runs": runs,
        "early_handoffs": early,
        "agent_initiated": agent,
        "customer_requested": customer,
        "successes": successes,
    }


def row(
    task,
    customer,
    group,
    kind,
    *,
    repeat=1,
    changes=True,
    position="no_transfer",
    **extra,
):
    official = None if kind is None else kind == "success"
    condition = group if group in ("no_memory", "empty_preload") else "memo"
    return {
        "study": "same_task",
        "task": task,
        "stage": f"solve{repeat}" if repeat else "source",
        "repeat": repeat,
        "customer": customer,
        "arm": group,
        "condition": condition,
        "group": group,
        "official": official,
        "kind": kind,
        "position": position if kind else None,
        "changes_required": changes,
        "initiator": None,
        "stance": None,
        "note_sha256": None,
        "source_tool_defect": False,
        "job": 1,
        **extra,
    }


class SummarizeTests(unittest.TestCase):
    CELLS = (
        # T1: first pass failed; this unit contains an escalation note.
        {
            **row("T1", "c1", "no_memory", "database_mismatch", repeat=0),
            "official": False,
        },
        row("T1", "c1", "no_memory", "success"),
        row("T1", "c1", "empty_preload", "database_mismatch"),
        row(
            "T1",
            "c1",
            "note_escalation",
            "early_handoff",
            position=EARLY,
            initiator="unannounced",
            stance="recommends",
            note_sha256="e1",
        ),
        row("T1", "c1", "note_plain", "success", note_sha256="p1"),
        row("T1", "c1", "writer_empty", None),
        # T2: first pass succeeded; no escalation note.
        row("T2", "c2", "no_memory", "success", repeat=0),
        row("T2", "c2", "no_memory", "success"),
        row("T2", "c2", "note_plain", "explanation_only", note_sha256="p2"),
        row("T2", "c2", "note_plain", "success", note_sha256="p2"),
        # T3: the reference changes nothing, so an immediate handoff can be correct.
        row("T3", "c2", "no_memory", "success", repeat=0, changes=False),
        row(
            "T3",
            "c2",
            "no_memory",
            "success",
            changes=False,
            position=EARLY,
            initiator="customer",
        ),
        row(
            "T3",
            "c2",
            "note_escalation",
            "success",
            changes=False,
            position=EARLY,
            initiator="agent",
            stance="records",
            note_sha256="e3",
        ),
    )

    def setUp(self):
        self.summary = summarize(self.CELLS)

    def test_outcomes_count_only_graded_second_pass_runs(self):
        self.assertEqual(
            self.summary["outcomes"]["all"],
            {
                "no_memory": {
                    "graded": 3,
                    "early_handoff_runs": 1,
                    "kinds": {"success": 3},
                },
                "empty_preload": {
                    "graded": 1,
                    "early_handoff_runs": 0,
                    "kinds": {"database_mismatch": 1},
                },
                "memo": {
                    "graded": 5,
                    "early_handoff_runs": 2,
                    "kinds": {"early_handoff": 1, "explanation_only": 1, "success": 3},
                },
            },
        )
        self.assertEqual(
            self.summary["outcomes"]["same_task"], self.summary["outcomes"]["all"]
        )

    def test_early_handoff_is_compared_as_behavior_and_as_failure(self):
        early = self.summary["paired_early_handoff"]["all"]
        # T1: the escalation note run handed off early and failed; T3: both runs handed off and passed.
        behavior, failure = early["behavior"], early["failure"]
        self.assertEqual(
            (behavior["pairs"], behavior["left_only"], behavior["both"]), (5, 1, 1)
        )
        self.assertEqual(
            (failure["pairs"], failure["left_only"], failure["both"]), (5, 1, 0)
        )

    def test_unit_keeps_its_escalation_assignment_when_that_run_is_ungraded(self):
        cells = [
            row("T1", "c1", "no_memory", "success"),
            row("T1", "c1", "note_escalation", None),
            row("T2", "c2", "no_memory", "database_mismatch"),
            row("T2", "c2", "note_plain", "success"),
        ]
        self.assertEqual(
            summarize(cells)["same_tasks"],
            {
                "with_escalation_notes": {"no_memory": {"success": 1}},
                "without_escalation_notes": {
                    "no_memory": {"database_mismatch": 1},
                    "note_plain": {"success": 1},
                },
            },
        )

    def test_wording_table_covers_only_tasks_whose_reference_changes_the_database(self):
        self.assertEqual(
            self.summary["wording"]["all"],
            {
                "no_memory": counts(2, successes=2),
                "empty_preload": counts(1),
                "note_plain": counts(3, successes=2),
                "note_escalation": counts(1, early=1, agent=1),
            },
        )

    def test_escalation_notes_are_split_by_their_judged_stance(self):
        # T3 needs no change, so its note that only records a handoff stays out.
        self.assertEqual(
            self.summary["stance"]["all"], {"recommends": counts(1, early=1, agent=1)}
        )
        pair = self.summary["paired_stance"]["all"]["recommends_vs_no_memory"]
        self.assertEqual(
            (pair["success"]["pairs"], pair["success"]["right_only"]), (1, 1)
        )
        self.assertEqual(pair["agent_initiated"]["left_only"], 1)

    def test_notes_that_recommend_are_compared_with_notes_that_only_record(self):
        cells = [
            row(
                "T1",
                "c1",
                "note_escalation",
                "early_handoff",
                position=EARLY,
                initiator="unannounced",
                stance="recommends",
            ),
            row("T1", "c1", "note_escalation", "success", stance="records"),
            row("T2", "c2", "note_escalation", "success", stance="recommends"),
        ]
        pair = summarize(cells)["paired_stance"]["all"]["recommends_vs_records"]
        # Only T1 holds both stances; T2 has nothing to pair with.
        self.assertEqual(
            (pair["success"]["pairs"], pair["success"]["right_only"]), (1, 1)
        )
        self.assertEqual(pair["agent_initiated"]["left_only"], 1)

    def test_customer_requested_handoff_is_not_counted_as_agent_initiated(self):
        cells = [
            row(
                "T1",
                "c1",
                "note_plain",
                "early_handoff",
                position=EARLY,
                initiator="customer",
            ),
            row(
                "T1",
                "c1",
                "note_plain",
                "early_handoff",
                position=EARLY,
                initiator="agent",
            ),
            row("T1", "c1", "no_memory", "success"),
        ]
        summary = summarize(cells)
        self.assertEqual(
            summary["wording"]["all"]["note_plain"],
            counts(2, early=2, agent=1, customer=1),
        )
        pair = summary["paired_wording"]["all"]["note_plain_vs_no_memory"]
        self.assertEqual(pair["early_handoff"]["left_only"], 2)
        self.assertEqual(pair["agent_initiated"]["left_only"], 1)

    def test_units_are_split_by_whether_an_escalation_note_was_assigned(self):
        same = self.summary["same_tasks"]
        self.assertEqual(
            same["with_escalation_notes"],
            {
                "no_memory": {"success": 1},
                "empty_preload": {"database_mismatch": 1},
                "note_plain": {"success": 1},
                "note_escalation": {"early_handoff": 1},
            },
        )
        self.assertEqual(
            same["without_escalation_notes"],
            {
                "no_memory": {"success": 1},
                "note_plain": {"explanation_only": 1, "success": 1},
            },
        )

    def test_initiators_cover_early_handoffs_on_tasks_that_needed_changes(self):
        self.assertEqual(
            self.summary["initiators"], {"note_escalation": {"unannounced": 1}}
        )

    def test_second_pass_results_are_split_by_the_first_pass_outcome(self):
        first = self.summary["first_pass"]
        self.assertEqual(
            first["failure"]["groups"],
            {
                "no_memory": {"graded": 1, "successes": 1},
                "empty_preload": {"graded": 1, "successes": 0},
                "note_plain": {"graded": 1, "successes": 1},
                "note_escalation": {"graded": 1, "successes": 0},
            },
        )
        self.assertEqual(
            first["success"]["groups"],
            {
                "no_memory": {"graded": 2, "successes": 2},
                "note_plain": {"graded": 2, "successes": 1},
                "note_escalation": {"graded": 1, "successes": 1},
            },
        )
        self.assertEqual(first["failure"]["notes_vs_no_memory"]["right_only"], 1)

    def test_scope_checks_and_distinct_notes_are_reported(self):
        self.assertEqual(
            self.summary["scope"],
            {
                "graded": {"no_memory": 3, "empty_preload": 1, "memo": 5},
                "customers": 2,
                "tasks": 3,
            },
        )
        self.assertEqual(
            self.summary["checks"]["ungraded"]["same_task"],
            {"no_memory": [0, 3], "empty_preload": [0, 1], "memo": [1, 6]},
        )
        # Distinct delivered notes: e1 and e3 carry escalation wording, p1 and p2 do not.
        self.assertEqual(
            self.summary["notes"],
            {
                "distinct": 4,
                "with_escalation": 2,
                "stances": {"recommends": 1, "records": 1},
            },
        )

    def test_note_runs_are_split_by_whether_the_source_met_the_tool_defect(self):
        cells = [
            row(
                "T1",
                "c1",
                "note_escalation",
                "early_handoff",
                position=EARLY,
                initiator="unannounced",
                source_tool_defect=True,
            ),
            row("T1", "c1", "note_escalation", "success", source_tool_defect=True),
            row("T2", "c2", "note_escalation", "success", source_tool_defect=False),
            row(
                "T2", "c2", "note_plain", "database_mismatch", source_tool_defect=False
            ),
            row("T2", "c2", "no_memory", "success", source_tool_defect=False),
            row(
                "T3",
                "c2",
                "note_escalation",
                "success",
                changes=False,
                source_tool_defect=True,
            ),
        ]
        self.assertEqual(
            summarize(cells)["source_tool_defect"],
            {
                "affected": {
                    "note_escalation": counts(2, early=1, agent=1, successes=1)
                },
                "unaffected": {
                    "note_plain": counts(1),
                    "note_escalation": counts(1, successes=1),
                },
            },
        )

    def test_same_arm_repeats_show_how_often_identical_conditions_disagree(self):
        cells = [
            row("T1", "c1", "no_memory", "success", repeat=1),
            row("T1", "c1", "no_memory", "database_mismatch", repeat=2),
            row("T1", "c1", "no_memory", "success", repeat=3),
            row("T2", "c2", "no_memory", "success", repeat=1),
            row("T2", "c2", "no_memory", None, repeat=2),
        ]
        for cell in cells:
            cell["arm"] = "no_memory"
        # T1 pairs (1,2) and (2,3) disagree and (1,3) agrees; T2 has no pair with two scores.
        self.assertEqual(summarize(cells)["repeats"], {"no_memory": [2, 3]})

    def test_first_pass_run_is_not_a_repeat_of_the_second_pass_condition(self):
        cells = [
            {
                **row("T1", "c1", "no_memory", "database_mismatch", repeat=0),
                "official": False,
            },
            row("T1", "c1", "no_memory", "success", repeat=1),
            row("T1", "c1", "no_memory", "success", repeat=2),
        ]
        self.assertEqual(summarize(cells)["repeats"], {"no_memory": [0, 1]})

    def test_paired_comparisons_use_the_same_units(self):
        success = self.summary["paired_success"]["all"]["memo_vs_no_memory"]
        self.assertEqual(
            (success["pairs"], success["left_only"], success["right_only"]), (5, 0, 2)
        )
        early = self.summary["paired_wording"]["all"]["note_escalation_vs_no_memory"][
            "early_handoff"
        ]
        self.assertEqual(
            (early["pairs"], early["left_only"], early["right_only"]), (1, 1, 0)
        )


if __name__ == "__main__":
    unittest.main()

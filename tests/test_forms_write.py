"""First-pass source and writer behavior with small, explicitly synthetic responses."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.memory_writing import audit_memory_writes
from agent_memory_eval.contracts.core import ContractError, RunContext, canonical, digest, read_json
from agent_memory_eval.experiments import forms_write
from agent_memory_eval.experiments.forms_write import load_form_sources, read_form_bank, run_form_writer
from agent_memory_eval.memory.service import MemoryService
from agent_memory_eval.models.openrouter import OpenRouterClient, SpendGuard
from agent_memory_eval.recording.journal import Journal
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry
from test_openrouter import MODEL, ROUTE, Reply, response


ROOT = Path(__file__).resolve().parents[1]


class ScriptedSource:
    """Respond to the recorded-envelope API locally; the fixture is not a model result."""

    def respond(self, wire):
        public = dict(role="assistant", content="PUBLIC_FINAL_REPLY", reasoning="PRIVATE_SOURCE_REASONING")
        return dict(choices=[dict(message=public, finish_reason="stop")], usage={"cost": 0.0})


def source_fixture(root):
    design = read_json((ROOT / "protocols/drafts/tau2-two-pass-memory-forms-v1.json").read_bytes())
    cfg = design["source"]
    source_root = root / "baseline"
    cohort, runs = [], []
    with Journal(source_root, "synthetic-source-fixture") as j:
        for task in ("3", "17"):
            run = "INTERNAL_SOURCE_RUN_" + task
            ctx = RunContext(j.experiment_id, run, "fixture-session-" + task, "no_memory", "collection", "solve", "observed")
            policy = dict(role="system", content="PUBLIC_POLICY")
            user = dict(role="user", content="PUBLIC_USER_CORRECTION_" + task)
            previous = dict(role="assistant", content="PUBLIC_PRIOR_REPLY", reasoning="PRIVATE_PRIOR_REASONING")
            j.append([ctx.event("run.started", dict(bank_id=None, start_snapshot_id=None, fresh_session=True,
                                                    source_run_ids=[], task_ref=j.blobs.put(dict(task_id=task))))])
            registry = SourceRegistry(j, ctx)
            template = registry.template(dict(model="source-fixture", system=policy, past=previous, current=user))
            gateway = RecordingGateway(j)
            gateway.call(ctx, registry, "worker", template, [(template, "/system"), (template, "/past"),
                         (template, "/current")], ScriptedSource(), max_requests=2)
            # The simulator's private request occurs *after* the final worker request.
            private = registry.template(dict(model="sim-fixture", system={"role": "system", "content": "PRIVATE_USER_SCENARIO"}),
                                        roles=("user_simulator",))
            gateway.call(ctx, registry, "user_simulator", private, [(private, "/system")], ScriptedSource(), max_requests=2)
            grader = j.blobs.put(dict(reference_actions="HIDDEN_REFERENCE", final_database="HIDDEN_FINAL_DB"))
            success = task == "3"
            j.append([ctx.at("grade").event("outcome.scored", dict(grading_status="graded", official_success=success,
                       official_score=float(success), official_artifact_ref=grader))])
            j.append([ctx.at("seal").event("run.ended", dict(termination_reason="synthetic-fixture",
                                                              grading_status="graded", end_snapshot_id=None))])
            runs.append(dict(task_id=task, run_id=run, repetition=1, status="completed", grading_status="graded",
                             official_success=success, official_score=float(success), report_ref=grader))
            projected = canonical(dict(kind="past_public_experience", task_context=policy, messages=[
                dict(role="assistant", content="PUBLIC_PRIOR_REPLY"), user, dict(role="assistant", content="PUBLIC_FINAL_REPLY")]))
            cohort.append(dict(task_id=task, run_id=run, source_group="PRIVATE_GROUP", reference_has_db_write=False,
                               public_context_sha256=digest(projected), public_context_bytes=len(projected)))
        log_hash = digest(j.data)
    summary = dict(native_revision=cfg["benchmark_revision"], experiment_id="synthetic-source-fixture",
                   memory_enabled=False, domain="airline", runs=runs)
    data = canonical(summary)
    (source_root / "baseline-summary.json").write_bytes(data)
    receipt = canonical(dict(audit=dict(summary, log_sha256=log_hash, summary_sha256=digest(data))))
    (root / "receipt.json").write_bytes(receipt)
    cfg.update(root="baseline", receipt_path="receipt.json", receipt_sha256=digest(receipt), summary_sha256=digest(data),
               log_sha256=log_hash, cohort_sha256=digest(canonical(cohort)), cohort=cohort)
    design["allocation"]["planned"]["eligible_source_episodes"] = len(cohort)
    return design


class ScriptedWriter:
    offline = True

    def __init__(self, guard, replies):
        self.guard, self.replies, self.requests = guard, iter(replies), []

    def respond(self, request):
        self.requests.append(request)
        return next(self.replies)


def completion(message=None, reason="stop"):
    return dict(choices=[dict(message=message or dict(role="assistant", content="Finished."), finish_reason=reason)])


def store(call_id):
    args = canonical(dict(title="Authored " + call_id, body="A procedural note " + call_id)).decode()
    message = dict(role="assistant", content=None, reasoning="PRIVATE_WRITER_REASONING",
                   tool_calls=[dict(id=call_id, type="function", function=dict(name="memory_store", arguments=args))])
    return completion(message, "tool_calls")


class FormsWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        files = TemporaryDirectory()
        cls.addClassCleanup(files.cleanup)
        cls.source_root = Path(files.name)
        cls.design = source_fixture(cls.source_root)
        with patch.object(forms_write, "_source", wraps=forms_write._source) as locked_read:
            cls.sources = load_form_sources(cls.design, cls.source_root)
            if locked_read.call_count != 1:
                raise AssertionError("Both public sources must share a single locked read")

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.guard = SpendGuard(max_calls=12, max_usd=2, max_seconds=60)

    def write(self, path, *, replies=None, client=None, arm="fact", rep=1, design=None, source=None, **kwargs):
        client = client or ScriptedWriter(self.guard, replies or [completion()])
        design = self.design if design is None else design
        with Journal(path, "writer-fixture", contract_version="1.2") as j:
            result = run_form_writer(j, design, source or self.sources["3"], repetition=rep, arm=arm,
                                     client=client, guard=self.guard, root=ROOT, **kwargs)
        return result, client

    def test_source_projection_matches_immutable_task_and_never_contains_private_artifacts(self):
        self.assertEqual(set(self.sources), {"3", "17"})
        self.assertIs(self.sources["17"].official_success, False)
        for task, source in self.sources.items():
            self.assertIn("PUBLIC_USER_CORRECTION_" + task, source.public_context)
            self.assertNotIn("PRIVATE_", source.public_context)
            self.assertNotIn("HIDDEN_", source.public_context)
            self.assertNotIn("INTERNAL_", source.public_context)
            self.assertEqual(digest(source.public_context.encode()), source.public_context_sha256)
            self.assertEqual(len(source.public_context.encode()), source.public_context_bytes)
        for field, wrong in (("public_context_sha256", "0" * 64), ("public_context_bytes", 5),
                             ("run_id", "unrelated-run")):
            with self.subTest(field=field), self.assertRaises(ContractError):
                altered = deepcopy(self.design)
                cohort = altered["source"]["cohort"]
                cohort[0][field] = wrong
                altered["source"]["cohort_sha256"] = digest(canonical(cohort))
                load_form_sources(altered, self.source_root)
        with self.assertRaisesRegex(ContractError, "source differs"):
            self.write(self.work / "wrong", source=replace(self.sources["3"], public_context=self.sources["17"].public_context))

    def feedback_design(self):
        design = deepcopy(self.design)
        selection = design["source"]
        selection["feedback_level"] = "result_only"
        for row in selection["cohort"]:
            row["result_feedback"] = self.sources[row["task_id"]].result_receipt()
        selection["cohort_sha256"] = digest(canonical(selection["cohort"]))
        return design

    def test_official_success_and_failure_are_sent_only_in_the_writer_feedback_message(self):
        from scripts.review_banking_evidence import feedback_pair
        from agent_memory_eval.memory.writing import REQUIRED_STORE_CONTRACT

        design = self.feedback_design()
        baseline_design = deepcopy(self.design)
        for variant in (baseline_design, design):
            variant["writing"]["completion_contract"] = REQUIRED_STORE_CONTRACT
        sources = load_form_sources(design, self.source_root)
        for task, source in sources.items():
            for arm in design["writing"]["lines"]:
                with self.subTest(task=task, arm=arm):
                    directory = self.work / task / arm
                    _, baseline = self.write(directory/"none", source=source, arm=arm, design=baseline_design,
                                             replies=[store("saved")])
                    result, observed = self.write(directory/"binary", source=source, arm=arm, design=design,
                                                  replies=[store("saved"), completion()])
                    first, second = baseline.requests[0], deepcopy(observed.requests[0])
                    self.assertEqual(second["tool_choice"], "required")
                    expected = feedback_pair(first, source)
                    self.assertEqual(expected["result_only_input_sha256"], digest(canonical(second)))
                    self.assertEqual(read_json(second["messages"][2]["content"].encode()),
                                     dict(kind="evaluation_feedback", level="result_only", success=task=="3"))
                    self.assertNotIn(source.outcome_event_id, canonical(second).decode())
                    self.assertNotIn("HIDDEN_", canonical(second).decode())
                    second["messages"][2] = first["messages"][2]
                    self.assertEqual(second, first)
                    bank = read_form_bank(directory/"binary", expected_log_sha256=result["log_sha256"])
                    self.assertEqual((bank["feedback_level"], bank["result_feedback"]), ("result_only", source.result_receipt()))

    def test_result_only_rejects_missing_and_substituted_outcomes_before_model_call(self):
        design = self.feedback_design()
        source = self.sources["3"]
        writer = ScriptedWriter(self.guard, [completion()])
        for index, altered in enumerate((replace(source, official_success=None), replace(source, outcome_event_id=None),
                                         replace(source, official_success=False), replace(source, outcome_event_id="other"))):
            with self.subTest(index=index), self.assertRaises(ContractError):
                self.write(self.work/str(index), source=altered, design=design, client=writer)
        self.assertEqual(writer.requests, [])
        cohort = design["source"]["cohort"]
        cohort[0]["result_feedback"] = self.sources["17"].result_receipt()
        design["source"]["cohort_sha256"] = digest(canonical(cohort))
        with self.assertRaisesRegex(ContractError, "differs from the scored first pass"):
            load_form_sources(design, self.source_root)

    def test_frozen_first_writer_request_is_checked_before_transport(self):
        design = self.feedback_design()
        _, recorded = self.write(self.work/"reference", design=design)
        source = self.sources["3"]
        entry = next(r for r in design["source"]["cohort"] if r["task_id"] == source.task_id)
        entry["first_writer_inputs"] = {"fact": digest(canonical(recorded.requests[0]))}
        self.write(self.work/"matched", design=design)
        entry["first_writer_inputs"]["fact"] = "0" * 64
        never = ScriptedWriter(self.guard, [completion()])
        with self.assertRaisesRegex(ContractError, "registered feedback comparison"):
            self.write(self.work/"different", design=design, client=never)
        self.assertEqual(never.requests, [])

    def test_all_five_forms_share_the_same_wire_except_one_line_and_have_independent_banks(self):
        wires, banks, sessions = [], set(), set()
        common = (ROOT / self.design["writing"]["common_instruction_ref"]["path"]).read_text().rstrip("\r\n")
        for arm, line in self.design["writing"]["lines"].items():
            path = self.work / arm
            result, client = self.write(path, arm=arm, run_id="fixture-" + arm)
            self.assertEqual(result["run_id"], "fixture-" + arm)
            bank = read_form_bank(path, expected_log_sha256=result["log_sha256"])
            self.assertEqual(bank, result["bank"])
            self.assertEqual((bank["status"], bank["items"], bank["head_revision"]), ("empty", [], 0))
            banks.add(result["bank_id"])
            sessions.add(result["session_id"])
            self.assertEqual(client.requests[0]["messages"], [
                dict(role="system", content=common + "\n" + line), dict(role="user", content=self.sources["3"].public_context),
                dict(role="user", content=canonical(dict(kind="evaluation_feedback", level="none")).decode())])
            self.assertEqual([t["function"]["name"] for t in client.requests[0]["tools"]], ["memory_store"])
            normalized = deepcopy(client.requests[0])
            normalized["messages"][0]["content"] = common
            wires.append(canonical(normalized))
        self.assertEqual((len(set(wires)), len(banks), len(sessions)), (1, 5, 5))
        second, client = self.write(self.work / "rep2", rep=2)
        self.assertEqual(client.requests[0]["seed"], 1)
        self.assertEqual(read_json(wires[0])["seed"], 0)
        self.assertNotIn(second["bank_id"], banks)

    def test_nonempty_export_preserves_order_identity_and_immutable_versions(self):
        result, client = self.write(self.work, replies=[store("first"), store("second"), completion()])
        self.assertEqual((result["requests"], result["memory_stores"], result["status"]), (3, 2, "completed"))
        self.assertNotIn("PRIVATE_WRITER_REASONING", canonical(client.requests).decode())
        bank = read_form_bank(self.work, expected_log_sha256=result["log_sha256"])
        self.assertEqual([v["title"] for v in bank["items"]], ["Authored first", "Authored second"])
        self.assertEqual(bank["head_revision"], 2)
        self.assertEqual(result["bank"], bank)
        result["bank"]["items"].reverse()
        self.assertEqual(read_form_bank(self.work, expected_log_sha256=result["log_sha256"]), bank)
        with self.assertRaisesRegex(ContractError, "immutable"):
            read_form_bank(self.work, expected_log_sha256="1" * 64)
        # This writer copied only its public projection, not the baseline log, grade or user state.
        for blob in (self.work / "blobs/sha256").iterdir():
            payload = blob.read_bytes()
            for private in (b"HIDDEN_REFERENCE", b"HIDDEN_FINAL_DB", b"PRIVATE_USER_SCENARIO", b"PRIVATE_SOURCE_REASONING"):
                self.assertNotIn(private, payload)

    def test_truncated_writer_keeps_partial_bank_but_cannot_supply_a_solver(self):
        result, _ = self.write(self.work, replies=[store("first"), completion(reason="length")])
        self.assertEqual((result["error_code"], result["memory_stores"]), ("WRITER_OUTPUT_TRUNCATED", 1))
        self.assertEqual(result["partial_snapshot_id"], result["snapshot_id"])
        self.assertIsNone(result["bank"])
        self.assertEqual(audit_memory_writes(self.work)["writes"][0]["termination_reason"], "memory_write_error")
        with self.assertRaisesRegex(ContractError, "normally finished"):
            read_form_bank(self.work, expected_log_sha256=result["log_sha256"])

    def test_descriptor_checks_authored_text_against_original_model_storage_call(self):
        store_original = MemoryService._store
        def changed_body(service, state, bank, arguments, prefix):
            return store_original(service, state, bank, dict(arguments, body="REPLACED_AFTER_REPLY"), prefix)
        with patch.object(MemoryService, "_store", changed_body), self.assertRaisesRegex(ContractError, "public storage call"):
            self.write(self.work, replies=[store("original"), completion()])
        with self.assertRaisesRegex(ContractError, "public storage call"):
            read_form_bank(self.work)

    def test_shared_guard_and_unknown_charge_survive_an_injected_http_reply(self):
        model = dict(MODEL, selected_endpoint=dict(provider_name="OpenAI"))
        sent = []
        def opener(request, timeout):
            sent.append(read_json(request.data))
            return Reply(response(None))
        client = OpenRouterClient(model, self.guard, route=ROUTE, opener=opener,
                                  max_input_bytes=self.design["writing"]["proposed_limits"]["input_bytes"])
        with patch.dict("os.environ", OPENROUTER_API_KEY="fixture-router-key"):
            result, _ = self.write(self.work, client=client)
        self.assertEqual((result["status"], result["error_code"], result["unknown_cost"]), ("error", "UNKNOWN_COST_STOP", True))
        self.assertEqual((len(sent), self.guard.calls, self.guard.unknown_cost), (1, 1, True))
        self.assertEqual(result["reported_cost_usd_decimal"], "0")
        self.assertIsNone(result["bank"])
        self.assertNotIn("HIDDEN_", canonical(sent).decode())
        with self.assertRaisesRegex(ContractError, "shared guard"):
            self.write(self.work / "different-guard", client=ScriptedWriter(object(), [completion()]))

    def test_full_request_limit_blocks_before_transport_and_preserves_assignment(self):
        design = deepcopy(self.design)
        design["writing"]["proposed_limits"]["input_bytes"] = self.sources["3"].public_context_bytes
        result, client = self.write(self.work, design=design)
        self.assertEqual((result["status"], result["error_code"], client.requests), ("error", "INPUT_SIZE_LIMIT", []))
        self.assertIs(result["unknown_cost"], False)
        self.assertEqual(audit_memory_writes(self.work)["writes"][0]["requests"], 1)
        with self.assertRaisesRegex(ContractError, "fresh journal"):
            self.write(self.work, design=design)

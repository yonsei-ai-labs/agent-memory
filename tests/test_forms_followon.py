from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, digest
from agent_memory_eval.experiments.forms_followon import identified_generation, pending_after, provider_error_receipt
from agent_memory_eval.experiments.forms_supplement import SupplementGuard, run_pending
from agent_memory_eval.models.openrouter import request_reservation_usd


class Blobs:
    def __init__(self):
        self.contents = {}

    def ref(self, value):
        h = digest(canonical(value))
        self.contents[h] = value
        return dict(sha256=h)

    def get(self, ref):
        return self.contents[ref["sha256"]]


class FollowonTests(unittest.TestCase):
    def setUp(self):
        self.blobs = Blobs()
        self.models = dict(user_simulator=dict(pricing=dict(prompt="0.0000003", completion="0.0000012")))
        self.wire = dict(messages=[dict(role="user", content="fixture")], max_tokens=512)
        self.id = "gen-valid-504"
        self.start = dict(type="model.request_started", payload=dict(request_id="request-one", actor_role="user_simulator",
            request_ref=self.blobs.ref(self.wire)))

    def receipt(self, result_code, body, *, delivery="response_received", cost="unknown"):
        end = dict(type="model.request_finished", payload=dict(request_id="request-one", status="error",
            delivery_status=delivery, cost_status=cost, response_ref=self.blobs.ref(body)))
        return provider_error_receipt(dict(error_code=result_code), [self.start, end], self.blobs, self.models)

    def test_received_http_or_json_504_must_have_valid_id_and_exact_original_wire(self):
        for code in ("HTTP_504", "PROVIDER_RESPONSE_ERROR"):
            receipt = self.receipt(code, dict(id=self.id, error=dict(code=504, retry_after_seconds=71)))
            self.assertEqual(receipt["request_sha256"], self.start["payload"]["request_ref"]["sha256"])
            self.assertEqual((receipt["code"], receipt["generation_id"]), (504, self.id))
            self.assertEqual(Decimal(receipt["reserved_usd"]), request_reservation_usd(self.wire, self.models["user_simulator"]))
            self.assertEqual(receipt["retry_after_seconds"], 71 if code=="HTTP_504" else None)
        paid = self.receipt("PROVIDER_RESPONSE_ERROR", dict(error=dict(code="504", metadata=dict(generation_id=self.id))), cost="observed")
        self.assertEqual((paid["generation_id"], paid["reserved_usd"]), (self.id, "0"))

    def test_other_codes_missing_id_and_ambiguous_sends_keep_unknown(self):
        for code, body, delivery in (
            ("TRANSPORT_UNKNOWN", dict(id=self.id, error=dict(code=504)), "unknown"),
            ("PROVIDER_RESPONSE_ERROR", dict(error=dict(code=504)), "response_received"),
            ("HTTP_504", dict(id="session-invalid", error=dict(code=504)), "response_received"),
            ("PROVIDER_RESPONSE_ERROR", dict(id=self.id, error=dict(code=503)), "response_received"),
            ("PROVIDER_RESPONSE_ERROR", dict(id=self.id, error=dict(code=429)), "response_received"),
            ("HTTP_504", dict(id=self.id, error=dict(code=504)), "not_submitted")):
            self.assertIsNone(self.receipt(code, body, delivery=delivery))
        self.assertIsNone(identified_generation(dict(id="gen-value with spaces")))

    def test_existing_confirmed_http_429_rule_still_needs_no_generation(self):
        receipt = self.receipt("HTTP_429", dict(error=dict(code=429)))
        self.assertEqual((receipt["code"], receipt["generation_id"]), (429,None))
        self.assertGreater(Decimal(receipt["reserved_usd"]), 0)

    def test_two_old_reserves_plus_one_new_504_and_no_second_send_for_that_job(self):
        guard = SupplementGuard(dict(max_calls=16000, max_seconds=129600, max_usd=5), datetime.now(timezone.utc).isoformat(),
            dict(http_calls=1861, known_usd="0.2916642358", reserved_usd="0.0100881", original_429=dict(reserved_usd="0.0018567")))
        receipt = self.receipt("PROVIDER_RESPONSE_ERROR", dict(id=self.id,error=dict(code=504)))
        indices, delays = [], []
        def execute(row):
            indices.append(row["index"])
            guard.before(self.wire,self.models["user_simulator"])
            first = row["index"]==303
            guard.after({} if first else dict(usage=dict(cost=0.001)))
            return (dict(status="error" if first else "completed",billing_by_role=dict(user_simulator=dict(unknown_count=int(first)))),
                    receipt if first else None)
        rows = [dict(index=i) for i in (303,304)]
        self.assertIsNone(run_pending(rows,execute,guard,lambda rs: None,60,delays.append,error_field="provider_error"))
        self.assertEqual((indices, delays, guard.calls), ([303,304], [60], 1863))
        self.assertEqual(guard.reserved, Decimal("0.0100881")+Decimal(receipt["reserved_usd"]))
        self.assertIn("provider_error", rows[0])
        self.assertNotIn("http_429", rows[0])

    def test_no_reassignment_of_started_rows_or_changed_identity(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = dict(index=303,phase="solve",task_id="3",repetition=1,arm="lesson",run_id="original-id",directory="jobs/0303")
            observed = dict(row, status="not_started",reason="unknown_cost")
            self.assertEqual(pending_after([observed],[row],root), [row])
            for change in (dict(run_id="another"),dict(reason="controller_error"),dict(result={})):
                with self.assertRaises(ContractError):
                    pending_after([dict(observed,**change)],[row],root)
            folder = root/row["directory"];folder.mkdir(parents=True)
            with self.assertRaises(ContractError):
                pending_after([observed],[row],root)
            raw = b"sealed previously sent job";(folder/"log.jsonl").write_bytes(raw)
            closed = dict(row,status="error",result=dict(status="error",run_id=row["run_id"],log_sha256=digest(raw)))
            self.assertEqual(pending_after([closed],[row],root), [])

    def test_received_504_reaches_actual_native_journal_and_next_allocation(self):
        from types import SimpleNamespace
        from agent_memory_eval.adapters.tau2 import native_modules
        from agent_memory_eval.contracts.core import read_json
        from agent_memory_eval.contracts.validation import ROOT
        from agent_memory_eval.experiments.forms_batch import DESIGN
        from agent_memory_eval.experiments.forms_supplement import solve_recorded_row
        from agent_memory_eval.models.openrouter import OpenRouterClient
        from agent_memory_eval.recording.journal import read_experiment, BlobStore
        from test_forms_solve import AVAILABLE, NativeHTTP
        from test_openrouter import Reply
        if not AVAILABLE:
            self.skipTest("Requires the pinned native checkout")
        checkout = ROOT / "data/tau2-bench"
        modules = native_modules(checkout.resolve())
        from loguru import logger
        logger.disable("tau2")
        design = read_json((ROOT / DESIGN).read_bytes())
        connection = read_json((ROOT / design["model"]["connection_profile_ref"]["path"]).read_bytes())
        tasks = {t["id"]: t for t in read_json((checkout / "data/tau2/domains/airline/tasks.json").read_bytes())}
        models = {role:dict(id=connection[role]["model_id"], canonical_slug=connection[role]["model_id"],
                            pricing=dict(prompt="0.0000003", completion="0.0000012"),
                            selected_endpoint=dict(tag=connection[role]["provider"]["only"][0],
                            provider_name="OpenAI" if role == "worker" else "Fireworks")) for role in ("worker","user_simulator")}
        guard = SupplementGuard(dict(max_calls=16000, max_usd=5, max_seconds=129600), datetime.now(timezone.utc).isoformat(),
            dict(http_calls=1861, known_usd="0.2916642358", original_429=dict(reserved_usd="0.0018567"), reserved_usd="0.0100881"))
        plan = dict(sources={"40":SimpleNamespace(public_context_sha256=None)}, tasks=tasks, connection=connection, modules=modules)
        fake = NativeHTTP()
        sent = []
        def opener(wire, timeout):
            sent.append(wire)
            if len(sent) == 1:
                return Reply(dict(id="gen-first-504", error=dict(code=504, message="no usage")))
            return fake(wire, timeout)
        clients = {role: OpenRouterClient(m, guard, route=connection[role]["provider"], opener=opener, max_input_bytes=128000)
                   for role,m in models.items()}
        with TemporaryDirectory() as tmp, patch.dict("os.environ", OPENROUTER_API_KEY="fake-key-for-local-opener"):
            path=Path(tmp)
            rows=[dict(index=i,task_id="40",repetition=1,arm="no_memory",run_id="local-"+str(i),directory="jobs/"+str(i)) for i in (303,304)]
            def run(row):
                return solve_recorded_row(row,path,path,plan,design,models,clients,guard,{},5,provider_error_receipt)
            waits=[]
            self.assertIsNone(run_pending(rows,run,guard,lambda rs:None,60,waits.append,error_field="provider_error"))
            first, second=rows
            self.assertEqual((first["result"]["http_calls"], first["result"]["official_success"]), (1,None))
            self.assertEqual(first["provider_error"]["generation_id"], "gen-first-504")
            self.assertEqual(second["result"]["grading_status"],"graded")
            scan=read_experiment(path/first["directory"])[0]
            recorded=provider_error_receipt(first["result"],scan["events"].values(),BlobStore(path/first["directory"]),models)
            self.assertEqual(recorded,first["provider_error"])
            self.assertEqual(guard.calls,1861+len(sent))
            self.assertEqual(waits,[60])

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.analysis.forms_results import checked_rows
from agent_memory_eval.analysis.prompt_report import publish_report
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import BatchGuard
from agent_memory_eval.experiments.fresh_forms import CostReservations, eligible_customers, run_study
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.recording.journal import BlobStore
from test_forms_solve import AVAILABLE, NativeHTTP
from test_openrouter import Reply, response

CONFIG = ROOT / "protocols/pilots/tau2-new-customer-forms-v1.json"
CHECKOUT = ROOT / "data/tau2-bench"


class FreshFormsTests(unittest.TestCase):
    def test_received_unknown_http_is_charged_to_the_reserve_at_the_wire_bound(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "j"
            folder.mkdir()
            blobs = BlobStore(folder)
            wire = dict(model="test", messages=[dict(role="user",content="hi")], max_tokens=4)
            req = dict(request_id="q", actor_role="writer", request_ref=blobs.put(wire))
            end = dict(request_id="q", delivery_status="response_received", status="error", cost_status="unknown",
                       response_ref=blobs.put(dict(error=dict(code=429))))
            scan = dict(events={1:dict(type="model.request_started",payload=req),2:dict(type="model.request_finished",payload=end)})
            guard = BatchGuard(max_calls=10,max_usd=1,max_seconds=10)
            model = dict(pricing=dict(prompt="0.0001",completion="0.0001"))
            reserve = CostReservations(root, dict(codes=[429],maximum_per_stage=1), guard, {"worker":model})
            row = dict(directory="j",result=dict(billing_by_role={"writer":dict(unknown_count=1)}))
            with patch("agent_memory_eval.experiments.fresh_forms.read_experiment",return_value=(scan,{},None)):
                self.assertTrue(reserve(row))
                self.assertFalse(reserve(row))
            self.assertGreater(reserve.reserved,0)
            self.assertEqual(guard.max_usd,1-float(reserve.reserved))
            self.assertEqual(row["result"]["unpriced_reservation"]["request_ref"],req["request_ref"])

            end.update(delivery_status="unknown", response_ref=None)
            row["result"].update(error_code="TRANSPORT_UNKNOWN")
            row["result"].pop("unpriced_reservation")
            config = dict(codes=[429], maximum_per_stage=1)
            guard = BatchGuard(max_calls=10, max_usd=1, max_seconds=100)
            with patch("agent_memory_eval.experiments.fresh_forms.read_experiment", return_value=(scan, {}, None)):
                self.assertFalse(CostReservations(root, config, guard, {"worker": model})(row))
                reserve_transport = CostReservations(root, dict(config, reserve_uncertain_transport=True), guard, {"worker": model})
                scan["pending_requests"] = ["unclosed"]
                self.assertFalse(reserve_transport(row))
                scan["pending_requests"] = []
                self.assertTrue(reserve_transport(row))
            self.assertEqual(reserve_transport.reserved, reserve.reserved)
            self.assertIsNone(row["result"]["unpriced_reservation"]["response_ref"])

    @unittest.skipUnless(AVAILABLE, "Requires pinned local airline benchmark")
    def test_selection_and_first_pass_to_all_six_second_pass_arms(self):
        config = json.loads(CONFIG.read_bytes())
        base = CHECKOUT / "data/tau2/domains/airline"
        tasks = {t["id"]:t for t in json.loads((base/"tasks.json").read_bytes())}
        args = (tasks,json.loads((base/"split_tasks.json").read_bytes()),json.loads((base/"db.json").read_bytes()))
        self.assertEqual(len(eligible_customers(config,*args)),6)
        config["tasks"] = ["19"]
        with self.assertRaises(ContractError):
            eligible_customers(config,*args)
        config["seed_pairs"] = config["seed_pairs"][:1]
        models = json.loads((ROOT/config["endpoint_ref"]["path"]).read_bytes())["models"]
        http = NativeHTTP()
        writer_inputs = []
        original = OpenRouterClient.__init__
        def open_request(request, timeout):
            wire = json.loads(request.data)
            if any(t["function"]["name"]=="memory_store" for t in wire.get("tools",[])):
                writer_inputs.append(wire["messages"])
                raw = response()
                raw.update(model=wire["model"],provider="OpenAI")
                return Reply(raw)
            if wire["model"].startswith("deepseek/") and len(wire["messages"])==2:
                http.by_role["user_simulator"].clear()
            return http(request,timeout)
        def inject(client,*args,**kwargs):
            original(client,*args,**kwargs,opener=open_request)
        ns="agent_memory_eval.experiments.fresh_forms."
        with TemporaryDirectory() as tmp:
            work=Path(tmp)
            (work/"runs").mkdir()
            cfg=work/"pilot.json"
            cfg.write_text(json.dumps(config))
            with patch.dict("os.environ",OPENROUTER_API_KEY="mock-fresh"), patch(ns+"eligible_customers",return_value={"19":"example"}), \
                    patch(ns+"implementation_identity",return_value={"implementation_commit":"test"}), \
                    patch(ns+"live_models",return_value=models), patch.object(OpenRouterClient,"__init__",inject):
                run_study(ROOT,work,CHECKOUT,cfg,"source",live=True)
                run_study(ROOT,work,CHECKOUT,cfg,"comparison",live=True)
                with self.assertRaises(ContractError):
                    run_study(ROOT,work,CHECKOUT,cfg,"source",live=True)
            self.assertEqual(len(writer_inputs),4)
            self.assertEqual(len({m[1]["content"] for m in writer_inputs}),1)
            self.assertTrue(all(set(json.loads(m[1]["content"]))=={"kind","task_context","messages"} for m in writer_inputs))
            root=work/"runs"/config["comparison_name"]
            d,_,rows,_=checked_rows(root)
            self.assertEqual(d["source"]["split"],"test")
            self.assertEqual(d["task_sets"]["first_failure"],["19"])
            self.assertEqual(sum(r["status"]=="empty" for r in rows),4)
            rep=publish_report(root,work/"report")
            self.assertEqual(rep["outcomes"],{"실패":6})
            self.assertEqual(len(rep["contrasts"]["first_failure"]),15)

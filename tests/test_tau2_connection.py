"""Optional native integration with mock HTTP; opt in by installing the tau2 extra."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import PIN, Tau2Bridge, prepare_native, run_native_check
from agent_memory_eval.analysis.connection import audit_connection
from agent_memory_eval.contracts.core import canonical
from agent_memory_eval.contracts.core import ContractError
from agent_memory_eval.recording.journal import Journal
from test_openrouter import Reply, response


ROOT = Path(__file__).resolve().parents[1]
AVAILABLE = importlib.util.find_spec("tau2") is not None and (ROOT/"data/tau2-bench/.git").is_dir()


@unittest.skipUnless(AVAILABLE, "Install the tau2 extra and pinned checkout for native tests")
class Tau2ConnectionTests(unittest.TestCase):
    def test_native_conversation_grading_and_traces_with_mock_http_only(self):
        self.native_conversation("openrouter-tau2-smoke.json")

    def test_historical_v1_profile_still_runs_and_audits(self):
        self.native_conversation("openrouter-tau2-smoke-v1.0.json")

    def test_null_usage_is_preserved_in_native_summary_and_audit(self):
        self.native_conversation("openrouter-tau2-smoke.json", null_usage=True)

    def native_conversation(self, filename, *, null_usage=False):
        config = json.loads((ROOT/"protocols/checks"/filename).read_text())
        catalog = {"data": [dict(id=config[role]["model_id"],canonical_slug=config[role]["model_id"],
            supported_parameters=list(config[role]["parameters"]),pricing=dict(prompt="0.000001",completion="0.000001"))
            for role in ("worker","user_simulator")]}
        def endpoints(model_id):
            provider = config["user_simulator"]["provider"]["only"][0] if model_id.startswith("deepseek/") else "openai"
            return dict(data=dict(id=model_id,endpoints=[dict(tag=provider,
                provider_name={"deepseek":"DeepSeek","openai":"OpenAI","fireworks":"Fireworks"}[provider],status=0,
                supported_parameters=["reasoning","max_tokens","temperature","tools","tool_choice"]
                    + (["seed"] if provider=="openai" else []),
                pricing=dict(prompt="0.000001",completion="0.000001"))]))
        with patch("agent_memory_eval.adapters.tau2.model_catalog",return_value=catalog), \
                patch("agent_memory_eval.adapters.tau2.model_endpoints",side_effect=endpoints):
            prepared = prepare_native(config,ROOT/"data/tau2-bench")
        native_user_calls = 0
        native_worker_calls = 0
        network_requests = []
        def opener(request,timeout):
            nonlocal native_user_calls, native_worker_calls
            wire=json.loads(request.data)
            network_requests.append(wire)
            body=response()
            if null_usage and len(network_requests) == 1:
                body["usage"] = None
            body["id"]="response-"+str(len(network_requests))
            body["model"]=wire["model"]
            if wire["model"]==config["user_simulator"]["model_id"]:
                body["provider"]="Fireworks" if wire["model"].startswith("deepseek/") else "OpenAI"
                native_user_calls+=1
                text="Hello, I need help." if native_user_calls==1 else "###STOP###"
            else:
                native_worker_calls+=1
                text="Please provide your details."
            body["choices"][0]["message"]["content"]=text
            if wire["model"]==config["worker"]["model_id"] and native_worker_calls==1:
                body["choices"][0].update(finish_reason="tool_calls",message=dict(role="assistant",content=None,
                    tool_calls=[dict(id="native-airport-tool",type="function",function=dict(name="list_all_airports",arguments="{}"))]))
            return Reply(body)
        original=Tau2Bridge.__init__
        def init(bridge,*args,**kwargs):
            return original(bridge,*args,**kwargs,opener=opener)
        with TemporaryDirectory() as folder, patch.dict("os.environ",OPENROUTER_API_KEY="test-key-only"):
            with Journal(folder,"native-mock") as journal, patch.object(Tau2Bridge,"__init__",init):
                report=run_native_check(journal,config,prepared,live=True)
                if null_usage:
                    self.assertTrue(report["unknown_cost"])
                    self.assertEqual(report["http_calls"], 1)
                    self.assertEqual(report["reported_tokens"], 0)
                    finishes = [e["payload"] for e in journal.events.values() if e["type"] == "model.request_finished"]
                    first = next(p for p in finishes if p["response_ref"] is not None)
                    self.assertIsNone(journal.blobs.get(first["response_ref"])["usage"])
                    self.assertEqual(first["cost_status"], "unknown")
                    (Path(folder)/"connection-summary.json").write_bytes(canonical(report))
                    audit = audit_connection(folder)
                    self.assertTrue(audit["unknown_cost"])
                    self.assertEqual(audit["reported_tokens"], 0)
                    return
                self.assertEqual(report["status"],"completed",report)
                self.assertEqual(report["grading_status"],"graded")
                self.assertIsInstance(report["official_success"],bool)
                native_report=journal.blobs.get(report["report_ref"])
                self.assertEqual(native_report["reward_info"]["reward"],report["official_score"])
                self.assertEqual(report["http_calls"],4)
                self.assertEqual(report["requests_by_role"],dict(user_simulator=2,worker=2))
                self.assertTrue(any(m.get("role")=="tool" for wire in network_requests for m in wire["messages"]))
                self.assertEqual(journal.scan["incomplete_runs"],[])
                self.assertFalse(any(e["type"].startswith("memory.") for e in journal.events.values()))
                self.assertTrue(all(wire["provider"]["allow_fallbacks"] is False for wire in network_requests))
                if config["profile_version"] == "1.1":
                    user_wires=[wire for wire in network_requests if wire["model"]==config["user_simulator"]["model_id"]]
                    self.assertTrue(all(wire["provider"]["only"]==config["user_simulator"]["provider"]["only"] and "seed" not in wire
                        and wire["max_tokens"]==512 and wire["reasoning"]==dict(enabled=False) for wire in user_wires))
                user_system=prepared[0].user.system_prompt
                worker_wires=[wire for wire in network_requests if wire["model"]==config["worker"]["model_id"]]
                self.assertTrue(all(user_system not in canonical(wire).decode() for wire in worker_wires))
                (Path(folder)/"connection-summary.json").write_bytes(canonical(report))
                original_log=(Path(folder)/"log.jsonl").read_bytes()
                audit=audit_connection(folder)
                self.assertEqual(audit["native_business_tool_calls"],1)
                self.assertEqual(audit["reported_cost_usd_decimal"],"0.0004")
                self.assertEqual((Path(folder)/"log.jsonl").read_bytes(),original_log)
                tampered=deepcopy(report)
                tampered["official_success"]=not tampered["official_success"]
                (Path(folder)/"connection-summary.json").write_bytes(canonical(tampered))
                with self.assertRaisesRegex(ContractError,"outcome differs"):
                    audit_connection(folder)


if __name__=="__main__":
    unittest.main()

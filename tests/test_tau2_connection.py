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
        config = json.loads((ROOT/"protocols/checks/openrouter-tau2-smoke.json").read_text())
        catalog = {"data": [dict(id=config[role]["model_id"],canonical_slug=config[role]["model_id"],
            supported_parameters=list(config[role]["parameters"]),pricing=dict(prompt="0.000001",completion="0.000001"))
            for role in ("worker","user_simulator")]}
        with patch("agent_memory_eval.adapters.tau2.model_catalog",return_value=catalog):
            prepared = prepare_native(config,ROOT/"data/tau2-bench")
        native_user_calls = 0
        native_worker_calls = 0
        network_requests = []
        def opener(request,timeout):
            nonlocal native_user_calls, native_worker_calls
            wire=json.loads(request.data)
            network_requests.append(wire)
            body=response()
            body["id"]="response-"+str(len(network_requests))
            body["model"]=wire["model"]
            if wire["model"]==config["user_simulator"]["model_id"]:
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

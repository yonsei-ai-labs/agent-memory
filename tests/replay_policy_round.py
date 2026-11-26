"""Exercise both native two-pass paths with a scripted transport and no network."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stdout
from functools import partial
import io
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import patch

from agent_memory_eval.contracts.core import canonical, digest
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments import forms_batch
from agent_memory_eval.models.openrouter import OpenRouterClient
from agent_memory_eval.runner import bfcl_relevance, policy_round
from scripts import run_banking_policy_cycle as bank, run_full_retail
from test_openrouter import Reply, response

ON_DEMAND_NOTICE = (
    "Prior work is available through the memory tools. Consult it when it helps with the current task."
)


def block_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise AssertionError("The integration fixture cannot send network requests")


def local_message(wire):
    tools = [t["function"]["name"] for t in wire.get("tools", [])]
    name = None
    if tools == ["memory_store"]:
        name, args = "memory_store", dict(title="Prior attempt", body="The worker asked for clarification.")
    elif "memory_list" in tools:
        assert ON_DEMAND_NOTICE in wire["messages"][0]["content"]
        issued = {c["function"]["name"]: c["id"] for m in wire["messages"] for c in m.get("tool_calls") or []}
        if "memory_list" not in issued:
            name, args = "memory_list", {}
        elif "memory_get" not in issued:
            message = next(m for m in wire["messages"] if m.get("tool_call_id") == issued["memory_list"])
            name, args = "memory_get", {"id": json.loads(message["content"])["items"][0]["id"]}
    if name is None:
        return dict(role="assistant", content="Could you clarify the request?")
    call = dict(id="fixture-" + name, type="function", function=dict(name=name, arguments=json.dumps(args)))
    return dict(role="assistant", content=None, tool_calls=[call])


def bank_reply(request, timeout):
    wire = json.loads(request.data)
    raw = response(cost=0)
    raw["model"] = wire["model"]
    msg = local_message(wire)
    if wire["model"].startswith("deepseek/"):
        raw["provider"] = "Fireworks"
        msg["content"] = "I need help with my bank account." if len(wire["messages"]) == 2 else "###STOP###"
    raw["choices"][0].update(message=msg, finish_reason="tool_calls" if msg.get("tool_calls") else "stop")
    return Reply(raw)


class LocalFixture:
    def __init__(self, python, checkpoint, model, seed, **kwargs):
        self.model = dict(id=model, selected_endpoint=dict(provider_name="local-transformers"))
        self.runtime = dict(transport="scripted_fixture")

    def respond(self, wire):
        msg = local_message(wire)
        # These token counts are fixture receipts.
        rendered = canonical(wire).decode()
        return dict(
            model=self.model["id"],
            provider="local-transformers",
            usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost=0),
            local_generation=dict(
                rendered_input=rendered,
                input_sha256=digest(rendered.encode()),
                prompt_tokens=1,
                completion_tokens=1,
            ),
            choices=[dict(message=msg, finish_reason="tool_calls" if msg.get("tool_calls") else "stop")],
        )

    def close(self):
        pass


def publish(root, name, value):
    path = root / name
    path.write_bytes(canonical(value) + b"\n")
    return dict(path=name, sha256=digest(path.read_bytes()))


def invoke(cmd, **kwargs):
    command = {
        "scripts.run_banking_policy_cycle": bank.main,
        "agent_memory_eval.runner.bfcl_relevance": bfcl_relevance.main,
    }[cmd[2]]
    with patch.object(sys, "argv", cmd[2:]), redirect_stdout(io.StringIO()):
        command()


def check(root, checkout, delivery):
    sys.addaudithook(block_network)
    root.mkdir(parents=True, exist_ok=False)
    (root / "data").mkdir()
    (root / "data/tau2-bench").symlink_to((ROOT / "data/tau2-bench").resolve(), target_is_directory=True)
    shutil.copytree(ROOT / "protocols", root / "protocols")
    template = json.loads((root / "protocols/diagnostics/portable-writer-round-v1.json").read_bytes())
    template["policy"]["reader"] = delivery
    if delivery == "on_demand":
        # Deliberately keep both assets outside the native runner's implementation checkout.
        instruction = root / "protocols/instructions/fixture-optional-reader.txt"
        instruction.write_text(ON_DEMAND_NOTICE + "\n", encoding="utf-8")
        authored = template["policy"]["candidate_instruction"]
        candidate = root / "protocols/instructions/fixture-candidate-writer.txt"
        candidate.write_bytes((root / authored["path"]).read_bytes())
        template["policy"]["candidate_instruction"] = dict(
            path=str(candidate.relative_to(root)), sha256=authored["sha256"]
        )
        template["policy"]["reader_instruction"] = dict(
            path=str(instruction.relative_to(root)), sha256=digest(instruction.read_bytes())
        )
    for evidence in template["evidence"]:
        path = Path(evidence["artifact"]["path"])
        (root / path.parent).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, root / path)
    a = json.loads((root / "protocols/pilots/tau-banking-opening-writer-pairs-v1.json").read_bytes())
    task = next(iter(a["tasks"]))
    a.update(
        tasks={task: a["tasks"][task]},
        source=1,
        validation=dict(
            writer_policies=["existing", "candidate"], write=10, solve=10, second_pass_no_memory=0
        ),
    )
    template["benchmarks"]["banking-opening"]["profile"] = publish(root, "bank-fixture.json", a)
    b = json.loads((root / template["benchmarks"]["bfcl-relevance"]["profile"]["path"]).read_bytes())
    b["tasks"] = {"irrelevance": [b["tasks"]["irrelevance"][0]]}
    b.update(model_revision="scripted-fixture", checkpoint_sha256={})
    template["benchmarks"]["bfcl-relevance"]["profile"] = publish(root, "bfcl-fixture.json", b)
    template["id"] = "fixture-policy-round"
    reg = policy_round.register(root, template)
    target, _, _ = policy_round.registered(root, template)
    cfg = bank.read(ROOT / bank.read(ROOT / bank.bank.PROFILE)["base"]["path"])
    models = bank.read(ROOT / cfg["endpoints"]["path"])["models"]
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, OPENROUTER_API_KEY="in-process-fixture"))
        stack.enter_context(
            patch.object(forms_batch, "OpenRouterClient", partial(OpenRouterClient, opener=bank_reply))
        )
        stack.enter_context(
            patch.object(
                run_full_retail, "ProcessPoolExecutor", lambda **kwargs: ThreadPoolExecutor(max_workers=1)
            )
        )
        stack.enter_context(
            patch.object(
                bank.bank.matched,
                "study_models",
                lambda *args: ({r: models[r] for r in ("worker", "user_simulator")}, None),
            )
        )
        stack.enter_context(patch.object(bfcl_relevance, "LocalQwen", LocalFixture))
        for adapter in policy_round.ADAPTERS:
            policy_round.execute(
                root, template, adapter, preflight=True, checkout=checkout, command_runner=invoke
            )
        preflight = bank.read(target / "bank-preflight.json")
        publish(
            target,
            "bank-authorization.json",
            dict(
                scope="full",
                assigned=preflight["assigned"],
                maximum_usd="1",
                protocol_sha256=preflight["protocol_sha256"],
                preflight_sha256=digest(canonical(preflight)),
                user_authorization="Scripted fixture with network disabled",
            ),
        )
        for adapter in policy_round.ADAPTERS:
            policy_round.execute(
                root,
                template,
                adapter,
                checkout=checkout,
                model_python=Path(sys.executable),
                checkpoint=root / "scripted-fixture",
                command_runner=invoke,
            )
        decision = policy_round.decide(root, template, checkout, "hold", "Scripted integration check.")
    report = bank.read(target / "results.json")
    for result in report["benchmarks"].values():
        assert result["writers"]["assigned"] == result["writers"]["observed"] == 10
        assert result["study_cost"]["confirmed_usd"] in {"0", "0.000000000"}
        assert {c["condition"]: (c["started"], c["graded"]) for c in result["by_condition"]} == {
            "no_memory": (1, 1),
            "existing": (5, 5),
            "candidate": (5, 5),
        }
        assert result["policy_comparisons"][0]["outcomes"]["assigned"] == 5
        for cond in result["by_condition"]:
            if cond["phase"] == "solve":
                assert cond["decisions_with_body"] == 5
                assert cond["read_calls"] == (
                    {"memory_list": 5, "memory_get": 5} if delivery == "on_demand" else {}
                )
        assert (
            sum(
                c["first_decision"]["no_call"]["success"] + c["first_decision"]["no_call"]["failure"]
                for c in result["by_condition"]
            )
            == 11
        )
    return dict(
        transport="scripted_fixture",
        external_calls=0,
        delivery=delivery,
        sources=2,
        writers=20,
        readers=20,
        policy_sha256=reg["policy_sha256"],
        decision=decision["decision"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bfcl-checkout", type=Path, required=True)
    parser.add_argument("--delivery", choices=("preload", "on_demand"), default="preload")
    args = parser.parse_args()
    print(json.dumps(check(args.output.resolve(), args.bfcl_checkout.resolve(), args.delivery)))

"""Transport for the pinned τ² native NL assertion evaluator, isolated from the conversational roles."""

from collections import Counter
from contextlib import contextmanager
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, read_json
from agent_memory_eval.recording.provenance import RecordingGateway, SourceRegistry


def judge_wire(config, model, messages, call_name, kwargs):
    if (
        model != config["native_model"]
        or call_name != "nl_assertions_eval"
        or kwargs != {"reasoning_effort": config["parameters"]["reasoning"]["effort"]}
        or [m.role for m in messages] != ["system", "user"]
    ):
        raise ContractError("Unexpected native grading request or model configuration")
    return dict(
        model=config["model"],
        provider=config["route"],
        stream=False,
        **config["parameters"],
        messages=[dict(role=m.role, content=m.content) for m in messages],
    )


def checked_answer(raw, assertions, config, model):
    from tau2.data_model.message import AssistantMessage

    if (
        raw.get("model") not in {config["model"], config["canonical_slug"]}
        or raw.get("provider") != model["selected_endpoint"]["provider_name"]
        or len(raw.get("choices", [])) != 1
        or raw["choices"][0].get("finish_reason") != "stop"
    ):
        raise ContractError("Unexpected native judge identity or incomplete response")
    msg = raw["choices"][0]["message"]
    body = read_json(msg["content"].encode()).get("results")
    if (
        msg.get("role") != "assistant"
        or not isinstance(body, list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("expectedOutcome"), str)
            or type(item.get("metExpectation")) is not bool
            or not isinstance(item.get("reasoning"), str)
            for item in body
        )
        or Counter(item["expectedOutcome"] for item in body) != Counter(assertions)
    ):
        raise ContractError(
            "Native judge did not return all declared assertions exactly once"
        )
    return AssistantMessage(role="assistant", content=msg["content"])


@contextmanager
def native_grade_transport(journal, ctx, task, specification, client, native_calls):
    """Record exact native judge inputs and provider replies under the grader-only audience."""
    from tau2.runner import simulation as runner

    config, model = specification
    ctx = ctx.at("grade")
    registry = SourceRegistry(journal, ctx)
    used = False
    origin = runner.evaluate_simulation

    def capture(**kwargs):
        native_calls["before_grade_ref"] = journal.blobs.put(
            kwargs["simulation"].model_dump(mode="json")
        )
        return origin(**kwargs)

    def generate(*, model, messages, call_name, **kwargs):
        nonlocal used
        if used or not task.evaluation_criteria.nl_assertions:
            raise ContractError("Unexpected duplicate or inactive native judge")
        used = True
        wire = judge_wire(config, model, messages, call_name, kwargs)
        entries = []
        for msg in wire.pop("messages"):
            event = ctx.event(
                "business.transition",
                dict(
                    origin="tau2.nl_assertions",
                    actor_role="grader",
                    model_message_ref=journal.blobs.put(msg),
                ),
            )
            journal.append([event])
            entries.append(
                (
                    registry.add(
                        "benchmark_message", event["event_id"], roles=("grader",)
                    ),
                    "",
                )
            )
        template = registry.template(wire, roles=("grader",))
        _, _, req = RecordingGateway(journal).call(
            ctx, registry, "grader", template, entries, client, max_requests=201
        )
        finish = next(
            e["payload"]
            for e in reversed(journal.events.values())
            if e["type"] == "model.request_finished"
            and e["payload"]["request_id"] == req
        )
        return checked_answer(
            journal.blobs.get(finish["response_ref"]),
            task.evaluation_criteria.nl_assertions,
            config,
            specification[1],
        )

    with (
        patch("tau2.evaluator.evaluator_nl_assertions.generate", generate),
        patch.object(runner, "evaluate_simulation", capture),
    ):
        yield

"""Write the registered τ² cohort memories against an independently funded continuation."""

from decimal import Decimal, ROUND_DOWN
import os
from pathlib import Path

from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import endpoint_terms
from agent_memory_eval.models.openrouter import model_catalog, model_endpoints, resolve_endpoint, resolve_model
from agent_memory_eval.recording.assets import asset_bytes
from agent_memory_eval.study.adapters.tau_banking import bank_shard
from agent_memory_eval.study.orchestrator import run_stages
from agent_memory_eval.study.records import fixed, read
from agent_memory_eval.study.writer_stage import verify_continuation, verify_tau_sources


def _writer_models(root, cfg, worker, *, live):
    # Preserve the usual shard control. Only the worker can receive a request in this stage.
    models = read_json(asset_bytes(root, cfg["endpoints"]))["models"]
    if live:
        require(os.environ.get("OPENROUTER_API_KEY"), "Missing OpenRouter writer credentials")
        parameters = worker["parameters"]
        model = resolve_model(model_catalog(), worker["model_id"], parameters)
        models["worker"] = resolve_endpoint(model, model_endpoints(model["id"]), worker["provider"],
                                           dict(parameters, tools=[], tool_choice="required"))
    require(endpoint_terms(models) == {k: cfg["expected_endpoint_terms"][k] for k in ("worker", "user_simulator")}
            and {k: m.get("canonical_slug") for k, m in models.items()} == cfg["expected_model_revisions"],
            "The registered writer revision, provider or prices changed")
    return models


def _check_writer_files(folder, cases, sources, width, continuation):
    """Bind existing work to this grant and reject gaps that could resend a paid shard."""
    marker = folder / "write-continuation.json"
    checkpoint = folder / "through-write.json"
    stages = {p.parent.name for p in folder.glob("task-*/write")}
    waves = {p.name for p in folder.glob("write-wave-*.json")}
    if not marker.exists():
        require(not (stages or waves or checkpoint.exists() or (folder / "completion.json").exists()
                     or any(folder.glob("solve*-wave-*.json")) or any(folder.glob("task-*/solve*"))),
                "Post-source evidence already exists without this writer continuation")
        return
    require(read(marker) == continuation, "Existing writer continuation has a different scope")
    expected = [(f"write-wave-{i // width + 1}.json", cases[i:i + width]) for i in range(0, len(cases), width)]
    started = expected[:len(waves)]
    require(waves == {name for name, _ in started}
            and stages <= {"task-" + c["task"] for _, wave in started for c in wave},
            "Writer evidence is missing its registered wave")
    if checkpoint.exists():
        require(len(started) == len(expected) and set(read(checkpoint)["stages"]) ==
                {f"task-{c['task']}/write" for c in cases}, "Writer checkpoint differs from the registered schedule")
    else:
        require(not (folder / "completion.json").exists(), "Completed study is missing its writer checkpoint")
    for name, wave in started[:-1]:
        if Decimal(read(folder / name)["per_shard_grant"]) > 0:
            require(all(sources[c["task"]]["success"] is None
                        or (folder / ("task-" + c["task"]) / "write/completion.json").is_file() for c in wave),
                    "A previously dispatched writer wave is missing a completed shard")


def run_writers(adapter, plan, workspace, root, *, resolved, registration, continuation, sources, progress=None):
    workspace, root = Path(workspace), Path(root)
    folder = workspace / "runs" / plan["id"]
    cfg = dict(resolved["cfg"], effective_sources=sources)
    record = resolved["record"]
    cases = [case for case in record["cases"] if "source" in case["scheduled_stages"]]
    width = cfg["max_concurrent"]
    common = dict(workspace=str(workspace), output=str(folder), cfg=cfg, conn=resolved["conn"],
                  identity=continuation["implementation"], tasks=resolved["tasks"])

    def register():
        cap = verify_continuation(plan, workspace, root, continuation, registration, sources)
        require(record == registration["preflight"] == read(folder / "resolved-plan.json") and
                cfg["registration_sha256"] == registration["approval"]["resolved_sha256"] == digest(canonical(record))
                and cfg["expected_endpoint_terms"] == registration["endpoint_terms"] and adapter.name == record["adapter"],
                "The resolved τ² controls differ from the original source registration")
        forms = set(plan["writing"]["representations"])
        require(cases and len(forms) == 2 and len(cases) == len(sources)
                and set(sources) == {c["task"] for c in cases}
                and all(c["scheduled_stages"] == ["source", "write"] and len(c["representations"]) == 2
                        and set(c["representations"]) == forms for c in cases)
                and set(read(folder / "through-source.json")["stages"]) == {f"task-{c['task']}/source" for c in cases},
                "Writer continuation needs both registered forms for every original source draw")
        _check_writer_files(folder, cases, sources, width, continuation)
        verify_tau_sources(workspace, record, sources)
        worker = resolved["conn"]["worker"]
        pending = any(sources[c["task"]]["success"] is not None and
                      not (folder / ("task-" + c["task"]) / "write/completion.json").is_file() for c in cases)
        if pending:
            # The registered first-wave minimum applies to the writer's own maximum request.
            rates = cfg["expected_endpoint_terms"]["worker"]["reservation_pricing"]
            largest = (cfg["writing"]["proposed_limits"]["input_bytes"] * Decimal(str(rates["prompt"]))
                       + worker["parameters"]["max_completion_tokens"] * Decimal(str(rates["completion"])))
            grant = (cap / min(width, len(cases))).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            require(grant >= 2 * largest, "First writer wave must cover twice the largest worker reservation")
        common["models"] = _writer_models(root, cfg, worker, live=pending)
        fixed(folder / "write-continuation.json", continuation)

    def verify_sealed(case, stage, grant):
        # bank_shard checks the sealed design, released source, implementation, endpoints and individual jobs.
        bank_shard((common, case, stage, grant))

    result = run_stages(folder, registration, [("write", "write", cases)], (bank_shard, common),
                        total_usd_limit=continuation["max_usd"], max_concurrent=width, through_stage="write",
                        register_continuation=register, admission=adapter._admission(cfg), verify_sealed=verify_sealed,
                        progress=progress)
    return dict(plan_id=plan["id"], completed_through=result["completed_through"], budget_deferred=result["budget_deferred"],
                total_covered_usd=result["total_covered_usd"], writer_shards=len(result["stages"]),
                checkpoint_sha256=digest((folder / "through-write.json").read_bytes()) if result["completed_through"] else None)

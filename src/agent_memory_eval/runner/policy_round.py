"""Register, run and read a two-pass writer comparison on native banking and BFCL."""

import argparse
from pathlib import Path
import subprocess
import sys
from jsonschema import Draft202012Validator

from agent_memory_eval.adapters.bfcl_task_start import REVISION
from agent_memory_eval.adapters.tau_banking_results import known_customer_groups
from agent_memory_eval.analysis.recorded_decisions import require
from agent_memory_eval.contracts.core import canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT, SCHEMA_ROOT
from agent_memory_eval.experiments.paired_repetitions import FORMS
from agent_memory_eval.policies.writer_pairs import writer_pairs
from agent_memory_eval.recording.assets import asset_text, publish_json
from agent_memory_eval.recording.journal import safe_path


ADAPTERS = ("banking-opening", "bfcl-relevance")


def pinned_json(workspace, ref):
    return read_json(asset_text(workspace, ref).encode())


def compile_round(workspace, spec):
    """Derive both native configurations from the same authored policy and frozen comparison criteria."""
    schema = read_json((SCHEMA_ROOT / "policy-round-v1.schema.json").read_bytes())
    error = next(Draft202012Validator(schema).iter_errors(spec), None)
    require(error is None, f"Invalid policy round: {error.message if error else ''}")
    evidence_tasks = {adapter: set() for adapter in ADAPTERS}
    for evidence in spec["evidence"]:
        asset_text(workspace, evidence["artifact"])
        evidence_tasks[evidence["benchmark"]].update(evidence["tasks"])
    if spec.get("previous_decision"):
        previous = pinned_json(workspace, spec["previous_decision"])
        require(previous.get("format") == "agent-memory-policy-decision/v1", "Invalid preceding decision")
        for adapter in ADAPTERS:
            cohort = previous["cohorts"][adapter]
            evidence_tasks[adapter].update(cohort["development_tasks"], cohort["evaluated_tasks"])
    policy = spec["policy"]
    strict = pinned_json(workspace, policy["writing"])
    base_ref = strict["common_instruction_ref"]
    base = asset_text(workspace, base_ref).rstrip("\r\n")
    clause = asset_text(workspace, policy["clause"]).rstrip("\r\n")
    candidate = asset_text(workspace, policy["candidate_instruction"]).rstrip("\r\n")
    notice = asset_text(workspace, policy["reader_instruction"])
    require(
        candidate == base + "\n" + clause and bool(notice.strip())
        and policy["reader"] in {"on_demand", "preload"}
        and set(strict["lines"]) == set(FORMS)
        and strict["completion_contract"] == "writer-required-store/v1"
        and policy["feedback"] == "result_only",
        "Unsupported policy or candidate differs beyond its registered clause",
    )
    conditions, pairs = writer_pairs(strict["lines"], clause)
    fingerprint = digest(
        canonical(
            dict(
                base=base,
                clause=clause,
                forms=strict["lines"],
                delivery=policy["reader"],
                reader_instruction=notice,
                feedback=policy["feedback"],
                max_items=1,
                completion_contract=strict["completion_contract"],
            )
        )
    )
    seal = dict(
        design_sha256=digest(canonical(spec)),
        policy_sha256=fingerprint,
        reader_instruction=policy["reader_instruction"],
    )
    root = Path("runs") / spec["id"]
    profiles, cohorts = {}, {}
    for adapter in ADAPTERS:
        settings = spec["benchmarks"][adapter]
        cfg = pinned_json(workspace, settings["profile"])
        observed = sorted(evidence_tasks[adapter].union(settings["development_tasks"]))
        if adapter == "banking-opening":
            require(
                cfg["adapter"] == "account_opening"
                and cfg["format"] in {"banking-registered-writer-pairs/v1", "banking-registered-writer-pairs/v3"},
                "Expected a registered banking cohort",
            )
            tasks = sorted(cfg["tasks"])
            groups = known_customer_groups(
                workspace / "data/tau2-bench/data/tau2/domains/banking_knowledge", observed, tasks
            )
            if spec["purpose"] == "confirmation":
                require(
                    all(
                        g["identified"] and not g["overlapping_development"] and not g["unresolved_development"]
                        for g in groups.values()
                    ),
                    "Development and confirmation customers overlap or are unresolved",
                )
            cfg.update(
                required_writer=policy["writing"],
                base_instruction=base_ref,
                candidate_instruction=policy["candidate_instruction"],
                clause=policy["clause"],
                feedback=policy["feedback"],
                reader=policy["reader"],
                source_study=spec["id"] + "-bank-sources",
                validation_study=spec["id"] + "-bank-readers",
                preflight_file=str(root / "bank-preflight.json"),
                authorization_file=str(root / "bank-authorization.json"),
            )
        else:
            require(cfg["revision"] == REVISION, "Unsupported BFCL profile")
            tasks = [t for ids in cfg["tasks"].values() for t in ids]
            require(len(tasks) == len(set(tasks)), "Duplicate BFCL question")
            families = settings["question_families"]
            require(
                all(isinstance(v, str) and v.strip() for v in families.values())
                and set(families) == set(observed) | set(tasks),
                "Missing BFCL development or confirmation family",
            )
            if spec["purpose"] == "confirmation":
                require(
                    {families[t] for t in observed}.isdisjoint(families[t] for t in tasks),
                    "Development and confirmation question families overlap",
                )
            cfg.update(
                name=spec["id"] + "-bfcl",
                purpose=spec["hypothesis"],
                release="One committed item admits its own reader. Each reader starts a fresh session.",
                writer_instruction=base,
                forms={c["id"]: c["prompt"] for c in conditions},
                policy_writer_pairs=pairs,
                policy_question_families={t: families[t] for t in tasks},
                delivery=[policy["reader"]],
                memory_notice=notice,
            )
        cfg["policy_registration"] = seal
        cfg["decision_criterion"] = spec["criterion"]
        profiles[adapter] = cfg
        cohorts[adapter] = dict(evaluated_tasks=tasks, development_tasks=observed)
    registration = dict(
        format="agent-memory-policy-registration/v1",
        **seal,
        purpose=spec["purpose"],
        criterion=spec["criterion"],
        cohorts=cohorts,
        profiles={k: digest(canonical(v)) for k, v in profiles.items()},
    )
    return registration, profiles


def native_directories(workspace, profiles):
    bank = profiles["banking-opening"]
    return [
        safe_path(workspace / "runs", name)
        for name in (bank["source_study"], bank["validation_study"], profiles["bfcl-relevance"]["name"])
    ]


def verify_native_registration(profile, cfg, adapter):
    """Also enforce the pre-run binding when a native command is invoked directly."""
    root = Path(profile).parent
    receipt = read_json((root / "registration.json").read_bytes())
    design = read_json((root / "design.json").read_bytes())
    seal = cfg["policy_registration"]
    require(
        receipt["format"] == "agent-memory-policy-registration/v1"
        and digest(canonical(design)) == receipt["design_sha256"] == seal["design_sha256"]
        and receipt["policy_sha256"] == seal["policy_sha256"]
        and receipt["profiles"][adapter] == digest(canonical(cfg)),
        "Native policy or criterion differs from its pre-run registration",
    )


def register(workspace, spec):
    receipt, profiles = compile_round(workspace, spec)
    root = safe_path(workspace / "runs", spec["id"])
    require(
        not any(p.exists() for p in native_directories(workspace, profiles)),
        "Native work predates the registration",
    )
    root.mkdir(parents=True, exist_ok=True)
    # Write the receipt last. An interrupted registration is recoverable only with the same preregistered inputs.
    for name, value in [("design", spec), *profiles.items(), ("registration", receipt)]:
        path = root / (name + ".json")
        try:
            publish_json(path, value)
        except FileExistsError:
            require(read_json(path.read_bytes()) == value, "Existing registration differs")
    return receipt


def registered(workspace, spec):
    root = safe_path(workspace / "runs", spec["id"])
    receipt, profiles = compile_round(workspace, spec)
    require(
        read_json((root / "registration.json").read_bytes()) == receipt
        and read_json((root / "design.json").read_bytes()) == spec,
        "Registered design or criterion changed",
    )
    for adapter, cfg in profiles.items():
        require(
            read_json((root / (adapter + ".json")).read_bytes()) == cfg, "Registered native configuration changed"
        )
    return root, receipt, profiles


def execute(
    workspace,
    spec,
    adapter,
    *,
    preflight=False,
    model_python=None,
    checkpoint=None,
    checkout=None,
    command_runner=subprocess.run,
):
    root, _, profiles = registered(workspace, spec)
    profile = root / (adapter + ".json")
    if adapter == "banking-opening":
        # The native command enforces its affirmative per-study authorization, ledger and recovery policy.
        for phase in ("preflight",) if preflight else ("sources", "validation"):
            cmd = [
                sys.executable,
                "-m",
                "scripts.run_banking_policy_cycle",
                phase,
                "--workspace",
                str(workspace),
                "--profile",
                str(profile),
            ]
            if phase == "preflight":
                cmd.extend(["--output", str(root / "bank-preflight.json")])
            command_runner(cmd, cwd=ROOT, check=True)
    else:
        from agent_memory_eval.runner.bfcl_relevance import load_inputs

        require(checkout is not None, "Specify the pinned BFCL checkout")
        cases, _ = load_inputs(checkout, profiles[adapter])
        if preflight:
            return dict(tasks=len(cases), writers_per_task=len(profiles[adapter]["forms"]), external_calls=0)
        require(model_python and checkpoint, "BFCL execution needs the registered local model")
        directory = native_directories(workspace, profiles)[2]
        command_runner(
            [
                sys.executable,
                "-m",
                "agent_memory_eval.runner.bfcl_relevance",
                "run",
                "--profile",
                str(profile),
                "--checkout",
                str(Path(checkout).resolve()),
                "--recording",
                str(directory),
                "--model-python",
                str(Path(model_python).absolute()),
                "--checkpoint",
                str(Path(checkpoint).resolve()),
            ],
            cwd=workspace,
            check=True,
        )


def results(workspace, spec, checkout):
    from agent_memory_eval.adapters.banking_trajectories import banking_report
    from agent_memory_eval.adapters.bfcl_trajectories import bfcl_report
    from agent_memory_eval.analysis.policy_evidence import PolicyEvidence

    root, receipt, profiles = registered(workspace, spec)
    require(checkout is not None, "Specify the pinned BFCL checkout")
    common = profiles["bfcl-relevance"]
    expected = {
        (form, arm): digest((common["writer_instruction"] + "\n" + common["forms"][pair[key]]).encode())
        for form, pair in common["policy_writer_pairs"].items()
        for arm, key in (("existing", "reference"), ("candidate", "candidate"))
    }
    reports = {}
    for adapter in ADAPTERS:
        collector = PolicyEvidence()
        profile = root / (adapter + ".json")
        if adapter == "banking-opening":
            raw = banking_report(profile, workspace, workspace / "data/tau2-bench", False, collector=collector)
        else:
            raw = bfcl_report(
                profile, native_directories(workspace, profiles)[2], checkout, False, collector=collector
            )
        collector.verify_policy(expected, spec["policy"]["reader"], raw["observations"])
        reports[adapter] = collector.report(raw, summary=True)
    return dict(registration_sha256=digest(canonical(receipt)), benchmarks=reports)


def decide(workspace, spec, checkout, decision, reason):
    require(
        decision in {"adopt", "hold", "reject"} and isinstance(reason, str) and reason.strip(),
        "Missing decision or reason",
    )
    require(
        decision != "adopt" or spec["purpose"] == "confirmation", "Policy adoption requires a confirmation round"
    )
    report = results(workspace, spec, checkout)
    root = safe_path(workspace / "runs", spec["id"])
    record = dict(
        format="agent-memory-policy-decision/v1",
        registration_sha256=report["registration_sha256"],
        results_sha256=digest(canonical(report)),
        cohorts=registered(workspace, spec)[1]["cohorts"],
        decision=decision,
        reason=reason,
    )
    for filename, value in (("results.json", report), ("decision.json", record)):
        try:
            publish_json(root / filename, value)
        except FileExistsError:
            require(
                read_json((root / filename).read_bytes()) == value,
                "This result or decision already has a different value",
            )
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "register", "preflight", "run", "report", "decide"))
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--adapter", choices=ADAPTERS)
    parser.add_argument("--bfcl-checkout", type=Path)
    parser.add_argument("--model-python", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--decision", choices=("adopt", "hold", "reject"))
    parser.add_argument("--reason")
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    spec = read_json(args.plan.read_bytes())
    try:
        if args.stage == "prepare":
            value = compile_round(workspace, spec)[0]
        elif args.stage == "register":
            value = register(workspace, spec)
        elif args.stage in {"preflight", "run"}:
            if not args.adapter:
                parser.error("--adapter is required for a native preflight or run")
            value = execute(
                workspace,
                spec,
                args.adapter,
                preflight=args.stage == "preflight",
                model_python=args.model_python,
                checkpoint=args.checkpoint,
                checkout=args.bfcl_checkout,
            )
        elif args.stage == "report":
            value = results(workspace, spec, args.bfcl_checkout)
        else:
            value = decide(workspace, spec, args.bfcl_checkout, args.decision, args.reason)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    if value is not None:
        print(canonical(value).decode())


if __name__ == "__main__":
    main()

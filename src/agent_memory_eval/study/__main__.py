"""Declared memory-policy studies: validate, resolve, authorize, run, resume and analyze one plan file."""

import argparse
import json
from pathlib import Path
import sys

from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.study.adapters import adapter_for
from agent_memory_eval.study.plan import conditions, load_plan, plan_digest, require_declared_models
from agent_memory_eval.study.records import fixed, read
from agent_memory_eval.study.scorecard import scorecard


def approval_path(plan):
    """Approvals are kept with the plans in the code repository; run data stays in the workspace."""
    return ROOT / "artifacts" / "approvals" / f"{plan['id']}.json"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m agent_memory_eval.study", description=__doc__)
    parser.add_argument("command", choices=["plan", "authorize", "run", "resume", "analyze"])
    parser.add_argument("action", nargs="?", choices=["validate", "resolve"])
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=ROOT, help="Folder holding data/, runs/ and artifacts/")
    parser.add_argument("--checkout", type=Path, help="Pinned BFCL checkout")
    parser.add_argument("--model-python", type=Path, help="Interpreter of the local model runtime")
    parser.add_argument("--checkpoint", type=Path, help="Pinned local model checkpoint")
    parser.add_argument("--max-usd", help="Cumulative ceiling a person approved")
    parser.add_argument("--quote", help="The person's own words approving the ceiling")
    parser.add_argument("--tasks", help="Comma-separated task ids: score only these tasks (for example the confirmation set)")
    parser.add_argument("--save", action="store_true", help="Keep rows and scorecard under artifacts/studies/<plan id>/")
    parser.add_argument("--reviews", type=Path, help="Follow-up transcript labels keyed by public-turn hash")
    parser.add_argument("--review-packets", type=Path, help="Export public transcripts for blinded review to a new JSON file")
    args = parser.parse_args(argv)
    plan = load_plan(args.plan, ROOT)
    if args.command in {"authorize", "run", "resume"}:
        require_declared_models(plan)
    adapter = adapter_for(plan["benchmark"]["adapter"])
    workspace = args.workspace.resolve()
    staged = hasattr(adapter, "resolve")  # τ² domains resolve paid stages; BFCL compiles a local profile.
    followup = hasattr(adapter, "analyze")
    if (args.reviews or args.review_packets) and (not followup or args.command != "analyze"):
        parser.error("Transcript review options apply to follow-up analysis")
    if followup and args.tasks:
        parser.error("Use the complete preregistered follow-up cohort")
    if args.command == "plan":
        if args.action == "validate":
            out = dict(plan=plan["id"], sha256=plan_digest(plan), adapter=adapter.name)
            if followup:
                out["comparison"] = plan["comparison"]
                if "scoring" in plan:
                    out["scoring"] = plan["scoring"]
            else:
                out["conditions"] = conditions(plan)
        elif staged:
            record = adapter.resolve(plan, workspace, ROOT)["record"]
            out = record if followup else dict(plan=plan["id"], assigned=record["assigned"], stages=record["stages"], cases=len(record["cases"]))
        else:
            cfg = adapter.compile(plan, ROOT)
            out = dict(plan=plan["id"], tasks=cfg["tasks"], conditions=cfg["conditions"])
    elif args.command == "authorize":
        from agent_memory_eval.study.adapters.tau_banking import approval_for
        if not staged:
            parser.error("This local adapter has no provider spending authorization")
        if not (args.max_usd and args.quote and args.quote.strip()):
            parser.error("Record the approved ceiling and the person's own words")
        if followup:
            from agent_memory_eval.study.adapters.retail_revisit import approval_amount
            approval_amount(args.max_usd)
        record = adapter.resolve(plan, workspace, ROOT)["record"]
        path = approval_path(plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        fixed(path, approval_for(plan, record, max_usd=args.max_usd, user_authorization=args.quote))
        out = dict(approval=str(path.relative_to(ROOT)), assigned=record["assigned"], max_usd=args.max_usd)
    elif args.command in {"run", "resume"}:
        if staged:
            out = adapter.run(plan, workspace, ROOT, approval=read(approval_path(plan)),
                              progress=lambda label, done, total, spent: print(label, done, "of", total, "USD", spent,
                                                                              file=sys.stderr, flush=True))
        else:
            if not (args.checkout and args.model_python and args.checkpoint):
                parser.error("A BFCL run needs the pinned checkout, the model interpreter and the checkpoint")
            out = adapter.run(plan, workspace, ROOT, checkout=args.checkout, model_python=args.model_python,
                              checkpoint=args.checkpoint)
    else:
        from agent_memory_eval.study.scorecard import save_analysis
        tasks = args.tasks.split(",") if args.tasks else None
        if followup:
            rows, out = adapter.analyze(plan, workspace, ROOT, reviews=args.reviews, review_packets=args.review_packets)
        else:
            kwargs = dict(checkout=args.checkout) if adapter.name == "bfcl-relevance" else {}
            rows = adapter.outcomes(plan, workspace, ROOT, **kwargs)
            if tasks is not None:
                unknown = set(tasks) - set(plan["benchmark"]["tasks"])
                if unknown:
                    parser.error(f"Tasks outside the plan: {sorted(unknown)}")
                rows = [r for r in rows if r["task"] in tasks]
            out = scorecard(rows)
        if args.save:
            out = dict(out, saved=save_analysis(ROOT, plan["id"], rows, out, tasks=tasks))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

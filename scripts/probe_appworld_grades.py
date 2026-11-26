"""Run one AppWorld task's official grader on prepared DB copies in its own interpreter."""

import hashlib
import json
from importlib.metadata import distribution
from pathlib import Path
import sys


def probe(task, spec):
    package = distribution("appworld")
    origin = json.loads(package.read_text("direct_url.json"))
    app_bundle = package.locate_file("appworld/.source/apps.bundle")
    evaluator = package.locate_file("appworld/evaluator.py")
    if (origin["vcs_info"]["commit_id"] != spec["revision"]
            or hashlib.sha256(evaluator.read_bytes()).hexdigest() != spec["evaluator_sha256"]
            or hashlib.sha256(app_bundle.read_bytes()).hexdigest() != spec["apps_bundle_sha256"]):
        raise ValueError("Unexpected AppWorld package, grader or app-code bundle")
    from appworld import evaluate_task, update_root
    from appworld.evaluator import CachedDBHandler

    update_root(str(Path.cwd()))
    grades = {}
    for state in ("initial", "recorded", "initial"):
        CachedDBHandler.reset()
        tracker = evaluate_task(task, state, save_report=False)
        if tracker.total_count != tracker.num_tests or not tracker.num_tests:
            raise ValueError("AppWorld grade did not cover every requirement")
        requirements = sorted((r["requirement"], r["label"], passed) for passed, group in
                              ((True, tracker.passes), (False, tracker.failures)) for r in group)
        grade = dict(success=tracker.success, passed=tracker.pass_count, failed=tracker.fail_count,
                     required=tracker.num_tests, passed_no_op=sum(p and label == "no_op_pass" for _, label, p in requirements),
                     requirements_sha256=hashlib.sha256(json.dumps(requirements, ensure_ascii=False).encode()).hexdigest())
        if state in grades and grade != grades[state]:
            raise ValueError("The initial DB grade changed after evaluating the recorded DB")
        grades[state] = grade
    return grades


if __name__ == "__main__":
    print(json.dumps(probe(sys.argv[1], json.loads(Path(sys.argv[2]).read_bytes())["appworld"])))

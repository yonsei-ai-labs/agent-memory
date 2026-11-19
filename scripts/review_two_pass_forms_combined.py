"""Reproduce the sealed original and supplemental reports in an isolated offline checkout."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from regrade_two_pass_forms import regrade

from agent_memory_eval.analysis.forms_supplement import audit
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.assets import asset_text


ROOT = Path(__file__).resolve().parents[1]
LOCK = "artifacts/pilots/20261118-two-pass-forms-combined-reproduction-lock.json"


def review(root):
    lock = read_json((root / LOCK).read_bytes())
    if lock["format"] != "agent-memory-two-pass-forms-combined-reproduction-lock/v1":
        raise ContractError("Unexpected combined study reproduction lock")
    profile = lock["supplement_profile_ref"]
    asset_text(root, profile)
    with TemporaryDirectory() as temporary:
        outputs = Path(temporary) / "audit"
        receipt = audit(root, root / profile["path"], outputs, expected_completion=lock["supplement_completion_sha256"])
        if receipt["sources"]["original"]["completion_sha256"] != lock["original_completion_sha256"]:
            raise ContractError("Combined review differs from the pinned original seal")
        native = regrade(outputs / "combined-view", root, root / "data/tau2-bench")
        if (native["official_results_checked"] != receipt["combined"]["graded_episodes"]
                or native["successes"] != receipt["combined"]["successful_episodes"]):
            raise ContractError("Native strict regrade does not match the audited episode totals")
        write_json(outputs / "native-regrade.json", native)
        files = {"original-results.json": "original_results_ref", "supplement-results.json": "supplement_results_ref",
                 "combined-results.json": "combined_results_ref", "audit.json": "audit_ref",
                 "episode-origins.csv": "origins_ref", "native-regrade.json": "native_regrade_ref"}
        checks = {}
        for generated, name in files.items():
            expected = asset_text(root, lock[name]).encode()
            actual = (outputs / generated).read_bytes()
            if actual != expected:
                raise ContractError("Historical review differs from the published " + generated)
            checks[generated] = digest(actual)
        return dict(status="passed", files=checks, source_seals=receipt["sources"],
                    combined=receipt["combined"], verified_raw_files=receipt["input_file_count"],
                    native_regrade={key: native[key] for key in ("official_results_checked", "successes",
                                    "failure_components", "new_model_or_network_attempts", "benchmark_revision")})


if __name__ == "__main__":
    print(json.dumps(review(ROOT), ensure_ascii=False, indent=2))

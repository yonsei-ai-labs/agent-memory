"""Validate all masked judgments and link each paired action to its actual first-pass memory."""
import argparse
from collections import Counter
from pathlib import Path

from agent_memory_eval.analysis.forms_actions import join_actions
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import write_json


def finalize(run, results, waves, writing, rubric):
    read = lambda p: read_json(p.read_bytes())
    labels, packet_sha = {}, []
    for key in sorted(waves.glob("blocks-*/key.json")):
        for side in (1, 2):
            packet = key.parent / f"actions-{side}.json"
            label_path = key.parent / f"judged-{side}.json"
            source = read(packet)
            if source and not label_path.is_file():
                raise ContractError("A nonempty blinded packet has no judgments")
            judged = read(label_path)["pairs"] if label_path.is_file() else {}
            if set(judged) != set(source) or set(labels).intersection(judged):
                raise ContractError("Blinded pair judgments are missing, duplicated, or from a different packet")
            labels.update(judged)
            packet_sha.append(dict(packet=digest(packet.read_bytes()),
                                   judgments=digest(label_path.read_bytes()) if label_path.is_file() else None))
    data = join_actions(run, read(results), waves, labels)
    ledger = read(run / "allocations.json")
    jobs = {r["index"]: r for r in ledger}
    writers = {(r["task_id"], r["repetition"], r["arm"]): r for r in ledger if r["phase"]=="write"}
    control = read(run / "control.json")
    sources = {s["task_id"]: s for s in control["design"]["source"]["cohort"]}
    writing_reviews = {r["run_id"]: r for r in read(writing)["annotations"]}
    categories = Counter()
    contexts = Counter()
    for pair in data["pairs"].values():
        t = jobs[pair["treatment_job"]]
        writer = writers[t["task_id"], t["repetition"], t["arm"]]
        review = writing_reviews[writer["run_id"]]
        if any(review[k] != writer[k] for k in ("task_id", "repetition", "arm", "index", "status")):
            raise ContractError("Action pair and previously blinded writing judgment refer to different allocations")
        source = sources[t["task_id"]]
        pair["source_experience"] = dict(first_pass_run_id=source["run_id"], first_pass_success=control["source_outcomes"][t["task_id"]],
                                         public_context_sha256=source["public_context_sha256"])
        pair["assigned_writer"] = dict(index=writer["index"], run_id=writer["run_id"],
                                      snapshot_id=writer["result"]["snapshot_id"], **{k:review[k] for k in
                ("status", "item_count", "fact", "episode", "lesson", "procedure", "grounding",
                 "main_note_quote", "main_source_quote", "suspect_quote", "rationale")})
        if pair["label"] in {"gain", "loss"}:
            categories[pair["judgment"]["category"]] += 1
            contexts[pair["judgment"]["user_context"]] += 1
    data.update(format="agent-memory-two-pass-forms-linked-action-review/v1", writing_review_sha256=digest(writing.read_bytes()),
                action_rubric_sha256=digest(rubric.read_bytes()), numeric_result_sha256=digest(results.read_bytes()),
                masked_packets_sha256=digest(canonical(packet_sha)), discordant_action_categories=dict(categories),
                discordant_current_user_context=dict(contexts), scope="One reviewer per outcome/condition-masked case, exact quotes checked before linking to actual note, public source excerpts, native grade and worker exposure. Links describe observed trajectories and do not identify note-content causality.")
    return data


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("run","results","waves","writing","rubric","output"):
        p.add_argument(name,type=Path)
    a=p.parse_args()
    if a.output.exists() or a.output.absolute().is_relative_to(a.run.absolute()):
        raise ContractError("Use a new output outside the evidence root")
    write_json(a.output,finalize(a.run,a.results,a.waves,a.writing,a.rubric))


if __name__=="__main__":
    main()

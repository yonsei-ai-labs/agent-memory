"""Export masked writing or paired-action material from closed two-pass assignments."""
import argparse
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path


def blind_id(kind, value):
    return kind + "_" + digest(canonical(["forms-review/v1", kind, value]))[:20]


def writer_material(run_root, rows):
    sources, bank_rows, joins = {}, {}, {}
    for row in rows:
        if row["phase"] != "write" or "result" not in row:
            continue
        folder = safe_path(run_root, row["directory"])
        result = row["result"]
        if digest((folder / "log.jsonl").read_bytes()) != result["log_sha256"]:
            raise ContractError("Writer journal changed after the allocation was closed")
        scan, state, _ = read_experiment(folder)
        if scan["tail_bytes"] or scan["incomplete_runs"] or scan["pending_requests"]:
            raise ContractError("Refusing to export an active writer")
        blobs = BlobStore(folder)
        assignment = state["form_writer"]
        packet = blobs.get(assignment["experience_ref"])
        receipt = blobs.get(packet["source_ref"])
        source = blobs.get(receipt["public_context_ref"])
        notes = snapshot_items(blobs, state, result["snapshot_id"])
        source_id = blind_id("source", receipt["public_context_sha256"])
        if digest(canonical(source)) != receipt["public_context_sha256"]:
            raise ContractError("Source digest differs from verified projection")
        sources[source_id] = source
        bank_id = blind_id("bank", row["run_id"])
        bank_rows[bank_id] = dict(source_id=source_id, notes=[dict(title=n["title"], body=n["body"]) for n in notes])
        joins[bank_id] = {key: row[key] for key in ("arm", "task_id", "index", "repetition", "status", "run_id")}
    return sources, bank_rows, joins


def action_material(folder, run_id):
    scan, _, _ = read_experiment(folder)
    blobs = BlobStore(folder)
    endings = [e["payload"] for e in scan["events"].values() if e["run_id"] == run_id
               and e["type"] == "business.transition" and e["payload"].get("origin") == "forms.native_result"]
    if len(endings) != 1:
        raise ContractError("A paired case has no unambiguous terminal trajectory")
    report = blobs.get(endings[0]["native_report_ref"])
    public = []
    for message in report["messages"]:
        role = message.get("role")
        if role in {"assistant", "user", "tool"}:
            # Native tool calls store name/arguments without an OpenAI function wrapper.
            m = {k: v for k, v in message.items() if k in {"role", "content", "tool_call_id"}}
            if message.get("tool_calls"):
                m["tool_calls"] = [{k: c[k] for k in ("id", "name", "arguments") if k in c}
                                   for c in message["tool_calls"]]
            public.append(m)
    return dict(public_policy=blobs.get(scan["starts"][run_id]["payload"]["task_ref"])["content"], messages=public)


def write_packets(output, sources, banks, joins):
    # Any two packets are assigned independent source sets; no arm or outcome in either.
    keys = sorted(sources)
    parts = {s: int(s.rsplit("_", 1)[-1], 16) % 2 for s in keys}
    for i in range(2):
        selected = {k: v for k, v in sorted(banks.items()) if parts[v["source_id"]] == i}
        packet = dict(format="masked-writing/v1", sources={s: sources[s] for s in keys if parts[s] == i}, banks=selected)
        write_json(output / f"writing-{i+1}.json", packet)
    write_json(output / "writing-key.json", joins)


def paired_material(raw, rows, contrasts):
    jobs = {r["index"]: r for r in rows}
    trajectories, transcripts, keys = {}, {}, {}
    for contrast in contrasts:
        if contrast["control"] != "no_memory" or contrast["treatment"] not in ("fact", "episode", "lesson", "procedure"):
            continue
        for label, pairs in contrast["review_cases"].items():
            for pair in pairs:
                ids = (pair["control_job"], pair["treatment_job"])
                key = blind_id("pair", [jobs[i]["run_id"] for i in ids])
                order = sorted(ids, key=lambda i: blind_id("side", jobs[i]["run_id"]))
                for i in order:
                    if i not in trajectories:
                        trajectories[i] = action_material(raw / jobs[i]["directory"], jobs[i]["run_id"])
                transcripts[key] = [trajectories[i] for i in order]
                keys[key] = dict(label=label, treatment=contrast["treatment"], jobs_in_blind_order=order, **pair)
    return dict(sorted(transcripts.items())), keys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path, help="A new directory outside the live run")
    parser.add_argument("--paired-results", type=Path, help="Completed independently audited results, for masking mechanism cases")
    args = parser.parse_args()
    raw, output = args.run.resolve(), args.output.absolute()
    if output.is_relative_to(raw) or output.resolve() != output:
        raise ContractError("Keep review material outside the immutable run")
    rows = read_json((raw / "allocations.json").read_bytes())
    output.mkdir(parents=True, exist_ok=False)
    write_packets(output, *writer_material(raw, rows))
    if args.paired_results:
        result = read_json(args.paired_results.read_bytes())
        transcripts, keys = paired_material(raw, rows, result["paired_contrasts"])
        write_json(output / "paired-actions.json", transcripts)
        write_json(output / "paired-key.json", keys)


if __name__ == "__main__":
    main()

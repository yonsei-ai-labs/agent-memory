"""Extract the preregistered payment and reservation timelines for tasks 23 and 42."""
import argparse
import json
from pathlib import Path
import re

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path


def decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def calls(message):
    for call in message.get("tool_calls") or []:
        fn = call.get("function", call)
        yield call["id"], fn["name"], decode(fn["arguments"])


def public_messages(messages):
    return [m for m in messages if m.get("role") in {"assistant", "user", "tool"}]


def timeline(messages, task, past_ids):
    public = public_messages(messages)
    # Public wire projections name the identifier tool_call_id. Native ToolMessage uses id.
    replies = {(m["tool_call_id"] if "tool_call_id" in m else m["id"]): (i, decode(m.get("content")))
               for i, m in enumerate(public) if m["role"] == "tool"}
    profiles, lookups, writes, dialogue = [], [], [], []
    first_mention = {}
    for i, m in enumerate(public):
        role = m["role"]
        content = m.get("content") or ""
        if role in {"assistant", "user"} and content.strip():
            dialogue.append(dict(index=i, role=role, text=content))
            if task == "42":
                for res_id in past_ids:
                    if res_id in content:
                        first_mention.setdefault((role, res_id), i)
        if role != "assistant":
            continue
        for call_id, name, args in calls(m):
            end, result = replies.get(call_id, (None, None))
            base = dict(call_index=i, result_index=end, name=name, arguments=args)
            if name == "get_user_details":
                detail = result if isinstance(result, dict) else {}
                payments = {key: {k: val[k] for k in ("source", "amount", "brand") if k in val}
                            for key, val in detail.get("payment_methods", {}).items()}
                profiles.append(dict(**base, returned_user_id=detail.get("user_id"),
                    returned_reservation_ids=detail.get("reservations"), payment_methods=payments if task == "23" else None))
            if name == "get_reservation_details" and task == "42":
                detail = result if isinstance(result, dict) else {}
                legs = [{k: v for k, v in f.items() if k in ("origin", "destination", "date", "flight_number")}
                        for f in detail.get("flights", [])]
                lookups.append(dict(**base, returned_id=detail.get("reservation_id"), flights=legs))
            if name in {"book_reservation", "cancel_reservation", "update_reservation_flights",
                        "update_reservation_passengers", "update_reservation_baggages", "transfer_to_human_agents"}:
                before = [x for x in dialogue if x["index"] < i]
                last = {r: next((m for m in reversed(before) if m["role"] == r), None) for r in ("assistant", "user")}
                outcome = ({k: result[k] for k in ("reservation_id", "status", "error", "payment_history") if k in result}
                           if isinstance(result, dict) else result)
                writes.append(dict(**base, last_prior_agent_text=last["assistant"], last_prior_customer_text=last["user"],
                                   returned=outcome))
    return dict(profiles=profiles, reservation_lookups=lookups, business_actions=writes, dialogue=dialogue,
                first_public_mentions=[dict(speaker=r, reservation_id=rid, index=i)
                    for (r, rid), i in sorted(first_mention.items(), key=lambda x:x[1])])


def archive_source(run, row):
    folder = safe_path(run, row["directory"])
    _, state, _ = read_experiment(folder)
    blobs = BlobStore(folder)
    imported = blobs.get(state["form_writer"]["experience_ref"])
    receipt = blobs.get(imported["source_ref"])
    public = blobs.get(receipt["public_context_ref"])
    if digest(canonical(public)) != receipt["public_context_sha256"]:
        raise ContractError("Diagnostic source differs from the writer's public experience")
    return public, receipt["public_context_sha256"]


def extract(run):
    ledger = read_json((run / "allocations.json").read_bytes())
    control = read_json((run / "control.json").read_bytes())
    completion = read_json((run / "completion.json").read_bytes())
    if (digest((run / "allocations.json").read_bytes()) != completion["allocation_sha256"]
            or digest((run / "control.json").read_bytes()) != completion["control_sha256"]):
        raise ContractError("Use a sealed source or an audited combined view")
    diagnostics = {}
    for task in ("23", "42"):
        block = [r for r in ledger if r["task_id"] == task]
        source, source_hash = archive_source(run, next(r for r in block if r["phase"] == "write"))
        previous = timeline(source["messages"], task, [])
        ids = sorted({rid for p in previous["profiles"] for rid in p["returned_reservation_ids"] or []})
        previous = timeline(source["messages"], task, ids)
        section = dict(source_public_sha256=source_hash, first_pass_success=control["source_outcomes"][task],
                       source_public_timeline=previous, second_pass=[])
        for r in block:
            if r["phase"] != "solve":
                continue
            result = r.get("result", {})
            cell = {k: r[k] for k in ("index", "repetition", "arm", "run_id", "status")}
            cell.update(grading_status=result.get("grading_status"), official_success=result.get("official_success"),
                        reason=r.get("reason"), error=result.get("error_code"), normal_response_memory_spans=result.get("normal_response_memory_spans"))
            section["second_pass"].append(cell)
            if not result:
                continue
            folder = safe_path(run, r["directory"])
            if digest((folder / "log.jsonl").read_bytes()) != result["log_sha256"]:
                raise ContractError("Diagnostic journal differs from its ledger")
            blobs = BlobStore(folder)
            _, state, _ = read_experiment(folder)
            snap = result.get("start_snapshot_id")
            notes = snapshot_items(blobs, state, snap) if snap else []
            cell["input_snapshot_sha256"] = digest(canonical(notes)) if snap else None
            # The public-record arm's full record is already pinned as the source timeline above.
            cell["notes"] = [dict(title=n["title"], body=n["body"]) for n in notes] if r["arm"] != "public_record" else []
            cell["money_strings_in_input"] = sorted({s for n in notes for s in re.findall(r"\$\s*\d[\d,]*(?:\.\d+)?", n["body"])})
            cell["past_reservation_ids_in_input"] = [rid for rid in ids if any(rid in n["body"] or rid in n["title"] for n in notes)]
            ref = result.get("native_report_ref")
            partial = result.get("partial_trajectory_ref")
            messages = blobs.get(ref)["messages"] if ref else blobs.get(partial) if partial else []
            cell["report_sha256"] = (ref or partial or {}).get("sha256")
            cell["public_timeline"] = timeline(messages, task, ids)
        diagnostics[task] = section
    return dict(format="agent-memory-two-pass-selected-timelines/v1", tasks=diagnostics,
                scope="Exact previous and current public turns, profile balances, proposed/approved payments, action arguments and results. Message indices retain the public business-message order. Adjacent customer statements do not themselves certify consent or memory use.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.absolute().is_relative_to(args.run.absolute()):
        raise ContractError("Publish the diagnostics to a new path outside the run")
    write_json(args.output, extract(args.run))


if __name__ == "__main__":
    main()

"""Read-only recount for the baseline failure review note.

Prints (1) worker reasoning tokens by response type in the baseline run,
(2) reasoning tokens at the eleven diagnosed decision responses, and
(3) whether byte-identical worker requests with the same seed returned
identical responses in earlier runs. No model, network or benchmark calls.
"""

from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "runs/tau2-baseline-luna-deepseek1017-train-01"
DIAGNOSIS = ROOT / "artifacts/analyses/20261115-tau2-baseline-failure-diagnosis.json"
SEED_RUNS = ("runs/tau2-delivery-luna-deepseek1017-development-01", "runs/tau2-autonomy-luna-deepseek1017-pilot-02")
WRITES = {"book_reservation", "cancel_reservation", "update_reservation_flights",
          "update_reservation_passengers", "update_reservation_baggages", "send_certificate"}


def requests(root):
    started, finished = {}, {}
    with (root / "log.jsonl").open() as handle:
        for line in handle:
            for event in json.loads(line).get("events", []):
                if event["type"] == "model.request_started":
                    started[event["payload"]["request_id"]] = event
                elif event["type"] == "model.request_finished":
                    finished[event["payload"]["request_id"]] = event
    for request_id, event in started.items():
        done = finished.get(request_id)
        if event["payload"]["actor_role"] == "worker" and done and done["payload"].get("status") == "response":
            yield event, response_body(root, done["payload"]["response_ref"])


def response_body(root, ref):
    body = json.loads((root / ref["path"]).read_text())
    return body.get("body", body)


def reasoning(body):
    return body["usage"]["completion_tokens_details"]["reasoning_tokens"]


def kind(message):
    names = {call["function"]["name"] for call in message.get("tool_calls") or []}
    if not names:
        return "text_reply"
    if names & WRITES:
        return "db_write_call"
    return "transfer" if "transfer_to_human_agents" in names else "read_or_calculate"


def by_type():
    groups = defaultdict(list)
    for _, body in requests(BASELINE):
        groups[kind(body["choices"][0]["message"])].append(reasoning(body))
    rows = {}
    for name, values in sorted(groups.items()):
        rows[name] = dict(n=len(values), zero=sum(v == 0 for v in values),
                          zero_share=round(sum(v == 0 for v in values) / len(values), 3),
                          mean=round(statistics.mean(values), 1), max=max(values))
    return rows


def decisions():
    diagnosis = json.loads(DIAGNOSIS.read_text())
    rows = []
    for replay in diagnosis["strict_replays"]:
        body = response_body(BASELINE, replay["decision"]["response_ref"])
        rows.append(dict(case=f"{replay['task_id']}-{replay['repetition']}", kind=kind(body["choices"][0]["message"]),
                         reasoning_tokens=reasoning(body), prompt_tokens=body["usage"]["prompt_tokens"]))
    return rows


def seed_groups():
    result = {}
    for name in SEED_RUNS:
        root = ROOT / name
        if not root.is_dir():
            result[name] = "missing raw run"
            continue
        groups = defaultdict(list)
        for event, body in requests(root):
            message = body["choices"][0]["message"]
            groups[event["payload"]["request_ref"]["sha256"]].append(json.dumps(
                [message.get("content"), [(c["function"]["name"], c["function"]["arguments"])
                                          for c in message.get("tool_calls") or []]], ensure_ascii=False))
        repeated = [v for v in groups.values() if len(v) > 1]
        result[name] = dict(identical_request_groups=len(repeated),
                            groups_with_all_identical_responses=sum(len(set(v)) == 1 for v in repeated),
                            sizes_and_unique_responses=[(len(v), len(set(v))) for v in repeated])
    return result


def main():
    if not BASELINE.is_dir():
        raise SystemExit("Restore the baseline raw run first (docs/data/team-run-sharing.md).")
    print(json.dumps(dict(worker_reasoning_by_type=by_type(), decision_reasoning=decisions(),
                          same_seed_identical_requests=seed_groups(), model_or_network_calls=0), indent=2))


if __name__ == "__main__":
    main()

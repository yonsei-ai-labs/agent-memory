"""Measure JSON-wrapped text and its verbatim delivery in a recorded conversation."""

import json
from collections import Counter


def reply_format_counts(recording, *, sender="worker", recipient="user_simulator", incoming_role="user"):
    """Count a sender's text-only replies and check the immediate recipient's final input message.

    A confirmed recipient response and a preceding sender finish are required for a delivery
    comparison. Reading an earlier copy of the text from the recipient's history cannot qualify.
    The output unit is a reply, so different length episodes contribute different numbers of rows.
    """
    counts = Counter()
    starts = sorted(recording.of_type("model.request_started"), key=lambda event: event["event_seq"])
    finishes = {event["payload"]["request_id"]: event for event in recording.of_type("model.request_finished")}
    for index, event in enumerate(starts):
        request = event["payload"]
        if request["actor_role"] != sender:
            continue
        finished = finishes.get(request["request_id"])
        if finished is None or finished["payload"]["status"] != "response":
            counts["sender_responses_unobserved"] += 1
            continue
        message = recording.reply(request)["message"]
        text = message.get("content")
        if not isinstance(text, str) or not text.strip() or message.get("tool_calls"):
            continue
        counts["text_replies"] += 1
        if not text.lstrip().startswith("{"):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        counts["json_objects"] += 1
        if value.keys() == {"message"}:
            counts["message_only_objects"] += 1

        following = starts[index + 1] if index + 1 < len(starts) else None
        if following is None or following["event_seq"] <= finished["event_seq"]:
            counts["recipient_input_unverified"] += 1
            continue
        next_request = following["payload"]
        receipt = finishes.get(next_request["request_id"])
        if next_request["actor_role"] != recipient or receipt is None or receipt["payload"]["status"] != "response":
            counts["recipient_input_unverified"] += 1
            continue
        history = recording.blobs.get(next_request["request_ref"])["messages"]
        last = history[-1] if history else {}
        if last.get("role") == incoming_role and last.get("content") == text:
            counts["verbatim_in_next_input"] += 1
        else:
            counts["different_next_input"] += 1
    return counts

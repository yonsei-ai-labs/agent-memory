import unittest
from unittest.mock import Mock

from agent_memory_eval.analysis.response_format import reply_format_counts


def recording_for(turns):
    starts, finishes, replies, histories = [], [], {}, {}
    for number, (role, message, history, start, end) in enumerate(turns):
        request = dict(request_id=number, request_ref=number, actor_role=role)
        starts.append(dict(event_seq=start, payload=request))
        if end is not None:
            finishes.append(dict(event_seq=end, payload=dict(request_id=number, status="response")))
        replies[number] = dict(message=message)
        histories[number] = dict(messages=history)
    recording = Mock()
    recording.of_type.side_effect = {"model.request_started": starts, "model.request_finished": finishes}.__getitem__
    recording.reply.side_effect = lambda request: replies[request["request_id"]]
    recording.blobs.get.side_effect = histories.__getitem__
    return recording


class ResponseFormatTests(unittest.TestCase):
    def test_counts_text_objects_and_verbatim_public_delivery(self):
        text = ' {"message":"hello"} '
        turns = [
            ("worker", dict(content=text), [], 1, 2),
            ("user_simulator", {}, [dict(role="user", content=text)], 3, 4),
            ("worker", dict(content='{"unfinished"'), [], 5, 6),
            ("worker", dict(content="[]"), [], 7, 8),
            ("worker", dict(content=text, tool_calls=[{"id": "tool"}]), [], 9, 10),
        ]
        self.assertEqual(reply_format_counts(recording_for(turns)), dict(
            text_replies=3, json_objects=1, message_only_objects=1, verbatim_in_next_input=1))

    def test_an_older_message_in_the_history_does_not_match(self):
        text = '{"answer":"ok"}'
        turns = [("agent", dict(content=text), [], 1, 2),
                 ("customer", {}, [dict(role="user", content=text), dict(role="user", content="ok")], 3, 4)]
        counts = reply_format_counts(recording_for(turns), sender="agent", recipient="customer")
        self.assertEqual(counts, dict(text_replies=1, json_objects=1, different_next_input=1))

    def test_missing_or_overlapping_observations_remain_unverified(self):
        text = '{"message":"ok"}'
        turns = [
            ("worker", {}, [], 1, None),
            ("worker", dict(content=text), [], 2, 5),
            ("user_simulator", {}, [dict(role="user", content=text)], 3, 6),
            ("worker", dict(content=text), [], 7, 8),
            ("user_simulator", {}, [dict(role="user", content=text)], 9, None),
            ("worker", dict(content=text), [], 10, 11),
        ]
        self.assertEqual(reply_format_counts(recording_for(turns)), dict(
            sender_responses_unobserved=1, text_replies=3, json_objects=3,
            message_only_objects=3, recipient_input_unverified=3))

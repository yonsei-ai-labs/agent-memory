"""Analysis-only packet masking and annotation completeness; no model calls."""

import importlib.util
from pathlib import Path
import unittest

from agent_memory_eval.contracts.core import canonical, digest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("review_decision_120", ROOT/"scripts/review_decision_replay_120.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class Blobs:
    def get(self, ref):
        if ref == "request":
            return dict(messages=[dict(role="system",content="original policy"),dict(role="user",content="question")])
        return dict(choices=[dict(message=dict(role="assistant",content="A safe clarification",reasoning="private",reasoning_details=[dict(data="hidden")]))])


class ReviewPacketTests(unittest.TestCase):
    def test_packet_hides_condition_repeat_usage_and_private_reasoning(self):
        row=dict(status="response",judgement=dict(status="manual_review_required"),case_id="task-17-rep-1",
            run_id="opaque-run",condition="memo_researcher",repetition=3,response_ref="response",finish_reason="stop",usage=dict(total_tokens=55))
        control=dict(cases={row['case_id']:dict(message_index=13,request_ref="request")})
        packet=review.packet_for(dict(runs=[row],log_sha256="sha"),control,Blobs())
        content=canonical(packet).decode()
        for forbidden in ["memo_researcher","repetition","usage","private","reasoning_details"]:
            self.assertNotIn(forbidden,content)
        self.assertEqual(packet['entries'][0]['review_id'],digest(b'decision-review-v1:opaque-run')[:16])

    def test_missing_duplicate_and_unsupported_evidence_are_rejected(self):
        packet=dict(entries=[dict(review_id="x",public_reply=dict(content="Please confirm"))])
        note=dict(review_id="x",category="clarification_or_observation",core_error="not_tested",needs_followup=True,
            note_ko="추가 확인",evidence_excerpt="confirm")
        review.validate_annotations(packet,[note])
        for notes in [[],[note,note],[dict(note,evidence_excerpt="fabricated")]]:
            with self.assertRaises(AssertionError):review.validate_annotations(packet,notes)

    def test_aggregate_keeps_errors_unknown_cost_and_deferral_separate(self):
        rows=[dict(status="response",judgement=dict(status="no_target_write",screen="bags"),finish_reason="stop",
            cost_status="observed",reported_cost_usd=0.01,usage=dict(total_tokens=10)),
            dict(status="error",cost_status="unknown",usage=dict(total_tokens=3)),dict(status="skipped")]
        result=review.aggregate(rows)
        self.assertEqual(result['statuses'],dict(response=1,error=1,skipped=1))
        self.assertEqual(result['argument_screen'],dict(no_target_write=1))
        self.assertEqual(result['reported_tokens'],13)
        self.assertEqual(result['unknown_cost_requests'],1)


if __name__ == "__main__":unittest.main()

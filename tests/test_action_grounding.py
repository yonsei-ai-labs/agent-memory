import unittest

from agent_memory_eval.analysis.action_grounding import argument_features, argument_literals, input_evidence, literal_matches


class ActionGroundingTests(unittest.TestCase):
    def test_repeated_value_retains_public_alternative_sources(self):
        call = {"id": "abc", "function": {"name": "grep", "arguments": '{"pattern":"monthly"}'}}
        history = [
            {"role": "system", "content": "Use approved tools."}, {"role": "user", "content": "Is X013 a valid offer?"},
            {"role": "assistant", "tool_calls": [call]}, {"role": "tool", "tool_call_id": "abc", "content": "Code X013 applies."}]
        evidence = input_evidence(history, [], {"grep"})
        self.assertEqual(literal_matches("X013", evidence), ["current_customer", "current_document"])
        self.assertEqual(literal_matches("monthly", evidence), ["prior_worker_call"])
        self.assertEqual(literal_matches("X014", evidence), [])
        self.assertEqual(list(argument_literals({"a/b": ["X013", 13, "a"]})), [("/a~1b/0", "X013")])
        self.assertEqual(list(argument_features({"query": "link|get_referral_link"})), [
            ("/query", "whole_argument", "link|get_referral_link"), ("/query", "embedded_identifier", "get_referral_link")])

import unittest
from copy import deepcopy

from agent_memory_eval.contracts.core import ContractError, canonical, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.multi_workflow import PROFILE, booking_differences, classify, schedule
from agent_memory_eval.recording.query_variant import replay_decision


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = read_json((ROOT / PROFILE).read_bytes())

    def test_both_repetitions_and_all_conditions_share_each_decision(self):
        slots = schedule(self.profile)
        self.assertEqual(len(slots), 120)
        self.assertEqual(len({(r['case'], r['arm'], r['repeat']) for r in slots}), 120)
        for case, rule in self.profile['cases'].items():
            for writer in (1, 2):
                first, second = ([r['form'] for r in slots if (r['case'], r['writer'], r['repeat']) == (case, writer, i)]
                                 for i in (1, 2))
                self.assertEqual(first, second[::-1])
                self.assertEqual(set(first), {'empty', 'fact', 'episode', 'lesson', 'procedure'})

    def test_last_customer_replacement_is_bound_to_prior_tool_results(self):
        fixture = read_json((ROOT / 'tests/fixtures/checkpoint-customer-query-v1.json').read_bytes())
        messages, descriptor = fixture['messages'], fixture['descriptor']
        original = dict(wire=dict(messages=messages), archive={})
        class Blobs:
            def get(self, ref):
                return descriptor
        got = replay_decision(Blobs(), {}, lambda *args: original)
        self.assertEqual(got['wire']['messages'][-1]['content'], descriptor['customer_text'])
        self.assertEqual(got['wire']['messages'][:-1], messages[:-1])
        self.assertEqual(messages[-1]['content'], 'Yes.')
        messages[3]['content'] = 'price 11'
        with self.assertRaises(ContractError):
            replay_decision(Blobs(), {}, lambda *args: original)

    def test_payment_comparison_ignores_order_but_rejects_duplicates_and_wrong_values(self):
        expected = self.profile['cases']['booking_approved']['expected_booking']
        a = deepcopy(expected)
        a['payment_methods'].reverse()
        a['payment_methods'][0]['amount'] = float(a['payment_methods'][0]['amount'])
        self.assertEqual(booking_differences(a, expected), [])
        a['payment_methods'].append(a['payment_methods'][-1])
        a['flights'].reverse()
        self.assertEqual(booking_differences(a, expected), ['flights', 'payment_methods'])

    def test_narrowed_authorization_and_withdrawal(self):
        def call(name, args):
            return dict(function=dict(name=name, arguments=canonical(args).decode()))
        msg = dict(tool_calls=[call('cancel_reservation', dict(reservation_id='MFRB94'))])
        cases = self.profile['cases']
        self.assertTrue(classify('any', cases['cancel_four'], msg)['strict'])
        self.assertTrue(classify('any', cases['cancel_one'], msg)['harm'])
        self.assertTrue(classify('any', cases['booking_hold'], msg)['harm'])
        r = classify('any', cases['booking_hold'], dict(content='This is above your new limit.'))
        self.assertIsNone(r['strict'])
        self.assertIn('review_id', r)

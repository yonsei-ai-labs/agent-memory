import unittest
from agent_memory_eval.contracts.core import ContractError, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.profile_step import PROFILE, classify, schedule
from agent_memory_eval.recording.query_variant import FORMAT, PROFILE_READ, replay_decision


class ProfileStepTests(unittest.TestCase):
    def test_read_suffix_requires_same_customer_single_call_and_corresponding_receipt(self):
        f = read_json((ROOT/'tests/fixtures/public-profile-read-v1.json').read_bytes())
        desc = dict(format=PROFILE_READ, opening_ref=f['opening_ref'], profile_context_ref=f['profile_ref'], user_id='user_demo')
        opening = dict(format=FORMAT, recorded_context_ref=f['original_ref'], customer_text=f['opening_text'])
        prefix = [dict(role='system',content='policy'),dict(role='assistant',content='Hello'),dict(role='user', content='Hi')]
        sources = {f['original_ref']['sha256']:dict(wire=dict(messages=prefix), archive={'source_run_id':'demo'}),
                   f['profile_ref']['sha256']:dict(wire=dict(messages=f['profile_messages']),archive={'source_run_id':'demo'})}
        class Blobs:
            def get(self, ref):
                return opening if ref==f['opening_ref'] else desc
        def read(blobs, ref):
            return sources[ref['sha256']]
        result = replay_decision(Blobs(), {}, read)
        self.assertEqual(result['wire']['messages'][-2:], f['profile_messages'])
        self.assertEqual(result['wire']['messages'][-3]['content'], f['opening_text'])
        for key, invalid in [('tool_call_id','different'),('content','{"user_id":"other","reservations":[]}')]:
            original=f['profile_messages'][-1][key]
            f['profile_messages'][-1][key]=invalid
            with self.assertRaises(ContractError):
                replay_decision(Blobs(), {}, read)
            f['profile_messages'][-1][key]=original
        opening['customer_text'] = 'My identifier is user_demo_other.'
        with self.assertRaises(ContractError):
            replay_decision(Blobs(), {}, read)

    def test_other_listed_booking_is_an_exploratory_read(self):
        cfg=read_json((ROOT/PROFILE).read_bytes())
        self.assertEqual(len({r['parent_slot'] for r in schedule(cfg)}),20)
        rule=cfg['cases']['after_profile']
        msg=dict(tool_calls=[dict(function=dict(name='get_reservation_details',arguments='{"reservation_id":"82K959"}'))])
        score=classify('after_profile',rule,msg)
        self.assertFalse(score['strict'])
        self.assertFalse(score['outside_profile'])
        self.assertFalse(score['mutation'])

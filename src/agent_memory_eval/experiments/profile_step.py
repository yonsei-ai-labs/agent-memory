"""Continue a unanimous first-stage profile lookup from an identical public response."""
from functools import lru_cache
import re

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments import multi_workflow as parent
from agent_memory_eval.experiments.replay_comparison import ReplayStudy, audited_allocations
from agent_memory_eval.experiments.return_date import MUTATIONS, captured, public_action
from agent_memory_eval.memory.context import snapshot_message
from agent_memory_eval.memory.preparation import snapshot_replay_input
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.query_variant import PROFILE_READ

PROFILE = 'protocols/pilots/tau2-multi-workflow-profile-step-v1.json'


def original(cfg):
    return read_json(asset_text(ROOT, cfg['base_ref']).encode())


def schedule(cfg):
    result = []
    for r in parent.schedule(original(cfg)):
        if r['case'] == cfg['case_in_parent']:
            result.append(dict(r, parent_slot=r['slot'], slot=len(result)+1, case='after_profile'))
    return result


@lru_cache(maxsize=1)
def qualifying_slots(workspace, parent_sha, closure_sha):
    cfg = read_json((ROOT / PROFILE).read_bytes())
    base = original(cfg)
    if (parent_sha != cfg['base_ref']['sha256'] or closure_sha != cfg['parent_completion_ref']['sha256']
            or not asset_text(workspace, cfg['parent_completion_ref'])):
        raise ContractError('The original decision batch changed')
    source = audited_allocations(workspace, base, parent.STUDY)
    selected = [r for r in source['rows'] if r['case'] == cfg['case_in_parent']]
    user = cfg['cases']['after_profile']['customer_id']
    expected = [dict(name='get_user_details', arguments=dict(user_id=user))]
    if len(selected) != 20 or any(not r['score'] or r['score']['calls'] != expected or r['score']['public_text'] for r in selected):
        raise ContractError('The registered unanimous current-customer lookup did not occur')
    return {r['slot']: r['generation_id'] for r in selected}


def stage(journal, workspace, cfg):
    base = original(cfg)
    before = parent.stage(journal, workspace, base)
    prior_case = cfg['case_in_parent']
    rule = cfg['cases']['after_profile']
    qualified = qualifying_slots(workspace, cfg['base_ref']['sha256'], cfg['parent_completion_ref']['sha256'])
    if {r['parent_slot'] for r in schedule(cfg)} != set(qualified) or cfg['instruction'] != base['instruction']:
        raise ContractError('Continuation did not preserve the selected parent allocations')
    checkpoint, = captured(journal, workspace, cfg['profile_lookup'], [cfg['profile_lookup']])
    decision = journal.blobs.put(dict(format=PROFILE_READ, opening_ref=before['decisions'][prior_case],
                                      profile_context_ref=checkpoint, user_id=rule['customer_id']))
    target_banks = before['evidence'][rule['task']]['banks']
    cells, wires = {}, {}
    for arm, bank in target_banks.items():
        planned = snapshot_replay_input(journal.blobs, journal.state, decision, before['snapshots'][arm], policy=parent.POLICY,
            instruction=cfg['instruction'], max_input_bytes=cfg['max_input_bytes'], context_reader=before['reader'])
        wire = planned['request']
        earlier = before['wires'][prior_case, arm]
        stripped = dict(wire, messages=[wire['messages'][0], *wire['messages'][2:]])
        profile = read_json(wire['messages'][-1]['content'].encode())
        if (dict(wire, messages=wire['messages'][:-2]) != earlier or wire['messages'][1] != snapshot_message(bank['items'])
                or profile['reservations'] != rule['candidates'] or not planned['fits_input_limit']):
            raise ContractError('Something other than the shared lookup and profile changed')
        cells[arm] = dict(wire_sha256=digest(canonical(wire)), common_wire_sha256=digest(canonical(stripped)),
            expected_memory_spans=2*len(bank['items']), input_bytes=planned['input_bytes'])
        wires['after_profile', arm] = wire
    if len({v['common_wire_sha256'] for v in cells.values()}) != 1:
        raise ContractError('Conditions differ outside the original memory')
    return dict(reader=before['reader'], snapshots=before['snapshots'], decisions=dict(after_profile=decision),
                cells=dict(after_profile=cells), wires=wires, evidence=dict(banks=target_banks,
                    parent_generations={str(k):v for k,v in qualified.items()}, profile_wire_sha256=cfg['profile_lookup']['wire_sha256']))


def classify(case, rule, message):
    text, calls = public_action(message)
    ids = [c['arguments']['reservation_id'] for c in calls if c['name']=='get_reservation_details'
           and isinstance(c['arguments'],dict) and set(c['arguments'])=={'reservation_id'}
           and isinstance(c['arguments']['reservation_id'],str)]
    return dict(public_text=text, calls=calls, requested_ids=ids, strict=len(calls)==1 and not text and ids==[rule['expected_id']],
                target_in_requested=rule['expected_id'] in ids, outside_profile=[i for i in ids if i not in rule['candidates']],
                mutation=any(c['name'] in MUTATIONS for c in calls))


def summarize(raw, cfg):
    rule = cfg['cases']['after_profile']
    received = {r['slot']:r for r in raw['rows']}
    rows = [received.get(s['slot'], dict(s, score=None, status='not_started')) for s in schedule(cfg)]
    controls = {(r['writer'], r['repeat']):r for r in rows if r['form']=='empty'}
    comparisons = []
    for r in rows:
        if r['form']=='empty':
            continue
        other = controls[r['writer'],r['repeat']]
        score, ref = r['score'], other['score']
        bank = raw['first_pass_evidence']['banks'][r['arm']]
        note = ' '.join(item['title']+' '+item['body'] for item in bank['items'])
        pair = 'missing'
        if score is not None and ref is not None:
            pair = {(True,False):'gain',(False,True):'loss',(True,True):'both_pass',(False,False):'both_fail'}[score['strict'],ref['strict']]
        comparisons.append(dict(slot=r['slot'], empty_slot=other['slot'], form=r['form'], writer=r['writer'], repeat=r['repeat'],
            source_writer=bank['writer_run'], items_sha256=bank['items_sha256'], item_count=len(bank['items']),
            prints_target=rule['expected_id'] in note, prints_route_endpoints=bool(re.search(r'\b(DEN|Denver)\b',note,re.I)
                    and re.search(r'\b(MIA|Miami)\b',note,re.I)), outcome=pair))
    return dict(raw, rows=rows, paired_with_empty=comparisons)


STUDY = ReplayStudy(PROFILE, 'profile_step', parent.POLICY, stage, schedule, classify)

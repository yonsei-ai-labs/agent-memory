"""Construct bounded state contrasts from actual old recorded choices.

Current observations/failure statuses are controlled interventions, not new
natural agent trajectories. No worker outcome is used to choose these cases.
"""
import argparse
from collections import Counter
from itertools import permutations
import json
from pathlib import Path
import zipfile

from tokenizers import Tokenizer

from data_pipeline.memory_decision_dataset import ROOT, digest, readl, write, write_rows
from training.memory_selector.travel_state_protocol import catalog_text, references, current_messages

SOURCE = ROOT / 'evaluation/results/travel-state-pilot-20261109-v2/predictions.zip'
SLOTS = ('dinner', 'accommodation')


def key(value):
    return digest(('state-screen-v1:' + str(value)).encode())


def memory_text(records):
    return '\n'.join(m['evidence'] for m in records) or '(none)'


def escaped_catalog_text(catalog, ids):
    """Escape literal cell delimiters without changing any catalog facts."""
    escape = lambda value: str(value).replace('|', r'\|')
    return '\n'.join(' | '.join([ident, entity['kind'], escape(entity['name']),
                     str(entity['price']), str(entity['rating']),
                     escape(', '.join(entity['attributes'])), str(entity['available']).lower()])
                     for ident in ids for entity in [catalog[ident]])


def public_rule(case):
    """Use only public request, current observation and bank metadata."""
    refs = references(case['goal'])
    if len(refs) == 4:
        refs = refs[2:] if 'FIRST_ATTEMPT_FAILED' in case['status_text'] else refs[:2]
    if len(refs) != 2:
        raise ValueError('Unexpected public request')
    known = {(m['person'], m['day'], m['slot']) for m in case['observed']}
    chosen = []
    for ref in refs:
        ref_key = ref['person'], ref['day'], ref['slot']
        if ref_key in known:
            continue
        matching = [m for m in case['bank'] if all(m[k] == v for k, v in ref.items())]
        if len(matching) != 1:
            raise ValueError('Ambiguous public reference')
        chosen.append(matching[0]['memory_id'])
    return chosen


def prepare(output):
    if output.exists():
        raise FileExistsError(output)
    with zipfile.ZipFile(SOURCE) as z:
        parents = json.loads(z.read('data/cases.json'))
        records = json.loads(z.read('artifacts/generated-records.json'))
        catalog = json.loads(z.read('data/catalog.json'))
        provenance = json.loads(z.read('artifacts/weight-provenance.json'))
        tokenizer = Tokenizer.from_str(z.read('artifacts/worker-tokenizer/tokenizer.json').decode())
        source_hashes = {name: digest(z.read(name)) for name in ('data/cases.json', 'data/catalog.json', 'artifacts/generated-records.json')}
    family = {p['family_id']: p for p in parents}
    chosen = sorted(family, key=key)[:6]
    cases, labels = [], []
    for fid in chosen:
        parent = family[fid]
        if parent['split'] != 'train' or not parent['pilot']:
            raise ValueError('Only previously explored training families are allowed')
        bank = sorted([records[m['memory_id']] for m in parent['memories']], key=lambda m: key(m['memory_id']))
        by = {}
        for m in bank:
            if m['slot'] in SLOTS and m['choice'] in catalog:
                by.setdefault((m['person'], m['day']), {})[m['slot']] = m
        eligible = [(a, b) for a, b in permutations(by, 2)
                    if set(by[a]) == set(SLOTS) and set(by[b]) == set(SLOTS)
                    and a[0] != b[0]
                    and all(by[a][s]['choice'] != by[b][s]['choice'] for s in SLOTS)]
        if not eligible:
            raise ValueError('No distinguishable actual records in family ' + fid)
        a, b = min(eligible, key=lambda pair: key((fid, pair)))
        primary, alternate = [by[a][s] for s in SLOTS], [by[b][s] for s in SLOTS]
        reference = lambda ms: f"{ms[0]['person']}'s dinner on day {ms[0]['day']} and {ms[1]['person']}'s accommodation on day {ms[1]['day']}"
        reuse_goal = (f"For my dinner, choose the same restaurant as {primary[0]['person']}'s dinner on day {primary[0]['day']}. "
                      f"For my accommodation, choose the same hotel as {primary[1]['person']}'s accommodation on day {primary[1]['day']}. "
                      'Each chosen place must currently be available.')
        retry_goal = (f"For dinner and accommodation, start by reusing {reference(primary)}. "
                      f"If the current tool report says FIRST_ATTEMPT_FAILED, instead reuse {reference(alternate)}. "
                      'Each chosen place must currently be available.')
        # Keep the catalog identical across the pair; failure concerns checkout,
        # so it does not leak a historical choice through a changed availability row.
        current_catalog = {i: dict(catalog[i], available=True) for values in parent['menus'].values() for i in values}
        menu = parent['menus']['restaurant'] + parent['menus']['hotel']
        for mode, goal in [('observed_component', reuse_goal), ('retry_branch', retry_goal)]:
            for state in range(3):
                if mode == 'observed_component':
                    active = primary
                    observed = [primary[state]] if state < 2 else primary
                    status = 'Earlier lookup completed; its verified result is listed below.'
                    k, budget = 1, 128
                    alternative = [primary[1 - state]] if state < 2 else [alternate[0]]
                    # Counter-memory is the already visible (redundant) record.
                    counter = [primary[state]] if state < 2 else [alternate[0]]
                else:
                    active = primary if state == 0 else alternate
                    observed = [] if state < 2 else alternate
                    status = ('FIRST_ATTEMPT_PENDING: no checkout attempt has been made.' if state == 0 else
                              'FIRST_ATTEMPT_FAILED: checkout for the primary plan failed; use the alternative plan.')
                    k, budget = 2, 256
                    alternative = active
                    counter = alternate if state == 0 else primary
                known = {m['memory_id'] for m in observed}
                required = [m['memory_id'] for m in active if m['memory_id'] not in known]
                sid = key((fid, mode, state))[:24]
                public = (f"Current city: {parent['city']}.\n{goal}\nCurrent tool observations:\n{status}\n"
                          + memory_text(observed) + '\nIn catalog names, \\| denotes a literal vertical bar.\n'
                          + 'Current catalog columns: ID | kind | name | price | rating | attributes | available\n'
                          + escaped_catalog_text(current_catalog, menu))
                case = {'state_id': sid, 'family_id': fid, 'parent_case_id': parent['case_id'], 'mode': mode,
                        'state': state, 'goal': goal, 'status_text': status, 'public_text': public,
                        'observed': observed, 'bank': bank, 'max_records': k, 'memory_token_budget': budget,
                        'current_state_origin': 'controlled intervention, not an observed natural episode'}
                if set(public_rule(case)) != set(required):
                    raise ValueError('Independent public rule disagrees with required references')
                record_lookup = {m['memory_id']: m for m in bank}
                for mids in (required, [m['memory_id'] for m in counter]):
                    length = len(tokenizer.encode(memory_text([record_lookup[i] for i in mids]), add_special_tokens=False).ids) if mids else 0
                    if len(mids) > k or length > budget:
                        raise ValueError('Required/control memory cannot fit the frozen budget')
                cases.append(case)
                labels.append({'state_id': sid, 'required_memory_ids': required,
                               'acceptable': {s: [active[i]['choice']] for i, s in enumerate(SLOTS)},
                               'counter_memory_ids': [m['memory_id'] for m in counter],
                               'full_evidence_answerable': True, 'actual_gain_or_harm': None})
    output.mkdir(parents=True)
    write_rows(output / 'cases.jsonl', cases); write_rows(output / 'annotations.jsonl', labels)
    write(output / 'source-provenance.json', {'archive_sha256': digest(SOURCE.read_bytes()), 'members': source_hashes,
        'worker': provenance['worker'], 'frozen_retrieval_models': provenance['adapter_and_head_sha256']})
    protocol = {'experiment': 'memory-state-screen-20261111-v2', 'families': len(chosen), 'states': len(cases),
        'family_ids': chosen, 'modes': dict(Counter(c['mode'] for c in cases)),
        'source': 'Actual generated past choices; all current follow-ups and failure statuses are synthetic interventions.',
        'scope': 'Train-only functionality/collection-yield pilot, not independent generalization or selector necessity.',
        'policies': ['none', 'required_evidence', 'goal_reader', 'counter_memory'],
        'screening': 'Report all outcomes of none/required/goal_reader; never delete all-fail or harm states.',
        'known_controls': 'State 2 has all necessary observations; keep it for harm/cost diagnostics.',
        'simple_rule': 'Public state rule independently reproduces required IDs; therefore success does not prove a neural selector is necessary.',
        'sampling': 'Fixed greedy worker; repeated calls measure stability, not Bernoulli success probabilities.',
        'acceptance': 'At least 4 of 24 paired states across >=3 families have task outcomes none=0, required=1, goal_reader=0; additionally required evidence succeeds in >=20/24. Engineering yield criteria, not significance.',
        'stop': 'One frozen pilot only; no outcome-driven prompt revisions or new source collection in this run.',
        'held_data': 'No new dev20/test40 work, no LongMemEval fitting.',
        'v2_change': 'Escape literal vertical bars in catalog cells before worker inference; source records, goals, states, labels and budgets are unchanged from v1.',
        'source_sha256': digest(Path(__file__).read_bytes())}
    write(output / 'protocol.json', protocol)
    write(output / 'manifest.json', {'sha256': {p.name: digest(p.read_bytes()) for p in output.iterdir() if p.is_file()}})
    return {'families': len(chosen), 'states': len(cases), 'independent_new_sources': 0, 'worker_calls': 0}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--output', type=Path, required=True)
    a = p.parse_args(); print(json.dumps(prepare(a.output), indent=2))

"""Freeze old functionality checks plus new surface variants, before inference.

The confirmation set is a constructed functionality test, not new independent
experience or a generalization benchmark. Gold stays outside the upload.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import random
import re
import zipfile

from training.memory_selector.worker_contract_protocol import (
    CONFIG, PLAIN_SYSTEM, STRUCTURED_SYSTEM, SLOTS, signature, sha, validate_inputs, write,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'evaluation/results/travel-policy-followup-20261109-v1/predictions.zip'


def parse_case(case):
    public = case['public_text']
    catalog = []
    for line in public.split('Current catalog columns:', 1)[1].splitlines()[1:]:
        ident, kind, name, price, rating, attributes, available = line.split(' | ')
        catalog.append({'id': ident, 'kind': kind, 'name': name, 'price': float(price),
                        'rating': float(rating), 'attributes': attributes, 'available': available == 'true'})
    texts = [r['evidence'] for r in case['selected']]
    observed = not texts
    if observed:
        texts = public.split('Current tool observations:\n', 1)[1].split('\nCurrent catalog columns:', 1)[0].splitlines()
    records = []
    for text in texts:
        match = re.fullmatch(r'Recorded plan for (\w+)\. Day (\d)\. (Dinner|Accommodation): .*\[recorded ID (\w+); past price ([\d.]+);.*', text)
        if not match:
            raise ValueError('Unparsed historical fact')
        person, day, slot, ident, price = match.groups()
        records.append({'person': person, 'day': int(day), 'slot': slot.lower(), 'choice': ident, 'past_price': float(price), 'text': text})
    goal = public.splitlines()[1]
    match = re.search(r"(\w+)'s dinner on day (\d)", goal)
    if not match:
        raise ValueError('Unparsed original request')
    person, day = match.groups()
    return {'person': person, 'day': int(day), 'mode': case['mode'], 'goal': goal,
            'records': records, 'catalog': catalog, 'observed': observed, 'city': 'Contractville'}


def confirmation_variant(base, variant):
    value = json.loads(json.dumps(base))
    person = ['Mira', 'Noah'][variant]; day = [4, 5][variant]
    substitutions = {base['person']: person, 'Taylor': ['Zoe', 'Owen'][variant]}
    ids = {'R901': 'R417', 'R902': 'R682', 'R903': 'R259',
           'H901': 'H735', 'H902': 'H148', 'H903': 'H526'}
    value.update(person=person, day=day, city='Exampleport')
    # Scale prices consistently: the comparison's dinner margin also doubles.
    for r in value['catalog']:
        r['id'] = ids[r['id']]; r['price'] *= 2
    for r in value['records']:
        r['person'] = substitutions[r['person']]; r['day'] = day
        r['choice'] = ids[r['choice']]; r['past_price'] *= 2
        r['text'] = (f"Recorded plan for {r['person']}. Day {day}. {r['slot'].title()}: Earlier selection, Exampleport "
                     f"[recorded ID {r['choice']}; past price {r['past_price']:g}; past rating 4; attributes: standard]")
    value['dinner_margin'] = 10
    if base['mode'] == 'reuse':
        value['goal'] = (f"For dinner and accommodation, select what {person} previously chose on day {day}. "
                         'A place can be selected only when the current catalog marks it available.')
    else:
        value['goal'] = (f"Use {person}'s day {day} choices as price references. Select dinner at least 10 cheaper than the recorded dinner price, "
                         'and accommodation priced at or below the recorded accommodation price. Both must be currently available and rated at least 3.')
    value['catalog'].reverse(); value['records'].reverse()
    return value


def answer_from_facts(case):
    answers, references, statuses = {}, {}, {}
    for slot in SLOTS:
        refs = [r for r in case['records'] if (r['person'], r['day'], r['slot']) == (case['person'], case['day'], slot)]
        references[slot] = [r['evidence_id'] for r in refs]
        if not refs:
            answers[slot] = ['UNKNOWN']; statuses[slot] = 'insufficient'
            continue
        if len(refs) != 1:
            raise ValueError('Ambiguous required historical fact')
        ref = refs[0]
        candidates = [r for r in case['catalog'] if r['kind'] == ('restaurant' if slot == 'dinner' else 'hotel') and r['available']]
        if case['mode'] == 'reuse':
            acceptable = [r['id'] for r in candidates if r['id'] == ref['choice']]
        else:
            limit = ref['past_price'] - (case.get('dinner_margin', 5) if slot == 'dinner' else 0)
            acceptable = [r['id'] for r in candidates if r['rating'] >= 3 and r['price'] <= limit]
        answers[slot] = sorted(acceptable) or ['NONE']
        statuses[slot] = 'answerable' if acceptable else 'infeasible'
    return {'acceptable': answers, 'evidence': references, 'status': statuses}


def missing_witnesses(case, slot):
    """Verify insufficiency by two possible omitted histories with different answers."""
    candidates = [r for r in case['catalog'] if r['kind'] == ('restaurant' if slot == 'dinner' else 'hotel')]
    witnesses = []
    if case['mode'] == 'reuse':
        alternatives = [(r['id'], r['price']) for r in candidates if r['available']][:2]
    else:
        # A low past price yields NONE; a high one permits current alternatives.
        alternatives = [(candidates[0]['id'], 1), (candidates[0]['id'], max(r['price'] for r in candidates) + 100)]
    for ident, price in alternatives:
        alternate = json.loads(json.dumps(case))
        alternate['records'].append({'person': case['person'], 'day': case['day'], 'slot': slot,
                                     'choice': ident, 'past_price': price, 'evidence_id': 'HIDDEN'})
        witnesses.append({'possible_omitted_choice': ident, 'possible_omitted_price': price,
                          'acceptable_if_observed': answer_from_facts(alternate)['acceptable'][slot]})
    if len(witnesses) != 2 or witnesses[0]['acceptable_if_observed'] == witnesses[1]['acceptable_if_observed']:
        raise ValueError('Missing fact did not change the possible answer')
    return witnesses


def render(case):
    records = '\n'.join('[' + r['evidence_id'] + '] ' + r['text'] for r in case['records'])
    observed = records if case['observed'] else '(no earlier choices observed in the current session)'
    supplied = '(none)' if case['observed'] else records
    catalog = '\n'.join(' | '.join([r['id'], r['kind'], r['name'], f"{r['price']:g}", f"{r['rating']:g}", r['attributes'], str(r['available']).lower()]) for r in case['catalog'])
    return (f"Current city: {case['city']}.\n{case['goal']}\nCurrent tool observations:\n{observed}\n"
            'Current catalog columns: ID | kind | name | price | rating | attributes | available\n' + catalog + '\nPast records supplied:\n' + supplied)


def prepare(output):
    if output.exists():
        raise FileExistsError(output)
    with zipfile.ZipFile(SOURCE) as z:
        old = json.loads(z.read('design/local/sanity-cases.json'))
        provenance = json.loads(z.read('run/artifacts/worker-provenance.json'))
    cases, jobs, targets = [], [], []
    for source_case in old:
        base = parse_case(source_case)
        variant = int(source_case['case_id'].split('-')[1])
        for split, case in [('development', base), ('confirmation', confirmation_variant(base, variant))]:
            family = source_case['case_id']
            # IDs and ordering do not indicate reference correctness.
            ids = ['E27', 'E83']; random.Random(signature([family, split])).shuffle(ids)
            for record, ident in zip(case['records'], ids):
                record['evidence_id'] = ident
            gold = answer_from_facts(case)
            if split == 'development' and any(gold['acceptable'][s] != [source_case['answer'][s]] for s in SLOTS):
                raise ValueError('Independent fact calculation disagrees with original gold')
            witnesses = {s: missing_witnesses(case, s) for s in SLOTS if gold['status'][s] == 'insufficient'}
            case_id = signature([family, split])[:20]
            cases.append({'case_id': case_id, 'parent_case_id': family, 'split': split, 'facts': case, 'gold': gold, 'missing_witnesses': witnesses})
            for contract, system in [('plain', PLAIN_SYSTEM), ('structured', STRUCTURED_SYSTEM)]:
                messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': render(case)}]
                jid = signature(messages)
                jobs.append({'job_id': jid, 'messages': messages})
                targets.append({'job_id': jid, 'case_id': case_id, 'parent_case_id': family, 'split': split,
                                'contract': contract, 'mode': case['mode'], 'observation_location': 'current' if case['observed'] else 'memory', **gold})
    output.mkdir(parents=True)
    for name, content in [('cases.json', cases), ('targets.json', targets)]:
        write(output / 'local' / name, content)
    data = output / 'data'; data.mkdir()
    (data / 'inputs.jsonl').write_text(''.join(json.dumps(j, ensure_ascii=False) + '\n' for j in sorted(jobs, key=lambda r: r['job_id'])))
    write(data / 'config.json', CONFIG); write(data / 'worker-provenance.json', provenance)
    write(data / 'input-manifest.json', {'sha256': {n: sha(data / n) for n in ('inputs.jsonl', 'config.json', 'worker-provenance.json')}})
    protocol = {'experiment': CONFIG['experiment'], 'source_archive_sha256': sha(SOURCE),
                'prepared_before_inference': True, 'cases': len(cases), 'main_calls': len(jobs), 'repeat_calls': 2,
                'splits': dict(Counter(c['split'] for c in cases)), 'contracts': ['plain', 'structured'],
                'primary': 'Confirmation answer/status accuracy by answerable/infeasible/insufficient, paired per original parent case.',
                'gates': {'confirmation_unknown_min_correct_of_24': 20, 'unknown_must_improve_over_plain': True,
                          'confirmation_answerable_min_correct_of_24': 22, 'confirmation_infeasible_min_correct_of_24': 20,
                          'known_answer_max_regression_vs_plain_of_48': 2,
                          'confirmation_required_citation_min_correct_of_48': 44,
                          'confirmation_max_invalid_of_36': 1, 'confirmation_max_token_limits': 0},
                'gate_scope': 'Predeclared engineering checks, not statistical generalization claims.',
                'differences_from_old_canary': ['Both arms get opaque evidence IDs.', 'Common max_new_tokens=256 instead of 64.',
                                              'Structured arm changes system instructions and output schema together; not an isolated schema causal claim.'],
                'confirmation_scope': 'Constructed price/name/ID/order/wording variants of the same 36 controls, not new independent source families.',
                'no_adaptive_tuning': 'Both arms and all cases frozen before inference; no prompt search after inspecting development.',
                'scope': 'Worker functionality only; no selector training, main travel relabeling, or full MemoryArena score.',
                'local_sha256': {p.name: sha(p) for p in (output / 'local').iterdir()},
                'source_sha256': {n: sha(ROOT / n) for n in ['data_pipeline/prepare_worker_contract.py', 'training/memory_selector/worker_contract_protocol.py']}}
    write(output / 'protocol.json', protocol)
    validate_inputs(data)
    return {k: protocol[k] for k in ['experiment', 'cases', 'main_calls', 'repeat_calls', 'splits']}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(prepare(args.output), indent=2))

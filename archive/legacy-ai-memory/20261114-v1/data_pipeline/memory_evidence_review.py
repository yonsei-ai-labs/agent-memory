"""Review evidence without turning a worker outcome or reference ID into truth.

The validators check identities, quoted spans and internal consistency. They do
not establish semantic entailment; provisional reviews still need adjudication.
"""
import argparse
import json
from pathlib import Path

from data_pipeline.memory_decision_dataset import readl

SCHEMA = 'memory-evidence-review-v1'
VIEWS = ('goal', 'goal_and_observations')


def unique(values, name):
    if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
        raise ValueError(f'{name} must be a list of IDs')
    if len(values) != len(set(values)):
        raise ValueError(f'Duplicate {name}')
    return set(values)


def validate_state(state, memories):
    data = state['input']
    if set(data) != {'goal', 'current_observations', 'candidate_memory_ids'}:
        raise ValueError('Unapproved selector input field')
    if any(not isinstance(data[k], str) or not data[k].strip()
           for k in ('goal', 'current_observations')):
        raise ValueError('Goal and pre-selection observations required')
    candidates = unique(data['candidate_memory_ids'], 'candidate IDs')
    scope = state['retrieval_scope']
    if set(scope) != {'bank_memory_ids', 'bank_inventory_complete'} or type(scope['bank_inventory_complete']) is not bool:
        raise ValueError('Explicit bank scope required')
    bank = unique(scope['bank_memory_ids'], 'bank IDs')
    if not candidates <= bank:
        raise ValueError('Candidate outside declared bank')
    for mid in bank:
        memory = memories[mid]
        if memory['source_family_id'] != state['family_id'] or memory['prior_round'] >= state['decision_round']:
            raise ValueError('Cross-family or future memory')
        if memory['memory_id'] != mid or not isinstance(memory['text'], str):
            raise ValueError('Invalid memory document')


def selected_ids(state, ordered_ids):
    selected = unique(ordered_ids, 'selected IDs')
    if not selected <= set(state['input']['candidate_memory_ids']):
        raise ValueError('Selected memory outside candidate pool')
    return selected


def render_selector(state, memories, view, ordered_bundle_ids=None):
    """Only the selector's observations change; never render review metadata."""
    validate_state(state, memories)
    if view not in VIEWS:
        raise ValueError('Unknown selector view')
    ids = state['input']['candidate_memory_ids'] if ordered_bundle_ids is None else ordered_bundle_ids
    selected_ids(state, ids)
    text = 'Current goal:\n' + state['input']['goal']
    if view == 'goal_and_observations':
        text += '\nCurrent observations:\n' + state['input']['current_observations']
    label = 'Available memory candidates' if ordered_bundle_ids is None else 'Proposed memory bundle'
    return text + '\n' + label + ':\n' + ('\n'.join(f'[{mid}] {memories[mid]["text"]}' for mid in ids) or '(none)')


def documents(state, memories):
    validate_state(state, memories)
    return {'goal': state['input']['goal'], 'observations': state['input']['current_observations'],
            **{'memory:' + mid: memories[mid]['text'] for mid in state['retrieval_scope']['bank_memory_ids']}}


def check_span(span, docs):
    if set(span) != {'document_id', 'start', 'end', 'quote'}:
        raise ValueError('Invalid citation fields')
    doc = span['document_id']
    if doc not in docs:
        raise ValueError('Citation outside decision-time evidence scope')
    start, end = span['start'], span['end']
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(docs[doc]):
        raise ValueError('Invalid citation offsets')
    if docs[doc][start:end] != span['quote']:
        raise ValueError('Invented or changed evidence quote')
    return doc


def locate_support(alternatives, current, selected, candidates, bank, bank_checked):
    """An alternative is a SET of jointly required documents; alternatives are OR.

    This operates on reviewer-asserted applicable support, not arbitrary matching
    text. No support found means absence in a reviewed scope, not world absence.
    """
    for name, scope in [('already_observed', current), ('in_selected_bundle', current | selected),
                        ('in_candidates', current | candidates), ('in_bank_outside_candidates', current | bank)]:
        if any(alt and alt <= scope for alt in alternatives):
            return name
    return 'not_found_in_reviewed_bank' if bank_checked else 'undetermined'


def validate_review(row, state, memories):
    fields = {'schema', 'state_id', 'ordered_selected_memory_ids', 'reviewer_id', 'review_status',
              'outcomes_seen', 'requirements_complete', 'requirements', 'bundle_sufficiency',
              'rationale', 'training_eligible'}
    if set(row) != fields or row['schema'] != SCHEMA or row['state_id'] != state['state_id']:
        raise ValueError('Invalid review identity or fields')
    if row['review_status'] != 'provisional' or row['training_eligible'] is not False:
        raise ValueError('Provisional evidence cannot become training gold in this workflow')
    if not row['reviewer_id'] or not row['rationale'] or type(row['outcomes_seen']) is not bool or type(row['requirements_complete']) is not bool:
        raise ValueError('Explicit reviewer, exposure and rationale required')
    selected = {'memory:' + mid for mid in selected_ids(state, row['ordered_selected_memory_ids'])}
    docs = documents(state, memories)
    current = {'goal', 'observations'}
    candidates = {'memory:' + mid for mid in state['input']['candidate_memory_ids']}
    bank = {'memory:' + mid for mid in state['retrieval_scope']['bank_memory_ids']}
    if not row['requirements']:
        raise ValueError('No reviewed requirements')
    ids, statuses, locations = set(), {}, {}
    for req in row['requirements']:
        if set(req) != {'requirement_id', 'description', 'goal_evidence', 'support_alternatives',
                        'counter_evidence', 'reviewed_document_ids', 'context_status', 'applicability_note'}:
            raise ValueError('Invalid requirement fields')
        rid = req['requirement_id']
        if not isinstance(rid, str) or not rid or rid in ids:
            raise ValueError('Missing or duplicate requirement ID')
        ids.add(rid)
        if check_span(req['goal_evidence'], docs) != 'goal':
            raise ValueError('Requirement must be tied to the current goal')
        checked = unique(req['reviewed_document_ids'], 'reviewed document IDs')
        if not checked <= docs.keys() or not current | selected <= checked:
            raise ValueError('Review must cover current context and selected records')
        alternatives = []
        for alt in req['support_alternatives']:
            if not isinstance(alt, list) or not alt:
                raise ValueError('Empty support alternative')
            cited = {check_span(span, docs) for span in alt}
            if not cited <= checked:
                raise ValueError('Unreviewed supporting document')
            alternatives.append(cited)
        counters = {check_span(span, docs) for span in req['counter_evidence']}
        if not counters <= checked:
            raise ValueError('Unreviewed counter evidence')
        status = req['context_status']
        supported = any(alt <= current | selected for alt in alternatives)
        if status not in {'supported', 'missing', 'conflicted', 'undetermined'}:
            raise ValueError('Invalid requirement status')
        if (status == 'supported' and not supported) or (status == 'missing' and supported):
            raise ValueError('Requirement status contradicts available support')
        if status == 'conflicted' and (not supported or not counters & (current | selected)):
            raise ValueError('Conflict needs supplied supporting and counter evidence')
        if not req['description'] or not req['applicability_note']:
            raise ValueError('Requirement and applicability explanation required')
        statuses[rid] = status
        locations[rid] = locate_support(alternatives, current, selected, candidates, bank,
            state['retrieval_scope']['bank_inventory_complete'] and bank <= checked)
    sufficiency = row['bundle_sufficiency']
    if sufficiency not in {'sufficient', 'insufficient', 'undetermined'}:
        raise ValueError('Invalid semantic sufficiency')
    if sufficiency == 'sufficient' and (not row['requirements_complete'] or set(statuses.values()) != {'supported'}):
        raise ValueError('Sufficient requires explicitly complete, supported requirements')
    if sufficiency == 'insufficient' and not set(statuses.values()) & {'missing', 'conflicted'}:
        raise ValueError('Insufficient needs a stated unmet requirement')
    return {'state_id': row['state_id'], 'ordered_selected_memory_ids': row['ordered_selected_memory_ids'],
            'requirement_locations': locations,
            'missing_requirement_ids': [rid for rid, status in statuses.items() if status == 'missing'],
            'bundle_sufficiency': sufficiency, 'training_eligible': False}


def validate_reviews(path, states, memories):
    results, seen = [], set()
    for row in readl(path):
        key = (row['state_id'], tuple(row['ordered_selected_memory_ids']))
        if key in seen:
            raise ValueError('Duplicate bundle review')
        seen.add(key)
        results.append(validate_review(row, states[row['state_id']], memories))
    return {'provisional_reviews': len(results), 'adjudicated_gold': 0, 'training_eligible': False, 'reviews': results}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--memories', type=Path, required=True)
    parser.add_argument('--reviews', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate_reviews(args.reviews,
        {r['state_id']: r for r in readl(args.inputs)},
        {r['memory_id']: r for r in readl(args.memories)}), ensure_ascii=False, indent=2))

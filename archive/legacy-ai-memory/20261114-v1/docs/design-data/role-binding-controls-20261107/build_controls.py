"""Print synthetic role controls; never train a model or alter an existing dataset."""
from collections import Counter
import itertools
import json


SCOPES = ['tests/test_alpha.py', 'tests/test_bravo.py', 'tests/test_delta.py']


def render(memory, requested, completed, template):
    request = requested if requested is not None else 'not specified'
    done = completed if completed is not None else 'record unavailable'
    if template == 'A':
        state = (f'Current verification target: {request}.\n'
                 f'Entire test scope already checked on the current working tree: {done}.\n'
                 'When available, this is the complete list of checked scopes. No code changed afterwards.')
        candidate = f'The earlier command explicitly selected this whole test scope: {memory}.'
    else:
        state = (f'For the next verification, the requested test scope is {request}.\n'
                 f'On this unchanged working tree, the record of fully checked scopes is: {done}.\n'
                 'An available record lists every scope checked so far.')
        candidate = f'A previous test command included the entire scope {memory} in its explicit selection.'
    return {'state_text': state, 'candidate_text': candidate}


def stratum(memory, requested, completed):
    if requested is None and completed is None:
        return 'both_unknown'
    if requested is None:
        return 'requested_unknown'
    if completed is None:
        return 'completed_unknown'
    if requested == memory and completed == memory:
        return 'both_match'
    if requested == memory:
        return 'requested_only_matches'
    if completed == memory:
        return 'completed_only_matches'
    return 'neither_matches'


def build():
    assert len({len(s) for s in SCOPES}) == 1
    rows = []
    for m, q, c in itertools.product(range(3), range(4), range(4)):
        memory = SCOPES[m]
        requested = SCOPES[q] if q < 3 else None
        completed = SCOPES[c] if c < 3 else None
        identity = f'm{m}-q{q}-c{c}'
        for template in ['A', 'B']:
            rows.append({
                'sample_id': f'role-control-{identity}-{template}',
                'base_condition_id': identity,
                'family_id': 'synthetic-equal-character-length-scopes',
                'annotation_kind': 'synthetic_role_binding_control_no_observed_execution',
                'template': template,
                'selected_scope': memory,
                'controlled_state': {'requested_scope': requested, 'completed_scope': completed},
                'stratum': stratum(memory, requested, completed),
                'input': render(memory, requested, completed, template),
                'labels': [2 if requested is None else int(requested != memory),
                           2 if completed is None else int(completed == memory)],
                'utility_label': None,
            })
    assert len(rows) == len({r['sample_id'] for r in rows}) == 96
    assert len({r['base_condition_id'] for r in rows}) == 48
    return {
        'dataset': 'synthetic-role-binding-controls-20261107-v1',
        'purpose': 'Frozen-model role and surface-cue diagnostic; not a new real-source benchmark.',
        'labels': {'compatibility': ['compatible', 'conflict', 'unknown'],
                   'redundancy': ['not_already_present', 'already_present', 'unknown']},
        'scope_characters': len(SCOPES[0]),
        'token_lengths_verified_equal': False,
        'independent_source_experiences': 0,
        'base_conditions': 48,
        'model_predictions_available': False,
        'existing_scope_profile_compatible': False,
        'partition': 'diagnostic_only_no_train_test_claim',
        'counts_by_stratum': dict(sorted(Counter(r['stratum'] for r in rows).items())),
        'rows': rows,
    }


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False, indent=2))

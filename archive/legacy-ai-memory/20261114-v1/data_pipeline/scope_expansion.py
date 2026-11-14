"""Reviewed source identifiers and controlled scope relations, never utility labels.

Only the selected scope in the source command is observed. Requested/completed
states are explicitly constructed. This module never executes source commands.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import itertools
import json
from pathlib import Path
import re
import shlex
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEV_REPOS = {'django/django', 'pvlib/pvlib-python'}
PARQUET_SHA = 'ac76e9efe75978f83d4daec98a55604587dd58b0f1509364893b8778cbf5b487'
PUBLIC_REVISION = '08e109b4a59eaeebf80e4675cd125d42e7ac99a4'
FILES = ['train-small.jsonl', 'train-wide.jsonl', 'train-wide-ab.jsonl', 'train-public-ab.jsonl',
         'dev.jsonl', 'legacy.jsonl', 'controls.jsonl', 'source-review.json']
UNKNOWN_REQUEST = 'not specified'
UNKNOWN_RECORD = 'record unavailable'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def selected_scopes(command):
    """Conservative syntactic selection, not evidence that a run completed.

    Mixed whole-file and node selectors are allowed, but only whole-file
    selectors are returned. Filters and shell composition are rejected.
    """
    try:
        words = shlex.split(command.removeprefix('cd /testbed && '))
    except ValueError:
        return []
    if any(any(c in w for c in [';', '|', '&', '>', '<', '`', '$', '*', '?']) for w in words):
        return []
    if words[:3] == ['python', '-m', 'pytest']:
        runner, words = 'pytest', words[3:]
    elif words[:1] == ['pytest']:
        runner, words = 'pytest', words[1:]
    elif words[:2] == ['python', 'tests/runtests.py']:
        runner, words = 'django', words[2:]
    else:
        return []
    selected = []
    i = 0
    while i < len(words):
        word = words[i]
        if runner == 'django' and word in ['--settings', '--verbosity']:
            if i + 1 >= len(words):
                return []
            i += 2
            continue
        if word in ['--disable-warnings', '--noinput'] or re.fullmatch(r'-[qvsx]{1,6}', word):
            i += 1
            continue
        if re.fullmatch(r'--maxfail=\d+|--tb=(short|long|auto|line|no|native)', word):
            i += 1
            continue
        if runner == 'django' and word.startswith(('--verbosity=', '--settings=')):
            i += 1
            continue
        if word.startswith('-'):
            return []
        if runner == 'pytest':
            if re.fullmatch(r'[A-Za-z0-9_./-]+\.py', word):
                selected.append(word)
            elif not re.fullmatch(r'[A-Za-z0-9_./-]+\.py::.+', word):
                return []
        elif re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', word):
            selected.append(word)
        else:
            return []
        i += 1
    return sorted(set(selected))


def render(scope, requested, completed, template):
    q = requested if requested is not None else UNKNOWN_REQUEST
    c = completed if completed is not None else UNKNOWN_RECORD
    if template == 'A':
        state = (f'Current verification target: {q}.\n'
                 f'Entire test scope already checked on the current working tree: {c}.\n'
                 'When available, this is the complete list of checked scopes. No code changed afterwards.')
        memory = f'The earlier command explicitly selected this whole test scope: {scope}.'
    elif template == 'B':
        state = (f'For the next verification, the requested test scope is {q}.\n'
                 f'On this unchanged working tree, the record of fully checked scopes is: {c}.\n'
                 'An available record lists every scope checked so far.')
        memory = f'A previous test command included the entire scope {scope} in its explicit selection.'
    elif template == 'C':
        # New wording; same closed-world record and unchanged-code assumption.
        state = (f'Requested verification scope = {q}\n'
                 f'Complete record of fully checked scopes = {c}\n'
                 'The code has not changed since the recorded checks. The record, if available, is exhaustive.')
        memory = f'Explicit whole scope selected by the historical command = {scope}'
    else:
        raise ValueError('Unknown template')
    return {'state_text': state, 'candidate_text': memory}


def parse_input(inputs):
    """Independent text-only parser for the narrow, explicitly stated contract."""
    state, memory = inputs['state_text'], inputs['candidate_text']
    lines = state.splitlines()
    if len(lines) != 3:
        raise ValueError('Missing record/completeness statements')
    if lines[0].startswith('Current verification target: '):
        q = lines[0].removeprefix('Current verification target: ').removesuffix('.')
        c = lines[1].removeprefix('Entire test scope already checked on the current working tree: ').removesuffix('.')
        m = memory.removeprefix('The earlier command explicitly selected this whole test scope: ').removesuffix('.')
    elif lines[0].startswith('For the next verification, the requested test scope is '):
        q = lines[0].removeprefix('For the next verification, the requested test scope is ').removesuffix('.')
        c = lines[1].removeprefix('On this unchanged working tree, the record of fully checked scopes is: ').removesuffix('.')
        m = memory.removeprefix('A previous test command included the entire scope ').removesuffix(' in its explicit selection.')
    elif lines[0].startswith('Requested verification scope = '):
        q = lines[0].split(' = ', 1)[1]
        c = lines[1].removeprefix('Complete record of fully checked scopes = ')
        m = memory.removeprefix('Explicit whole scope selected by the historical command = ')
    else:
        raise ValueError('Unrecognized input wording')
    return m, None if q == UNKNOWN_REQUEST else q, None if c == UNKNOWN_RECORD else c


def labels_from_text(inputs):
    m, q, c = parse_input(inputs)
    return [2 if q is None else (0 if q == m else 1),
            2 if c is None else (1 if m in {c} else 0)]


def stratum(labels):
    q, c = labels
    if q == c == 2:
        return 'both_unknown'
    if q == 2:
        return 'requested_unknown'
    if c == 2:
        return 'completed_unknown'
    return {(0, 1): 'both_match', (0, 0): 'requested_only_matches',
            (1, 1): 'completed_only_matches', (1, 0): 'neither_matches'}[q, c]


def input_key(inputs):
    return json.dumps(inputs, sort_keys=True, ensure_ascii=False)


def construct(families, templates, maximum_scopes=None):
    merged = {}
    for family in families:
        choices = sorted(family['scopes'])
        if maximum_scopes is not None:
            choices = choices[:maximum_scopes]
        if len(choices) < 2:
            continue
        for m, q, c, template in itertools.product(choices, choices + [None], choices + [None], templates):
            inputs = render(m, q, c, template)
            labels = [2 if q is None else int(q != m), 2 if c is None else int(c == m)]
            key = input_key(inputs)
            row = merged.get(key)
            if row is None:
                row = {'sample_id': hashlib.sha256(key.encode()).hexdigest()[:24],
                       'family_id': family['repository'], 'repository': family['repository'],
                       'source_families': [], 'source_observation_ids': [],
                       'split': family['split'], 'template': template,
                       'selected_scope': m, 'controlled_state': {'requested_scope': q, 'completed_scope': c},
                       'input': inputs, 'labels': labels, 'stratum': stratum(labels),
                       'annotation_kind': 'source_selected_scope_with_constructed_current_state',
                       'decision_stage': 'controlled_pre_execution_scope_check',
                       'utility_label': None}
                merged[key] = row
            if row['labels'] != labels or row['split'] != family['split'] or row['repository'] != family['repository']:
                raise ValueError('Duplicate input crosses labels/splits/repositories')
            row['source_families'].append(family['source'])
            row['source_observation_ids'].extend(family['scopes'][m])
    for r in merged.values():
        r['source_families'] = sorted(set(r['source_families']))
        r['source_observation_ids'] = sorted(set(r['source_observation_ids']))
    return sorted(merged.values(), key=lambda r: r['sample_id'])


def validate_rows(rows):
    seen = set()
    for r in rows:
        if r['sample_id'] in seen or set(r['input']) != {'state_text', 'candidate_text'}:
            raise ValueError('Duplicate ID or unexpected learner input')
        seen.add(r['sample_id'])
        if r['labels'] != labels_from_text(r['input']) or r['utility_label'] is not None:
            raise ValueError('Text/label or utility contract violation')
        if r['input'] != render(r['selected_scope'], r['controlled_state']['requested_scope'],
                                r['controlled_state']['completed_scope'], r['template']):
            raise ValueError('Input is not the declared complete-record scenario')
    return {'rows': len(rows), 'label_counts': [dict(Counter(r['labels'][h] for r in rows)) for h in [0, 1]],
            'strata': dict(Counter(stratum(r['labels']) for r in rows))}


def validate_bundle(data):
    manifest = json.loads((data / 'manifest.json').read_text())
    if set(manifest['export_sha256']) != set(FILES):
        raise ValueError('Unexpected bundle files')
    loaded = {}
    for name, expected in manifest['export_sha256'].items():
        if sha(data / name) != expected:
            raise ValueError('Changed export: ' + name)
        if name.endswith('.jsonl'):
            loaded[name] = [json.loads(x) for x in (data / name).read_text().splitlines()]
            validate_rows(loaded[name])
    training = [r for n, rows in loaded.items() if n.startswith('train-') for r in rows]
    evaluation = [r for n, rows in loaded.items() if not n.startswith('train-') for r in rows]
    if {input_key(r['input']) for r in training} & {input_key(r['input']) for r in evaluation}:
        raise ValueError('Training/evaluation input leakage')
    if {r['repository'] for r in training} & {r['repository'] for r in evaluation if 'repository' in r}:
        raise ValueError('Training/evaluation repository leakage')
    review = json.loads((data / 'source-review.json').read_text())
    allowed = {o['observation_id']: o for o in review['observations'] if o['status'] == 'included_identifier_evidence'}
    for rows in loaded.values():
        for r in rows:
            if r.get('annotation_kind') == 'source_selected_scope_with_constructed_current_state':
                if not r['source_observation_ids'] or any(o not in allowed or r['selected_scope'] not in allowed[o]['selected_scopes'] for o in r['source_observation_ids']):
                    raise ValueError('Memory scope missing approved source command')
    return {name: validate_rows(rows) for name, rows in loaded.items()}


def validate_training_config(config):
    if config['precision'] != 'float32' or config['updates'] <= 0 or config['batch_size'] <= 0:
        raise ValueError('Invalid precision or training budget')
    if not config['evaluation_steps'] or sorted(set(config['evaluation_steps'])) != config['evaluation_steps'] or config['evaluation_steps'][-1] != config['updates'] or config['evaluation_steps'][0] <= 0:
        raise ValueError('Evaluation steps must be positive, ordered and include the final step')
    if config['script_budget_seconds'] <= 0 or config['max_length'] <= 0:
        raise ValueError('Invalid runtime/context budget')
    if any(n not in [7, 14, 21, 28] for n in config['probe_layers']):
        raise ValueError('Unsupported predeclared probe layer')
    names = set()
    for v in config['variants']:
        if not re.fullmatch(r'[a-z0-9_-]+', v['name']) or v['name'] in names:
            raise ValueError('Invalid or duplicate variant name')
        names.add(v['name'])
        if v['architecture'] not in ['linear', 'pair', 'pair-no-length', 'split', 'joint', 'role', 'role-aligned']:
            raise ValueError('Unknown architecture')
        if v['train_split'] + '.jsonl' not in FILES or not v['train_split'].startswith('train-'):
            raise ValueError('Training must use a designated training split')
        updates=v.get('updates',config['updates'])
        evaluation_steps=v.get('evaluation_steps',config['evaluation_steps'])
        if (not isinstance(updates,int) or updates<=0 or not evaluation_steps or
            sorted(set(evaluation_steps))!=evaluation_steps or evaluation_steps[0]<=0 or evaluation_steps[-1]!=updates):
            raise ValueError('Invalid variant-specific update/evaluation schedule')
        layer = v.get('layer', 'last')
        if layer != 'last' and (v['lora'] or v['architecture'] != 'linear' or layer not in {f'block{n}' for n in config['probe_layers']}):
            raise ValueError('Layer probes must be frozen separate-input linear heads')
    if not names:
        raise ValueError('No variants requested')


def collect_local(bundle):
    with zipfile.ZipFile(bundle) as z:
        observations = {}
        for s in ['train', 'dev', 'test']:
            for line in z.read('data/' + s + '.jsonl').decode().splitlines():
                r = json.loads(line)
                observations[r['observation_id']] = r
    catalog = []
    family_scopes = defaultdict(lambda: defaultdict(list))
    repos = {}
    for oid, r in sorted(observations.items()):
        provenance = r['provenance']; path = ROOT / provenance['path']
        if sha(path) != provenance['sha256']:
            raise ValueError('Source changed: ' + str(path))
        original = json.loads(path.read_text().splitlines()[provenance['line'] - 1])
        if original.get('action', {}).get('command') != provenance['command']:
            raise ValueError('Source command line does not match')
        scopes = selected_scopes(provenance['command'])
        family = r['source_instance_id']; repos[family] = r['group_id']
        for scope in scopes:
            family_scopes[family][scope].append(oid)
        catalog.append({'observation_id': oid, 'source': family, 'repository': r['group_id'],
                        'provenance': provenance, 'selected_scopes': scopes,
                        'status': 'pending_family_check' if scopes else 'excluded_no_supported_whole_scope',
                        'reason': 'Only explicit whole-file/module selection is used; no completion or passing claim.' if scopes else
                                  'No conservative whole-file/module selector; filtered/unsupported commands are excluded.'})
    families = []
    for source, scopes in sorted(family_scopes.items()):
        if len(scopes) >= 2:
            families.append({'source': source, 'repository': repos[source],
                             'split': 'dev' if repos[source] in DEV_REPOS else 'train', 'scopes': dict(scopes)})
    included = {f['source'] for f in families}
    for row in catalog:
        if row['status'] == 'pending_family_check':
            row['status'] = 'included_identifier_evidence' if row['source'] in included else 'excluded_single_scope_family'
            if row['source'] not in included:
                row['reason'] = 'Only one observed whole scope; no invented alternative is added to this source.'
    return {'observations': catalog, 'families': families, 'reviewer': 'single_researcher_source_and_command_review',
            'source_bundle_sha256': sha(bundle), 'current_states_observed': False}


def build(output, public_parquet=None):
    bundle = ROOT / 'training/memory_selector/outputs/20261107T074128Z/upload.zip'
    review = collect_local(bundle)
    train = [f for f in review['families'] if f['split'] == 'train']
    dev = [f for f in review['families'] if f['split'] == 'dev']
    output.mkdir(parents=True, exist_ok=False)
    public_train, external = [], []
    public_review = None
    if public_parquet is not None:
        from data_pipeline.collect_scope_sources import collect
        public_review = collect(public_parquet, output / 'evidence/public')
        public_train = [f for f in public_review['families'] if f['split'] == 'train']
        external = [f for f in public_review['families'] if f['split'] == 'external']
        review['observations'].extend(public_review['observations'])
        review['families'].extend(public_review['families'])
        write_json(output / 'public-source-review.json', public_review)
    rows = {'train-small.jsonl': construct(train, ['A'], 2),
            'train-wide.jsonl': construct(train, ['A']),
            'train-wide-ab.jsonl': construct(train, ['A', 'B']),
            'train-public-ab.jsonl': construct(train + public_train, ['A', 'B']),
            'dev.jsonl': construct(dev, ['A', 'B', 'C'])}
    with zipfile.ZipFile(ROOT / 'training/memory_selector/outputs/20261107T104953Z/upload.zip') as z:
        rows['legacy.jsonl'] = [json.loads(line) for line in z.read('data/dev.jsonl').decode().splitlines()]
    controls = ROOT / 'docs/design-data/role-binding-controls-20261107/controls.json'
    rows['controls.jsonl'] = json.loads(controls.read_text())['rows']
    for name, values in rows.items():
        validate_rows(values)
        (output / name).write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in values))
    write_json(output / 'source-review.json', review)
    external_rows = construct(external, ['A', 'B', 'C'])
    (output / 'external.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False, sort_keys=True) + '\n' for r in external_rows))
    if {r['repository'] for r in external_rows} & {r['repository'] for r in rows['train-public-ab.jsonl'] + rows['dev.jsonl']}:
        raise ValueError('External repository overlaps training/development')
    manifest = {'dataset': 'scope-expansion-20261108-v1', 'created_before_model_evaluation': True,
                'source_review': 'source-review.json', 'counts': {name: validate_rows(values) for name, values in rows.items()},
                'source_families': review['families'], 'export_sha256': {name: sha(output / name) for name in FILES},
                'external_holdout': {'file': 'external.jsonl', 'sha256': sha(output / 'external.jsonl'),
                                     'counts': validate_rows(external_rows), 'excluded_from_training_upload': True,
                                     'repositories': sorted({r['repository'] for r in external_rows})},
                'original_scope_controls_sha256': sha(controls),
                'limitations': ['All current requested/completed states are constructed, not observed target executions.',
                                'Only actual selected source identifiers are expanded; source families were already inspected.',
                                'Duplicate visible inputs are merged; source parents are retained.',
                                'Source/format development metrics are not new independent memory utility evidence.',
                                'A text parsing/equality rule is perfect by the bounded task contract.']}
    write_json(output / 'manifest.json', manifest)
    validate_bundle(output)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--public-parquet', type=Path)
    args = parser.parse_args()
    manifest = build(args.output, args.public_parquet)
    print(json.dumps(manifest['counts'], ensure_ascii=False, indent=2))

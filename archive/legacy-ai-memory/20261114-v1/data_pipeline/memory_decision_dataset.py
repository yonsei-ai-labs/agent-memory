"""Export observed memory interventions with current-state inputs and masked outcomes.

Uses saved, audited trajectories only. No model generation or synthetic effects.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
from evaluation.memory_outcome_labels import outcome_vector, summarize_vectors
from training.memory_selector.travel_state_protocol import state, current_messages

SOURCE = ROOT / 'evaluation/results/travel-policy-followup-20261109-v1'
SCHEMA = 'observed-memory-decisions-v2'
DATA_FILES = {'state-inputs.jsonl', 'memories.jsonl', 'supervision.jsonl', 'provenance.jsonl',
              'exploratory-family-folds.json', 'contract.json', 'summary.json'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def signature(messages):
    return digest(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode())


def readl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in rows))


def validate_input(row, memories):
    if set(row['input']) != {'current_context', 'candidate_memory_ids'}:
        raise ValueError('Unexpected input field; supervision must be separate')
    data = row['input']
    if not isinstance(data['current_context'], str) or not data['current_context'].strip():
        raise ValueError('Current observations required')
    ids = data['candidate_memory_ids']
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate memory identity')
    for mid in ids:
        memory = memories[mid]
        if set(memory) != {'memory_id', 'text', 'source_family_id', 'prior_round'}:
            raise ValueError('Unexpected memory field')
        if memory['source_family_id'] != row['family_id'] or memory['prior_round'] >= row['decision_round']:
            raise ValueError('Cross-family or future memory')


def render_bundle(row, memories, memory_ids):
    """Explicit allowlist; never renders split, condition, policy, or outcomes."""
    validate_input(row, memories)
    if len(memory_ids) != len(set(memory_ids)) or not set(memory_ids) <= set(row['input']['candidate_memory_ids']):
        raise ValueError('Invalid selected memory IDs')
    return ('Current request and observations:\n' + row['input']['current_context'] +
            '\nCandidate memory bundle:\n' + ('\n'.join(f"[{mid}] {memories[mid]['text']}" for mid in memory_ids) or '(none)'))


def render_selection(row, memories):
    return render_bundle(row, memories, row['input']['candidate_memory_ids']).replace(
        '\nCandidate memory bundle:\n', '\nAvailable memory candidates:\n', 1)


def observed_outcome(labels, state_id, ordered_memory_ids):
    """An unexecuted set is missing data, never an inferred failure or success."""
    key = (state_id, tuple(ordered_memory_ids))
    return labels.get(key)


def export(output, source=SOURCE):
    from tokenizers import Tokenizer
    if output.exists():
        raise FileExistsError(output)
    shared = json.loads((source / 'shared-manifest.json').read_text())
    archive = source / 'predictions.zip'
    if digest(archive.read_bytes()) != shared['archive_sha256']:
        raise ValueError('Source archive changed')
    with zipfile.ZipFile(archive) as z:
        load = lambda name: json.loads(z.read(name))
        cases = {c['case_id']: c for c in load('design/local/cases.json')}
        catalog = load('design/local/catalog.json')
        records = load('design/local/generated-records.json')
        jobs = {r['job_id']: r for r in load('design/local/all-jobs.json')}
        selections = {r['state_id']: r for r in load('design/local/selections.json')}
        config = load('run/artifacts/config.json')
        protocol = load('design/data/protocol.json')
        tokenizer_bytes = z.read('run/artifacts/worker-tokenizer/tokenizer.json')
        tokenizer = Tokenizer.from_str(tokenizer_bytes.decode())
    decisions = json.loads((source / 'decisions.json').read_text())
    rows = [r for r in decisions if r['contract'] == 'original' and r['policy'] != 'all']
    by_state, by_action = defaultdict(list), defaultdict(list)
    for r in rows:
        by_state[r['state_id']].append(r)
        by_action[r['state_id'], tuple(r['selected_memory_ids'])].append(r)
    memories = {}
    for case in cases.values():
        if case['split'] != 'train':
            raise ValueError('Reserved evaluation family in exploratory export')
        for mid in (m['memory_id'] for m in case['memories']):
            record = records[mid]
            memory = {'memory_id': mid, 'text': record['evidence'], 'source_family_id': case['family_id'],
                      'prior_round': record['prior_round']}
            if mid in memories and memories[mid] != memory:
                raise ValueError('Inconsistent memory identity')
            memories[mid] = memory
    inputs, provenance, supervision = [], [], []
    snapshots = {}
    for state_id, group in sorted(by_state.items()):
        first = group[0]
        case = cases[first['case_id']]
        current = state(case, first['condition'], records, catalog)
        candidates = sorted([r['memory_id'] for r in current['bank']],
                            key=lambda mid: digest((state_id + ':' + mid).encode()))
        if set(candidates) != set(selections[state_id]['candidate_ids']):
            raise ValueError('Candidate bank differs from observed run')
        row = {'schema': SCHEMA, 'state_id': state_id, 'family_id': first['family_id'], 'split': 'train',
               'decision_round': 4, 'input': {'current_context': current['public_text'], 'candidate_memory_ids': candidates}}
        validate_input(row, memories)
        inputs.append(row)
        snapshots[state_id] = current
        provenance.append({'state_id': state_id, 'case_id': first['case_id'], 'condition': first['condition'],
                           'family_id': first['family_id'], 'source_archive_sha256': shared['archive_sha256'],
                           'source_job_ids': sorted(r['job_id'] for r in group), 'new_source': False,
                           'current_context_sha256': digest(current['public_text'].encode())})
    input_map = {r['state_id']: r for r in inputs}
    baseline = {r['state_id']: r for r in rows if r['policy'] == 'none'}
    for (sid, mids), aliases in sorted(by_action.items()):
        original = aliases[0]
        vector = outcome_vector(original)
        for r in aliases:
            current = snapshots[sid]
            selected = [records[mid] for mid in r['selected_memory_ids']]
            expected_messages = current_messages(current['public_text'], selected)
            job = jobs[r['job_id']]
            if job['messages'] != expected_messages or signature(expected_messages) != r['generation_id']:
                raise ValueError('Observed job input does not match reconstructed state/action')
            if outcome_vector(r) != vector or r['generation_id'] != original['generation_id']:
                raise ValueError('Aliased actions disagree')
        if vector['input_contract_success'] != original['input_contract_success']:
            raise ValueError('Input-contract outcome changed')
        if vector['counts']['unsupported_infeasibility']['true'] != original['none_without_evidence_components']:
            raise ValueError('Unsupported NONE definition drift')
        delivered_text = '\n'.join(memories[mid]['text'] for mid in mids)
        memory_tokens = len(tokenizer.encode(delivered_text, add_special_tokens=False).ids)
        if memory_tokens > 512:
            raise ValueError('Observed action exceeds shared memory budget')
        render_bundle(input_map[sid], memories, list(mids))
        supervision.append({'state_id': sid, 'ordered_selected_memory_ids': list(mids),
            'generation_id': original['generation_id'], 'worker_contract': 'original',
            'source_job_ids': sorted(r['job_id'] for r in aliases), 'policy_aliases': sorted(r['policy'] for r in aliases),
            'outcomes': vector, 'paired_baseline_generation_id': baseline[sid]['generation_id'],
            'observed_success_delta_vs_none': original['success'] - baseline[sid]['success'],
            'delivered_memory_tokens': memory_tokens,
            'actual_worker_measurement': True, 'observed_repetitions_used': 1})
    families = sorted({r['family_id'] for r in inputs})
    folds = [{'held_family': fam, 'train_state_ids': [r['state_id'] for r in inputs if r['family_id'] != fam],
              'validation_state_ids': [r['state_id'] for r in inputs if r['family_id'] == fam]}
             for fam in families]
    worker = {k: config[k] for k in ('worker_model_id', 'worker_revision', 'do_sample', 'enable_thinking', 'max_new_tokens', 'max_input_tokens')}
    contract = {'schema': SCHEMA, 'state_model_input_allowlist': ['input.current_context', 'input.candidate_memory_ids'],
        'memory_model_input_allowlist': ['memory_id', 'text'], 'supervision_separate': True,
        'worker': worker, 'worker_weight_provenance': protocol['worker_weight_provenance'],
        'worker_prompt_contract': 'original', 'memory_delivery_token_cap': 512,
        'memory_budget_tokenizer': worker['worker_model_id'] + '@' + worker['worker_revision'],
        'memory_budget_tokenizer_sha256': digest(tokenizer_bytes),
        'memory_budget_scope': 'Exact delivered record text joined with newlines; same upper bound, no padding requirement.',
        'research_question': 'With worker and memory bank fixed per state, can goal-, progress-, and failure-aware selection improve task success under a shared context budget?',
        'observed_input_coverage': {'current_goal': True, 'current_tool_observations': True,
                                  'sequential_progress_history': False, 'failed_attempt_history': False},
        'model_comparison': ['Harrier 0.6B base', 'Harrier 0.6B LoRA', 'Qwen3.5-4B selector', 'Qwen3.5-9B selector'],
        'model_training_performed': False, 'exploratory_training_allowed': True,
        'fresh_confirmatory_test_eligible': False, 'folds_are_exploratory_not_fresh_test': True,
        'unobserved_actions': 'Missing outcome; request a separately recorded worker execution, never fill zero.',
        'unknown_error_labels': 'Mask null; do not train as false.', 'selector_cost_available': False,
        'limits': ['Reformatted existing 12 train families; zero new independent experiences.',
                   'Recorded action sets are not all possible memory combinations.',
                   'Mixed policy aliases with identical inputs are one observed action.',
                   'Candidate order is deterministic and unrelated to labels; method must record its final delivery order.',
                   'Component factual infeasibility is relative to common evidence in the original judge, not inaccessible world state.',
                   'No failure-history or multi-step progress ablation is supported by these archived snapshots.',
                   'All-memory policy exceeds the delivery budget and is excluded from the matched-budget export.',
                   'No split of a state/action or family into independent training/test rows.']}
    output.mkdir(parents=True)
    write_rows(output / 'state-inputs.jsonl', inputs)
    write_rows(output / 'memories.jsonl', [memories[k] for k in sorted(memories)])
    write_rows(output / 'supervision.jsonl', supervision)
    write_rows(output / 'provenance.jsonl', provenance)
    write(output / 'exploratory-family-folds.json', folds)
    write(output / 'contract.json', contract)
    summary = {'states': len(inputs), 'families': len(families), 'memory_records': len(memories),
        'state_action_outcomes': len(supervision), 'policy_rows_deduplicated': len(rows),
        'unique_worker_inputs': len({r['generation_id'] for r in supervision}), 'new_source_families': 0,
        'new_model_calls': 0, 'new_relation_labels': 0, 'original_success_labels_unchanged': True,
        'maximum_delivered_memory_tokens': max(r['delivered_memory_tokens'] for r in supervision),
        'outcomes': summarize_vectors([r['outcomes'] for r in supervision])}
    write(output / 'summary.json', summary)
    write(output / 'manifest.json', {'schema': SCHEMA, 'files_sha256': {p.name: digest(p.read_bytes()) for p in sorted(output.iterdir())},
        'source_sha256': {name: digest((source / name).read_bytes()) for name in ('decisions.json', 'shared-manifest.json', 'predictions.zip')},
        'generator_sha256': digest(Path(__file__).read_bytes()),
        'outcome_schema_code_sha256': digest((ROOT / 'evaluation/memory_outcome_labels.py').read_bytes())})
    validate_dataset(output)
    return summary


def validate_dataset(path):
    manifest = json.loads((path / 'manifest.json').read_text())
    if set(manifest['files_sha256']) != DATA_FILES:
        raise ValueError('Incomplete dataset manifest')
    for name, expected in manifest['files_sha256'].items():
        if digest((path / name).read_bytes()) != expected:
            raise ValueError('Changed export: ' + name)
    inputs = readl(path / 'state-inputs.jsonl')
    memory_rows = readl(path / 'memories.jsonl')
    memories = {r['memory_id']: r for r in memory_rows}
    if len(memories) != len(memory_rows):
        raise ValueError('Duplicate memories')
    states = {r['state_id']: r for r in inputs}
    if len(states) != len(inputs):
        raise ValueError('Duplicate states')
    for r in inputs:
        validate_input(r, memories)
    seen, by_state, generation_families = set(), defaultdict(list), defaultdict(set)
    for row in readl(path / 'supervision.jsonl'):
        key = (row['state_id'], tuple(row['ordered_selected_memory_ids']))
        if key in seen:
            raise ValueError('Duplicate action labels')
        seen.add(key)
        render_bundle(states[row['state_id']], memories, row['ordered_selected_memory_ids'])
        by_state[row['state_id']].append(row)
        generation_families[row['generation_id']].add(states[row['state_id']]['family_id'])
        if not 0 <= row['delivered_memory_tokens'] <= 512:
            raise ValueError('Memory budget exceeded')
    if set(by_state) != set(states) or any(len(f) != 1 for f in generation_families.values()):
        raise ValueError('Missing state outcome or cross-family generation reuse')
    for sid, rows in by_state.items():
        baseline = [r for r in rows if not r['ordered_selected_memory_ids']]
        if len(baseline) != 1:
            raise ValueError('Missing or duplicated no-memory baseline')
        baseline = baseline[0]
        if any(r['paired_baseline_generation_id'] != baseline['generation_id'] or
               r['observed_success_delta_vs_none'] != r['outcomes']['task_success'] - baseline['outcomes']['task_success']
               for r in rows):
            raise ValueError('Invalid paired outcome')
    folds = json.loads((path / 'exploratory-family-folds.json').read_text())
    families = {r['family_id'] for r in inputs}
    if len(folds) != len(families) or {r['held_family'] for r in folds} != families:
        raise ValueError('Incomplete family folds')
    for fold in folds:
        train, held = set(fold['train_state_ids']), set(fold['validation_state_ids'])
        if train & held or train | held != set(states):
            raise ValueError('Incomplete/overlapping fold')
        if {states[s]['family_id'] for s in train} & {states[s]['family_id'] for s in held}:
            raise ValueError('Cross-family fold leakage')
        if {states[s]['family_id'] for s in held} != {fold['held_family']}:
            raise ValueError('Incorrect held family')
    return {'states': len(inputs), 'actions': len(seen), 'family_fold_overlap': 0, 'input_contract_checked': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.output), ensure_ascii=False, indent=2))

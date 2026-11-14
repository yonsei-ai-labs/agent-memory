"""Add outcome-independent reference diagnostics and selector views to frozen runs.

This is a sidecar, not a new source dataset or automatically labeled semantic
sufficiency benchmark. It never edits the observed-v2 export or calls a model.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import zipfile

from data_pipeline.memory_decision_dataset import (ROOT, SOURCE, digest, readl, write,
                                                   write_rows, validate_dataset)
from data_pipeline.memory_evidence_review import validate_state, render_selector, VIEWS
from training.memory_selector.travel_state_protocol import state, reference_records, current_messages

BASE = ROOT / 'datasets/memory_decisions/20261109-observed-v2'
SCHEMA = 'memory-evidence-sidecar-v1'
FILES = {'selector-inputs.jsonl', 'state-reference-locations.jsonl', 'bundle-reference-coverage.jsonl',
         'contract.json', 'summary.json', 'evidence-outcome-summary.json'}


def action_key(row):
    return row['state_id'], tuple(row['ordered_selected_memory_ids'])


def reference_location(mid, observed, candidates, bank):
    if mid in observed:
        return 'already_observed'
    if mid in candidates:
        return 'in_candidates'
    if mid in bank:
        return 'in_bank_outside_candidates'
    return 'designated_record_absent_from_bank'


def coverage_for_action(reference_row, ordered_ids):
    selected = set(ordered_ids)
    refs = reference_row['designated_references']
    observed = {r['memory_id'] for r in refs if r['location'] == 'already_observed'}
    ref_ids = {r['memory_id'] for r in refs}
    delivered = (observed | selected) & ref_ids
    return {'state_id': reference_row['state_id'], 'ordered_selected_memory_ids': ordered_ids,
            'proxy_type': 'designated_record_identity_only',
            'designated_reference_count': len(ref_ids),
            'covered_designated_reference_ids': sorted(delivered),
            'complete_designated_reference_coverage': ref_ids <= delivered,
            'newly_added_designated_reference_ids': sorted((selected & ref_ids) - observed),
            'repeated_current_reference_ids': sorted(selected & observed),
            'semantic_sufficiency': None}


def build_inputs(base=BASE, source=SOURCE):
    """Uses only decision-time inputs, provenance and original archived state.

    No worker decisions or outcome values enter evidence construction.
    """
    manifest = json.loads((base / 'manifest.json').read_text())
    for name in ('state-inputs.jsonl', 'memories.jsonl', 'provenance.jsonl'):
        if digest((base / name).read_bytes()) != manifest['files_sha256'][name]:
            raise ValueError('Frozen input changed: ' + name)
    archive = source / 'predictions.zip'
    if digest(archive.read_bytes()) != manifest['source_sha256']['predictions.zip']:
        raise ValueError('Source archive changed')
    with zipfile.ZipFile(archive) as z:
        cases = {c['case_id']: c for c in json.loads(z.read('design/local/cases.json'))}
        records = json.loads(z.read('design/local/generated-records.json'))
        catalog = json.loads(z.read('design/local/catalog.json'))
        jobs = {j['job_id']: j for j in json.loads(z.read('design/local/all-jobs.json'))}
    base_rows = readl(base / 'state-inputs.jsonl')
    provenance = {r['state_id']: r for r in readl(base / 'provenance.jsonl')}
    memories = {m['memory_id']: m for m in readl(base / 'memories.jsonl')}
    inputs, locations = [], []
    for old in base_rows:
        sid = old['state_id']
        origin = provenance[sid]
        case = cases[origin['case_id']]
        snapshot = state(case, origin['condition'], records, catalog)
        goal = f"Current city: {case['city']}.\n{case['question']}"
        prefix = goal + '\n'
        if not snapshot['public_text'].startswith(prefix) or snapshot['public_text'] != old['input']['current_context']:
            raise ValueError('Cannot recover the exact pre-selection context')
        candidates = old['input']['candidate_memory_ids']
        bank = {m['memory_id'] for m in snapshot['bank']}
        if set(candidates) != bank:
            raise ValueError('Archived candidate inventory differs')
        row = {'schema': SCHEMA, 'state_id': sid, 'family_id': old['family_id'],
               'decision_round': old['decision_round'],
               'input': {'goal': goal, 'current_observations': snapshot['public_text'][len(prefix):],
                         'candidate_memory_ids': candidates},
               'retrieval_scope': {'bank_memory_ids': sorted(bank), 'bank_inventory_complete': True},
               'preselection_context_sha256': digest(snapshot['public_text'].encode())}
        validate_state(row, memories)
        for view in VIEWS:
            render_selector(row, memories, view)
        # Confirm the common prefix really reached every saved worker job.
        expected_prefix = snapshot['public_text'] + '\nPast records supplied:\n'
        if any(not jobs[jid]['messages'][-1]['content'].startswith(expected_prefix) for jid in origin['source_job_ids']):
            raise ValueError('Saved worker did not use the same current state')
        refs = reference_records(case['question'], [records[m['memory_id']] for m in case['memories']])
        if len(refs) != 2:
            raise ValueError('Travel proxy expects exactly two valid designated records')
        observed = {r['memory_id'] for r in snapshot['observed']}
        locations.append({'state_id': sid, 'family_id': old['family_id'],
            'proxy_type': 'designated_record_identity_only',
            'designated_references': [{'component': r['slot'], 'memory_id': r['memory_id'],
                'location': reference_location(r['memory_id'], observed, set(candidates), bank)} for r in refs],
            'semantic_requirements_reviewed': False})
        inputs.append(row)
    return inputs, locations, memories


def worker_messages(row, memories, ordered_ids):
    """Neither selector view changes this worker input."""
    render_selector(row, memories, 'goal_and_observations', ordered_ids)
    public = row['input']['goal'] + '\n' + row['input']['current_observations']
    if digest(public.encode()) != row['preselection_context_sha256']:
        raise ValueError('Worker context changed')
    return current_messages(public, [{'evidence': memories[mid]['text']} for mid in ordered_ids])


def outcome_summary(coverage, labels, locations):
    """Join after evidence construction; the result is evaluation-only."""
    cover = {action_key(r): r for r in coverage}
    available = {r['state_id']: all(x['location'] != 'designated_record_absent_from_bank'
                                  for x in r['designated_references']) for r in locations}
    policies = defaultdict(lambda: Counter(states=0, complete_designated_reference_coverage=0, task_success=0))
    for action in labels:
        if not available[action['state_id']]:
            continue
        row = cover[action_key(action)]
        for policy in action['policy_aliases']:
            policies[policy]['states'] += 1
            policies[policy]['complete_designated_reference_coverage'] += int(row['complete_designated_reference_coverage'])
            policies[policy]['task_success'] += action['outcomes']['task_success']
    return {'scope': '72 states with both designated records available in the bank or current observations; 12 existing families',
            'semantic_sufficiency_measured': False, 'policies': dict(sorted(policies.items()))}


def assemble(base=BASE, source=SOURCE):
    validate_dataset(base)
    inputs, locations, memories = build_inputs(base, source)
    loc = {r['state_id']: r for r in locations}
    labels = readl(base / 'supervision.jsonl')
    coverage = [coverage_for_action(loc[a['state_id']], a['ordered_selected_memory_ids']) for a in labels]
    for row in coverage:
        if row['semantic_sufficiency'] is not None:
            raise ValueError('Reference coverage is not a semantic label')
    count_rule = []
    for row in inputs:
        available = all(r['location'] != 'designated_record_absent_from_bank'
                        for r in loc[row['state_id']]['designated_references'])
        n = len(row['input']['candidate_memory_ids'])
        count_rule.append({'state_id': row['state_id'], 'candidate_count': n,
                           'designated_records_available': available,
                           'count_only_prediction': n % 3 == 0})
    summary = {'states': len(inputs), 'families': len({r['family_id'] for r in inputs}),
        'observed_bundles': len(coverage), 'new_source_families': 0, 'new_model_calls': 0,
        'automatic_semantic_labels': 0, 'reference_locations': dict(Counter(
            ref['location'] for row in locations for ref in row['designated_references'])),
        'candidate_count_shortcut': {'rule': 'candidate_count % 3 == 0',
            'target': 'both designated records available in bank or observations, NOT semantic sufficiency',
            'status': 'post_hoc_design_diagnostic_not_generalization_evaluation',
            'correct': sum(r['count_only_prediction'] == r['designated_records_available'] for r in count_rule),
            'total': len(count_rule), 'rows': count_rule}}
    contract = {'schema': SCHEMA, 'purpose': 'Evidence diagnostics for memory benefit/harm research',
        'base_dataset': 'datasets/memory_decisions/20261109-observed-v2',
        'selector_views': list(VIEWS), 'worker_observations_unchanged_between_views': True,
        'memory_bank_and_order_unchanged_between_views': True,
        'selector_input_fields': ['input.goal', 'input.current_observations', 'input.candidate_memory_ids', 'memory_id', 'text'],
        'labels_never_in_selector_inputs': True, 'automatic_reference_labels_are_semantic_gold': False,
        'semantic_null_means': 'Unreviewed; mask, never convert to insufficient or negative utility',
        'outcome_join_key': ['state_id', 'ordered_selected_memory_ids'],
        'unobserved_bundle_outcome': None, 'actual_progress_history': False, 'actual_failure_history': False,
        'fresh_confirmatory_test_eligible': False, 'memory_delivery_token_cap': 512,
        'limits': ['Original 12 train families; no additional independent data.',
                   'Two answer components do not establish within-answer multi-memory reasoning.',
                   'Candidate pool equals the allowed bank here; bank-outside-candidate gaps are not observed.',
                   'Deleting two references exposes a candidate-count shortcut.',
                   'Identity coverage does not verify facts, applicability, alternative proofs or sufficiency.',
                   'Current redundancy does not imply zero or negative observed utility.',
                   'Shared token cap does not constitute length-matched training controls.']}
    return {'selector-inputs.jsonl': inputs, 'state-reference-locations.jsonl': locations,
            'bundle-reference-coverage.jsonl': coverage, 'contract.json': contract,
            'summary.json': summary, 'evidence-outcome-summary.json': outcome_summary(coverage, labels, locations)}


def manifest(base, source):
    return {'schema': SCHEMA,
            'base_manifest_sha256': digest((base / 'manifest.json').read_bytes()),
            'archive_sha256': digest((source / 'predictions.zip').read_bytes()),
            'generator_sha256': digest(Path(__file__).read_bytes()),
            'review_code_sha256': digest((ROOT / 'data_pipeline/memory_evidence_review.py').read_bytes())}


def export(output, base=BASE, source=SOURCE):
    if output.exists():
        raise FileExistsError(output)
    payloads = assemble(base, source)
    output.mkdir(parents=True)
    for name, rows in payloads.items():
        (write_rows if name.endswith('.jsonl') else write)(output / name, rows)
    record = manifest(base, source)
    record['files_sha256'] = {name: digest((output / name).read_bytes()) for name in sorted(FILES)}
    write(output / 'manifest.json', record)
    return {k: v for k, v in payloads['summary.json'].items() if k != 'candidate_count_shortcut'}


def check(path, base=BASE, source=SOURCE):
    record = json.loads((path / 'manifest.json').read_text())
    if {k: v for k, v in record.items() if k != 'files_sha256'} != manifest(base, source):
        raise ValueError('Sidecar source/code identity changed')
    if set(record['files_sha256']) != FILES:
        raise ValueError('Incomplete evidence sidecar')
    expected = assemble(base, source)
    for name, value in expected.items():
        if digest((path / name).read_bytes()) != record['files_sha256'][name]:
            raise ValueError('Changed evidence sidecar: ' + name)
        actual = readl(path / name) if name.endswith('.jsonl') else json.loads((path / name).read_text())
        if actual != value:
            raise ValueError('Evidence sidecar does not reproduce: ' + name)
    return {'states': len(expected['selector-inputs.jsonl']), 'bundles': len(expected['bundle-reference-coverage.jsonl']),
            'semantic_gold_created': 0, 'reproduced_from_frozen_source': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['export', 'check'])
    parser.add_argument('--path', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps((export if args.command == 'export' else check)(args.path), ensure_ascii=False, indent=2))

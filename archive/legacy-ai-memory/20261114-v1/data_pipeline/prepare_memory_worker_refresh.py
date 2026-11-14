"""Re-execute original and contrast candidates together; never reuse old labels."""
import argparse
import json
from pathlib import Path
import tempfile

from data_pipeline.memory_decision_dataset import ROOT, readl, write, write_rows, digest, signature, validate_dataset
from data_pipeline.memory_evidence_dataset import BASE, check as check_evidence
from data_pipeline.prepare_memory_state_ablation import EVIDENCE, identity, family_folds
from data_pipeline.prepare_memory_contrasts import prepare as prepare_contrasts, source_data
from training.memory_selector.travel_state_protocol import state, current_messages

CONFIG = ROOT / 'training/memory_selector/configs/memory-worker-refresh-v1.json'


def reconstruct():
    from tokenizers import Tokenizer
    source, old_outputs, tokenizer_bytes, weights, source_sha = source_data()
    tokenizer = Tokenizer.from_str(tokenizer_bytes.decode())
    cases = {r['case_id']: r for r in source['cases.json']}
    origins = {r['state_id']: r for r in readl(BASE / 'provenance.jsonl')}
    actions = {}
    for old in readl(BASE / 'supervision.jsonl'):
        sid, mids = old['state_id'], old['ordered_selected_memory_ids']
        origin = origins[sid]
        case = cases[origin['case_id']]
        current = state(case, origin['condition'], source['generated-records.json'], source['catalog.json'])
        selected = [source['generated-records.json'][mid] for mid in mids]
        messages = current_messages(current['public_text'], selected)
        if signature(messages) != old['generation_id'] or old_outputs[old['generation_id']]['messages'] != messages:
            raise ValueError('Original worker input reconstruction differs')
        count = len(tokenizer.encode('\n'.join(r['evidence'] for r in selected), add_special_tokens=False).ids)
        if count != old['delivered_memory_tokens'] or count > 512:
            raise ValueError('Original delivery budget differs')
        actions[sid, tuple(mids)] = {'action_id': identity(sid, mids), 'state_id': sid,
            'case_id': case['case_id'], 'family_id': case['family_id'], 'condition': origin['condition'],
            'ordered_selected_memory_ids': mids, 'generation_id': signature(messages), 'messages': messages,
            'delivered_memory_tokens': count, 'original_candidate': True,
            'policy_aliases': old['policy_aliases'], 'contrast_cells': []}
    with tempfile.TemporaryDirectory() as tmp:
        contrast = Path(tmp) / 'contrasts'
        prepare_contrasts(contrast)
        extra = json.loads((contrast / 'local/all-jobs.json').read_text())
        reviews = json.loads((contrast / 'local/record-reviews.json').read_text())
        excluded = json.loads((contrast / 'local/excluded-cases.json').read_text())
        contrast_protocol = json.loads((contrast / 'data/protocol.json').read_text())
    for row in extra:
        key = row['state_id'], tuple(row['selected_memory_ids'])
        if key not in actions:
            mids = row['selected_memory_ids']
            actions[key] = {'action_id': identity(row['state_id'], mids),
                **{k: row[k] for k in ('state_id', 'case_id', 'family_id', 'condition', 'generation_id', 'messages', 'delivered_memory_tokens')},
                'ordered_selected_memory_ids': mids, 'original_candidate': False, 'policy_aliases': [], 'contrast_cells': []}
        action = actions[key]
        if action['generation_id'] != row['generation_id'] or action['messages'] != row['messages']:
            raise ValueError('Same action has different input')
        action['contrast_cells'].append({'job_id': row['job_id'], 'relation_bits': row['relation_bits']})
    actions = sorted(actions.values(), key=lambda r: r['action_id'])
    jobs = {}
    for action in actions:
        if (set(action['ordered_selected_memory_ids']) -
                {r['memory_id'] for r in state(cases[action['case_id']], action['condition'], source['generated-records.json'], source['catalog.json'])['bank']}):
            raise ValueError('Absent or cross-state memory supplied')
        job = {k: action[k] for k in ('generation_id', 'messages')}
        if job['generation_id'] in jobs and jobs[job['generation_id']] != job:
            raise ValueError('Generation identity conflict')
        jobs[job['generation_id']] = job
    # Four outcome-independent strata; each input is repeated later in the full run.
    groups = {
        'none': [r for r in actions if not r['ordered_selected_memory_ids']],
        'original_nonempty': [r for r in actions if r['original_candidate'] and r['ordered_selected_memory_ids']],
        'contrast_one_near': [r for r in actions if any(sum(c['relation_bits']) == 1 for c in r['contrast_cells'])],
        'contrast_two_near': [r for r in actions if any(sum(c['relation_bits']) == 2 for c in r['contrast_cells'])]}
    repeats, repeat_design = [], []
    for category, group in groups.items():
        chosen = min(group, key=lambda r: digest((category + r['generation_id']).encode()))
        repeats.append(jobs[chosen['generation_id']])
        repeat_design.append({'stratum': category, 'generation_id': chosen['generation_id'], 'action_id': chosen['action_id']})
    if len({r['generation_id'] for r in repeats}) != 4:
        raise ValueError('Stability prompts must be distinct')
    if (len(actions), len(jobs), sum(not r['original_candidate'] for r in actions)) != (504, 480, 171):
        raise ValueError('Frozen action population changed')
    return {'actions': actions, 'jobs': [jobs[k] for k in sorted(jobs)], 'repeats': repeats,
            'repeat_design': repeat_design, 'reviews': reviews, 'excluded': excluded,
            'weights': weights, 'source_sha': source_sha,
            'contrast_protocol_sha256': digest((json.dumps(contrast_protocol, ensure_ascii=False, indent=2) + '\n').encode())}


def prepare(output):
    if output.exists():
        raise FileExistsError(output)
    validate_dataset(BASE); check_evidence(EVIDENCE)
    built = reconstruct()
    output.mkdir(parents=True)
    data, local = output / 'data', output / 'local'
    data.mkdir(); local.mkdir()
    write_rows(local / 'actions.jsonl', built['actions'])
    for name, key in [('repeat-design.json', 'repeat_design'), ('record-reviews.json', 'reviews'), ('excluded-cases.json', 'excluded')]:
        write(local / name, built[key])
    write(local / 'family-folds.json', family_folds([r['family_id'] for r in built['actions']], 3))
    write(data / 'pending-jobs.json', built['jobs'])
    write(data / 'repeat-jobs.json', built['repeats'])
    write(data / 'protocol.json', {'experiment': 'memory-worker-refresh-20261110-v1',
        'source_archive_sha256': built['source_sha'], 'worker_weight_provenance': built['weights'],
        'pending_unique_prompts': 480, 'reused_old_outputs': 0, 'planned_decisions': 504,
        'original_decisions': 333, 'new_candidate_decisions': 171, 'nonempty_candidates': 408,
        'states': 96, 'families': 12, 'requests': 24, 'new_independent_source_families': 0,
        'repeat_calls': 4, 'expected_total_calls': 484, 'memory_token_cap': 512,
        'candidate_design': 'All 333 original actions plus 171 new actions from the unchanged 19-request, 57-state, four-cell contrast design.',
        'collection': 'Execute all 480 unique messages afresh in one worker process. Same messages shared by states reuse that new output only.',
        'stability_rule': 'Four fresh repeat prompts must match input tokens and every non-cost outcome axis for all linked actions. Output-token differences are reported; no requirement to match the previous dataset.',
        'failure_rule': 'Do not promote an incomplete run or a non-cost stability mismatch to a completed learning dataset. Do not replace failed model answers or drop hard cases.',
        'decoding': 'Pinned BF16/SDPA worker, greedy, thinking disabled, no quantization, batch size 1.',
        'compute_bound': {'colab_attempts': 1, 'colab_gpu': 'L4', 'colab_controller_seconds': 3900,
                          'local_fallback_attempts': 1, 'local_fallback_seconds': 16200, 'local_gpu_weight_limit_gib': 11},
        'compute_fallback': 'Local fallback allowed only when Colab produced zero worker outputs; otherwise preserve and review the partial run before any new collection.',
        'labels': 'Four-way task effects and input-grounded outcomes derived separately from freshly executed no-memory/candidate pairs. No relation-to-utility substitution.',
        'folds': 'Same three exploratory family folds; derivatives remain with their parent family. No held-out source promotion.',
        'scope': 'Controlled snapshots from prior 12 development families; no new progress/failure histories or new past-record generation.',
        'local_sha256': {p.name: digest(p.read_bytes()) for p in sorted(local.iterdir())},
        'contrast_protocol_sha256': built['contrast_protocol_sha256'],
        'frozen_before_worker_calls': True})
    write(data / 'manifest.json', {'export_sha256': {n: digest((data / n).read_bytes()) for n in ('pending-jobs.json', 'repeat-jobs.json', 'protocol.json')}})
    sources = [Path(__file__), CONFIG, ROOT / 'data_pipeline/prepare_memory_contrasts.py',
        ROOT / 'training/memory_selector/travel_state_protocol.py', BASE / 'manifest.json', EVIDENCE / 'manifest.json']
    write(output / 'manifest.json', {'files_sha256': {str(p.relative_to(output)): digest(p.read_bytes()) for p in sorted(output.rglob('*')) if p.is_file()},
        'source_sha256': {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in sources}})
    return validate(output)


def validate(path):
    manifest = json.loads((path / 'manifest.json').read_text())
    for name, sha in manifest['files_sha256'].items():
        if digest((path / name).read_bytes()) != sha:
            raise ValueError('Changed refresh design: ' + name)
    for name, sha in manifest['source_sha256'].items():
        if digest((ROOT / name).read_bytes()) != sha:
            raise ValueError('Changed refresh source: ' + name)
    rebuilt = reconstruct()
    if readl(path / 'local/actions.jsonl') != rebuilt['actions']:
        raise ValueError('Action design differs from source reconstruction')
    for name, expected in [('data/pending-jobs.json', rebuilt['jobs']), ('data/repeat-jobs.json', rebuilt['repeats']),
                           ('local/repeat-design.json', rebuilt['repeat_design']), ('local/record-reviews.json', rebuilt['reviews']),
                           ('local/excluded-cases.json', rebuilt['excluded'])]:
        if json.loads((path / name).read_text()) != expected:
            raise ValueError('Refresh input differs from source reconstruction: ' + name)
    data_manifest = json.loads((path / 'data/manifest.json').read_text())
    if set(data_manifest['export_sha256']) != {'pending-jobs.json', 'repeat-jobs.json', 'protocol.json'}:
        raise ValueError('Unexpected worker upload file')
    for name, sha in data_manifest['export_sha256'].items():
        if digest((path / 'data' / name).read_bytes()) != sha:
            raise ValueError('Changed worker upload input')
    for job in rebuilt['jobs'] + rebuilt['repeats']:
        if set(job) != {'generation_id', 'messages'} or signature(job['messages']) != job['generation_id']:
            raise ValueError('Unexpected worker input fields')
        if [m['role'] for m in job['messages']] != ['system', 'user'] or any(set(m) != {'role', 'content'} for m in job['messages']):
            raise ValueError('Unexpected worker message fields')
    folds = family_folds([r['family_id'] for r in rebuilt['actions']], 3)
    if json.loads((path / 'local/family-folds.json').read_text()) != folds:
        raise ValueError('Family split changed')
    return {'actions': 504, 'states': 96, 'families': 12, 'unique_inputs': 480,
        'nonempty_candidates': 408, 'new_candidate_actions': 171, 'fresh_calls_including_repeats': 484,
        'old_outputs_reused': 0, 'new_independent_families': 0}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['prepare', 'check'])
    p.add_argument('--path', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.path) if a.command == 'prepare' else validate(a.path), indent=2))

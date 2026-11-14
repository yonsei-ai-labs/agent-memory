"""Add repeated and length-matched distractor records to frozen external update cases.

No model calls. Source texts, case IDs and source splits are preserved. Control
matching uses record counts and token lengths only, before observing any outputs.
"""
import argparse
import collections
import hashlib
import itertools
import json
from pathlib import Path

from data_pipeline import prepare_external_updates as original
from training.memory_selector.external_updates_protocol import CONTROL_CONFIG, CONTROL_SYSTEM

SCHEMA = 'external-update-controls-v2'
CONDITIONS = ('none', 'new_only', 'old_only', 'both', 'current_new', 'current_new_plus_old',
              'current_new_plus_repeat', 'current_new_plus_distractor')
MEMORY_TOKENS = 1024
MAX_RECORDS = 8


def readl(path):
    return original._readl(path)


def dump(path, value):
    original._write(path, value)


def writel(path, rows):
    original._write_rows(path, rows)


def sha(path):
    return original._file_sha(path)


def memory_text(case, ids):
    selected = sorted((r for r in case['records'] if r['record_id'] in ids), key=lambda r: r['order'])
    return '\n'.join(f"[{n}] {r['time'] or 'date unknown'}, {r['speaker']}: {r['text']}"
                     for n, r in enumerate(selected, 1))


def make_control(case, annotation, count_tokens, override=None):
    old, new = annotation['old_record_ids'], annotation['new_record_ids']
    old_length = count_tokens(memory_text(case, old))
    repeat_length = count_tokens(memory_text(case, new))
    records = {r['record_id']: r for r in case['records']}
    cutoff = min(records[r]['order'] for r in new)
    eligible = [r for r in annotation['distractor_record_ids'] if records[r]['order'] < cutoff]
    if override is not None:
        if not set(override['allowed_distractor_record_ids']) <= set(eligible) or not override.get('rationale'):
            raise ValueError('Invalid content-based control override')
        eligible = override['allowed_distractor_record_ids']
    choices = list(itertools.combinations(eligible, len(old)))
    distractor = min(choices, key=lambda ids: (abs(count_tokens(memory_text(case, ids)) - old_length), tuple(ids))) if choices else ()
    distractor_length = count_tokens(memory_text(case, distractor)) if choices else None
    tolerance = max(16, .25 * old_length)
    count_matched = len(new) == len(old) == len(distractor)
    length_matched = bool(choices) and abs(repeat_length - old_length) <= tolerance and abs(distractor_length - old_length) <= tolerance
    return {'case_id': case['case_id'], 'distractor_record_ids': list(distractor),
            'old_records': len(old), 'repeat_records': len(new), 'distractor_records': len(distractor),
            'old_tokens': old_length, 'repeat_tokens': repeat_length, 'distractor_tokens': distractor_length,
            'count_matched': count_matched, 'length_matched': length_matched,
            'distractor_available': bool(choices),
            'matched_controls': count_matched and length_matched,
            'length_tolerance_tokens': tolerance,
            'matching_note': 'Closest original distractor bundle by token length; no worker outputs used.'}


def prepare(source, output, tokenizer, overrides_path=None):
    source, output = Path(source), Path(output)
    if output.exists():
        raise FileExistsError(output)
    manifest = json.loads((source / 'manifest.json').read_text())
    for name, h in manifest['sha256'].items():
        if sha(source / name) != h:
            raise ValueError('Changed source: ' + name)
    count_tokens = original.token_counter(tokenizer)
    source_provenance = json.loads((source / 'source-provenance.json').read_text())
    if sha(tokenizer) != source_provenance['tokenizer']['sha256']:
        raise ValueError('Control lengths must use the original worker tokenizer')
    cases = readl(source / 'cases.jsonl')
    annotations = {a['case_id']: a for a in readl(source / 'annotations.jsonl')}
    overrides = {r['case_id']: r for r in readl(overrides_path)} if overrides_path else {}
    if not set(overrides) <= set(annotations):
        raise ValueError('Unknown case in control overrides')
    controls, decisions, jobs = [], [], {}
    for case in cases:
        label = annotations[case['case_id']]
        control = make_control(case, label, count_tokens, overrides.get(case['case_id']))
        controls.append(control)
        bundles = original.condition_bundles(case, label)
        bundles.update(current_new_plus_repeat={'current': label['new_record_ids'], 'memory': label['new_record_ids']},
                       current_new_plus_distractor={'current': label['new_record_ids'], 'memory': control['distractor_record_ids']})
        for condition in CONDITIONS:
            if condition == 'current_new_plus_distractor' and not control['distractor_available']:
                continue
            bundle = bundles[condition]
            added_tokens = count_tokens(memory_text(case, bundle['memory']))
            if len(bundle['memory']) > MAX_RECORDS or added_tokens > MEMORY_TOKENS:
                raise ValueError('Memory budget exceeded; do not truncate evidence')
            messages = original.render_messages(case, bundle['current'], bundle['memory'])
            messages[0]['content'] = CONTROL_SYSTEM
            job_id = original.signature(messages)
            jobs[job_id] = {'job_id': job_id, 'messages': messages}
            decisions.append({'case_id': case['case_id'], 'condition': condition,
                              'current_record_ids': bundle['current'], 'memory_record_ids': bundle['memory'],
                              'memory_tokens': added_tokens, 'job_id': job_id})
    output.mkdir(parents=True)
    for filename in ('cases.jsonl', 'annotations.jsonl', 'source-provenance.json'):
        (output / filename).write_bytes((source / filename).read_bytes())
    writel(output / 'controls.jsonl', controls)
    writel(output / 'control-overrides.jsonl', list(overrides.values()))
    writel(output / 'decisions.jsonl', decisions)
    writel(output / 'jobs.jsonl', [jobs[k] for k in sorted(jobs)])
    group = lambda c: c['source'] + '/' + c['split']
    control_by_id = {c['case_id']: c for c in controls}
    groups = sorted({group(c) for c in cases})
    summary = {}
    for g in groups:
        subset = [c for c in cases if group(c) == g]
        numeric = [c for c in subset if {k['kind'] for k in annotations[c['case_id']]['accept']} == {'number'}]
        matched = [c for c in numeric if control_by_id[c['case_id']]['matched_controls']]
        summary[g] = {'cases': len(subset), 'numeric': len(numeric), 'numeric_matched': len(matched),
                      'numeric_matched_families': len({c['family_id'] for c in matched})}
    dump(output / 'protocol.json', {'schema': SCHEMA, 'source_manifest_sha256': sha(source / 'manifest.json'),
         'conditions': list(CONDITIONS), 'max_memory_records': MAX_RECORDS, 'max_memory_tokens': MEMORY_TOKENS,
         'system_prompt_sha256': hashlib.sha256(CONTROL_SYSTEM.encode()).hexdigest(),
         'matching': 'equal record counts; both control lengths within max(16, 0.25 * old tokens) of old bundle',
         'distractor_time': 'strictly before every new-evidence record; absent condition if no equal-count bundle',
         'roles_are_not_effect_labels': True, 'source_texts_unchanged': True,
         'grading': 'external_update_validation; one asserted quantity or explicit text-hashed review',
         'primary_cases': 'numeric references; matched controls reported separately; content review required before export'})
    filenames = sorted(p.name for p in output.iterdir() if p.is_file())
    dump(output / 'manifest.json', {'schema': SCHEMA, 'sha256': {n: sha(output / n) for n in filenames},
                                    'summary': summary, 'distinct_jobs': len(jobs), 'decisions': len(decisions)})
    return summary


def export(data, output, content_reviews, worker_provenance, phase):
    data, output = Path(data), Path(output)
    if output.exists():
        raise FileExistsError(output)
    manifest = json.loads((data / 'manifest.json').read_text())
    for filename, h in manifest['sha256'].items():
        if sha(data / filename) != h:
            raise ValueError('Changed dataset: ' + filename)
    cases = {c['case_id']: c for c in readl(data / 'cases.jsonl')}
    labels = {a['case_id']: a for a in readl(data / 'annotations.jsonl')}
    controls = {c['case_id']: c for c in readl(data / 'controls.jsonl')}
    reviews = {r['case_id']: r for r in readl(content_reviews)}
    wanted_cases, excluded = set(), []
    for case_id, c in cases.items():
        a = labels[case_id]
        if {k['kind'] for k in a['accept']} != {'number'}:
            continue
        eligible = ((c['split'] == 'design' or (c['source'] == 'beam' and c['split'] == 'dev' and a['stale'] is not None))
                    if phase == 'design' else c['source'] == 'beam' and c['split'] == 'confirm')
        if not eligible:
            continue
        review = reviews.get(case_id)
        if (not review or review.get('distractor_record_ids') != controls[case_id]['distractor_record_ids']
                or not review.get('rationale') or not review.get('reviewer')
                or review.get('dataset_manifest_sha256') != sha(data / 'manifest.json')):
            raise ValueError('Missing or stale content review: ' + case_id)
        if not review['label_valid']:
            excluded.append({'case_id': case_id, 'reason': review['rationale']})
            continue
        if controls[case_id]['distractor_available'] and not review['distractor_valid']:
            raise ValueError('Control content needs resolution before execution: ' + case_id)
        wanted_cases.add(case_id)
    chosen = [d for d in readl(data / 'decisions.jsonl') if d['case_id'] in wanted_cases]
    wanted_jobs = {d['job_id'] for d in chosen}
    jobs = [j for j in readl(data / 'jobs.jsonl') if j['job_id'] in wanted_jobs]
    if not 2 <= len(jobs) <= 600:
        raise ValueError('Unexpected bounded batch size')
    output.mkdir(parents=True)
    writel(output / 'inputs.jsonl', jobs)
    dump(output / 'config.json', {**CONTROL_CONFIG, 'main_jobs': len(jobs)})
    dump(output / 'worker-provenance.json', worker_provenance)
    dump(output / 'input-manifest.json', {'sha256': {n: sha(output / n) for n in ('inputs.jsonl', 'config.json', 'worker-provenance.json')}})
    selection = {'phase': phase, 'dataset_manifest_sha256': sha(data / 'manifest.json'),
                 'content_reviews_sha256': sha(content_reviews), 'case_ids': sorted(wanted_cases),
                 'excluded': excluded, 'decisions': len(chosen), 'unique_jobs': len(jobs), 'labels_uploaded': False}
    dump(output / 'selection.json', selection)
    return selection


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sp = p.add_subparsers(dest='command', required=True)
    prepare_p = sp.add_parser('prepare')
    prepare_p.add_argument('--source', type=Path, required=True)
    prepare_p.add_argument('--tokenizer', type=Path, required=True)
    prepare_p.add_argument('--output', type=Path, required=True)
    prepare_p.add_argument('--overrides', type=Path)
    export_p = sp.add_parser('export')
    for name in ('data', 'output', 'content-reviews', 'worker-provenance'):
        export_p.add_argument('--' + name, type=Path, required=True)
    export_p.add_argument('--phase', choices=('design', 'confirm'), required=True)
    a = p.parse_args()
    if a.command == 'prepare':
        result = prepare(a.source, a.output, a.tokenizer, a.overrides)
    else:
        worker = json.loads(a.worker_provenance.read_text())
        result = export(a.data, a.output, a.content_reviews, worker.get('worker', worker), a.phase)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

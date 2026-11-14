"""Preserve historical context while masking reviewed quantity spans, without model calls.

The complete v2 design set is retained. Marked and masked prompts have equal
worker-tokenizer lengths. Two enumerations require broader edits and are flagged.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from data_pipeline.prepare_external_update_controls import dump, memory_text, readl, sha, writel
from data_pipeline.prepare_external_updates import render_messages, signature, token_counter
from training.memory_selector.external_updates_protocol import CONTROL_CONFIG, CONTROL_SYSTEM, validate_inputs

CONDITIONS = ('current_new', 'current_new_plus_old', 'old_values_marked', 'old_values_masked')


def apply_spans(text, spans, replacements):
    previous_end = 0
    pieces = []
    if len(spans) != len(replacements):
        raise ValueError('Wrong replacement count')
    for span, replacement in zip(spans, replacements):
        start, end = span['start'], span['end']
        if not 0 <= previous_end <= start < end <= len(text) or text[start:end] != span['quote']:
            raise ValueError('Overlapping, out-of-range or changed source span')
        pieces.extend((text[previous_end:start], replacement))
        previous_end = end
    return ''.join(pieces) + text[previous_end:]


def mark(value):
    return '[[value: ' + value + ']]'


def edit_record(record, spans, count):
    """Match each replacement in its real text context, then verify the full record."""
    marked = apply_spans(record['text'], spans, [mark(s['quote']) for s in spans])
    replacements = [mark(s['quote']) for s in spans]
    for i in range(len(spans)):
        before = apply_spans(record['text'], spans, replacements)
        for width in range(1, 129):
            candidate = replacements[:]
            candidate[i] = mark(' '.join(['?'] * width))
            after = apply_spans(record['text'], spans, candidate)
            if count(before) == count(after):
                replacements = candidate
                break
        else:
            raise ValueError('No exact token-matched mask for ' + record['record_id'])
    masked = apply_spans(record['text'], spans, replacements)
    if count(marked) != count(masked):
        raise ValueError('Changed total record length')
    return marked, masked, replacements


def prepare(source, selection, reviews, tokenizer, output):
    source, output = Path(source), Path(output)
    if output.exists():
        raise FileExistsError(output)
    manifest = json.loads((source / 'manifest.json').read_text())
    for filename, expected in manifest['sha256'].items():
        if sha(source / filename) != expected:
            raise ValueError('Changed source: ' + filename)
    provenance = json.loads((source / 'source-provenance.json').read_text())
    if sha(tokenizer) != provenance['tokenizer']['sha256']:
        raise ValueError('Use the frozen original worker tokenizer')
    chosen = json.loads(Path(selection).read_text())
    if chosen['phase'] != 'design' or chosen['dataset_manifest_sha256'] != sha(source / 'manifest.json'):
        raise ValueError('Only the previously observed design selection is authorized here')
    ids = chosen['case_ids']
    cases = {c['case_id']: c for c in readl(source / 'cases.jsonl')}
    labels = {a['case_id']: a for a in readl(source / 'annotations.jsonl')}
    review_rows = readl(reviews)
    checked = {r['case_id']: r for r in review_rows}
    if len(checked) != len(review_rows) or set(checked) != set(ids):
        raise ValueError('Every design case must have exactly one span review')
    count = token_counter(tokenizer)
    jobs, decisions, transformations = {}, [], []
    for case_id in ids:
        case, label, review = cases[case_id], labels[case_id], checked[case_id]
        if not review.get('reviewer') or not review.get('rationale'):
            raise ValueError('Missing content review')
        if review['source_manifest_sha256'] != sha(source / 'manifest.json'):
            raise ValueError('Stale span review')
        if review['edit_scope'] not in ('quantity_only', 'quantity_and_enumeration'):
            raise ValueError('Unknown edit scope')
        old_ids, new_ids = label['old_record_ids'], label['new_record_ids']
        if not old_ids or set(old_ids) & set(new_ids):
            raise ValueError('Expected distinct current and historical evidence')
        spans_by_record = {}
        for span in review['spans']:
            if span['record_id'] not in old_ids or not span.get('role'):
                raise ValueError('A mask may only edit reviewed historical evidence')
            spans_by_record.setdefault(span['record_id'], []).append(span)
        if not spans_by_record:
            raise ValueError('An empty mask is not a value-removal contrast')
        marked_case, masked_case = copy.deepcopy(case), copy.deepcopy(case)
        for original, marked_r, masked_r in zip(case['records'], marked_case['records'], masked_case['records']):
            spans = spans_by_record.get(original['record_id'], [])
            if not spans:
                continue
            if any(s['record_sha256'] != hashlib.sha256(original['text'].encode()).hexdigest() for s in spans):
                raise ValueError('Source record hash changed')
            spans = sorted(spans, key=lambda s: s['start'])
            marked_r['text'], masked_r['text'], replacements = edit_record(original, spans, count)
            transformations.append({'case_id': case_id, 'record_id': original['record_id'],
                'original': original['text'], 'marked': marked_r['text'], 'masked': masked_r['text'],
                'spans': spans, 'replacements': replacements,
                'marked_tokens': count(marked_r['text']), 'masked_tokens': count(masked_r['text'])})
        lengths = {}
        for condition in CONDITIONS:
            variant = marked_case if condition == 'old_values_marked' else masked_case if condition == 'old_values_masked' else case
            memory = [] if condition == 'current_new' else old_ids
            tokens = count(memory_text(variant, memory))
            if len(memory) > 8 or tokens > 1024:
                raise ValueError('Memory budget exceeded; do not truncate')
            messages = render_messages(variant, new_ids, memory)
            messages[0]['content'] = CONTROL_SYSTEM
            job_id = signature(messages)
            jobs[job_id] = {'job_id': job_id, 'messages': messages}
            lengths[condition] = count(messages[1]['content'])
            decisions.append({'case_id': case_id, 'condition': condition, 'job_id': job_id,
                'current_record_ids': new_ids, 'memory_record_ids': memory, 'memory_tokens': tokens,
                'edit_scope': review['edit_scope']})
        if lengths['old_values_marked'] != lengths['old_values_masked']:
            raise ValueError('Full marked/masked prompt lengths differ')
    output.mkdir(parents=True)
    writel(output / 'cases.jsonl', [cases[i] for i in ids])
    writel(output / 'annotations.jsonl', [labels[i] for i in ids])
    writel(output / 'decisions.jsonl', decisions)
    writel(output / 'jobs.jsonl', [jobs[i] for i in sorted(jobs)])
    writel(output / 'transformations.jsonl', transformations)
    (output / 'span-reviews.jsonl').write_bytes(Path(reviews).read_bytes())
    dump(output / 'protocol.json', {'schema': 'external-update-masking-v1', 'source_manifest_sha256': sha(source / 'manifest.json'),
        'selection_sha256': sha(selection), 'tokenizer_sha256': sha(tokenizer), 'conditions': list(CONDITIONS),
        'primary': 'old_values_marked versus old_values_masked within each source; quantity_only subset',
        'secondary': 'all cases; original versus marked; current_new versus masked',
        'confirmation_executed': False, 'labels_uploaded': False, 'no_new_facts_inserted': True,
        'max_memory_records': 8, 'max_memory_tokens': 1024})
    dump(output / 'manifest.json', {'sha256': {p.name: sha(p) for p in sorted(output.iterdir())},
         'cases': len(ids), 'decisions': len(decisions), 'jobs': len(jobs), 'modified_records': len(transformations)})
    return json.loads((output / 'manifest.json').read_text())


def export(data, previous_inputs, previous_predictions, output):
    data, previous_inputs, output = Path(data), Path(previous_inputs), Path(output)
    if output.exists():
        raise FileExistsError(output)
    manifest = json.loads((data / 'manifest.json').read_text())
    for name, h in manifest['sha256'].items():
        if sha(data / name) != h:
            raise ValueError('Changed masking data')
    previous = {j['job_id']: j for j in validate_inputs(previous_inputs)}
    predictions = {p['job_id']: p for p in readl(previous_predictions)}
    cache_ids = {d['job_id'] for d in readl(data / 'decisions.jsonl') if d['condition'] in CONDITIONS[:2]}
    jobs = readl(data / 'jobs.jsonl')
    for job in jobs:
        if job['job_id'] in cache_ids and (previous.get(job['job_id']) != job or job['job_id'] not in predictions):
            raise ValueError('Cached comparison must have byte-equivalent messages and an actual output')
    fresh = [j for j in jobs if j['job_id'] not in cache_ids]
    if not fresh or len(fresh) != 2 * manifest['cases']:
        raise ValueError('Unexpected new worker input count')
    output.mkdir(parents=True)
    writel(output / 'inputs.jsonl', fresh)
    dump(output / 'config.json', {**CONTROL_CONFIG, 'main_jobs': len(fresh)})
    (output / 'worker-provenance.json').write_bytes((previous_inputs / 'worker-provenance.json').read_bytes())
    dump(output / 'input-manifest.json', {'sha256': {n: sha(output / n) for n in ('inputs.jsonl', 'config.json', 'worker-provenance.json')}})
    dump(output / 'selection.json', {'phase': 'design', 'data_manifest_sha256': sha(data / 'manifest.json'),
        'fresh_jobs': len(fresh), 'cached_job_ids': sorted(cache_ids), 'previous_inputs_manifest_sha256': sha(previous_inputs / 'input-manifest.json'),
        'previous_predictions_sha256': sha(previous_predictions), 'worker_protocol_reused': 'external-memory-updates-20261111-v2'})
    validate_inputs(output)
    return {'fresh_jobs': len(fresh), 'cached_jobs': len(cache_ids)}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sp = p.add_subparsers(dest='mode', required=True)
    first = sp.add_parser('prepare')
    for name in ('source', 'selection', 'reviews', 'tokenizer', 'output'):
        first.add_argument('--' + name, type=Path, required=True)
    second = sp.add_parser('export')
    for name in ('data', 'previous-inputs', 'previous-predictions', 'output'):
        second.add_argument('--' + name, type=Path, required=True)
    args = vars(p.parse_args()); mode = args.pop('mode')
    print(json.dumps((prepare if mode == 'prepare' else export)(**args), indent=2))


if __name__ == '__main__':
    main()

"""Freeze near-reference bundle contrasts from real archived records, before outcomes."""
import argparse
from collections import Counter
import itertools
import json
from pathlib import Path
import zipfile

from data_pipeline.memory_decision_dataset import ROOT, SOURCE, digest, write, write_rows, readl, signature
from training.memory_selector.travel_state_protocol import state, current_messages, reference_records


CONDITIONS = ('needed', 'visible', 'changed')


def source_data():
    manifest = json.loads((SOURCE / 'shared-manifest.json').read_text())
    archive = SOURCE / 'predictions.zip'
    if digest(archive.read_bytes()) != manifest['archive_sha256']:
        raise ValueError('Original worker archive changed')
    with zipfile.ZipFile(archive) as z:
        load = lambda n: json.loads(z.read(n))
        local = {name: load('design/local/' + name) for name in ('cases.json', 'catalog.json', 'generated-records.json')}
        cache = {}
        for name in ('design/local/cached-predictions.jsonl', 'run/artifacts/predictions.jsonl'):
            for line in z.read(name).decode().splitlines():
                row = json.loads(line)
                if signature(row['messages']) != row['generation_id']:
                    raise ValueError('Source prompt identity mismatch')
                if row['generation_id'] in cache and cache[row['generation_id']]['output_token_ids'] != row['output_token_ids']:
                    raise ValueError('Source prediction disagreement')
                cache[row['generation_id']] = row
        tokenizer = z.read('run/artifacts/worker-tokenizer/tokenizer.json')
        provenance = load('run/artifacts/worker-provenance.json')
    return local, cache, tokenizer, provenance, manifest['archive_sha256']


def choose_near(case, ref, records, token_length):
    """Change one identity relation using a real past record; do not inspect outcomes."""
    wanted = 'day' if int(digest((case['case_id'] + ref['slot']).encode()), 16) % 2 else 'person'
    candidates = []
    for meta in case['memories']:
        m = records[meta['memory_id']]
        if m['choice'] is None or m['slot'] != ref['slot'] or m['choice'] == ref['choice']:
            continue
        person_changed, day_changed = m['person'] != ref['person'], m['day'] != ref['day']
        if person_changed == day_changed:  # exactly one identity field differs
            continue
        if case['mode'] == 'price' and m['past_price'] == ref['past_price']:
            continue
        changed = 'person' if person_changed else 'day'
        gap = abs(token_length(m['evidence']) - token_length(ref['evidence']))
        candidates.append((gap > 8, changed != wanted, gap, digest((case['case_id'] + m['memory_id']).encode()), m, changed))
    if not candidates:
        return None
    _, _, gap, _, chosen, changed = min(candidates, key=lambda x: x[:4])
    return {'memory_id': chosen['memory_id'], 'changed_field': changed, 'record_token_gap': gap,
            'length_matched_within_8_tokens': gap <= 8, 'desired_changed_field': wanted,
            'reference_memory_id': ref['memory_id'], 'eligible_near_records': len(candidates)}


def prepare(output):
    from tokenizers import Tokenizer
    if output.exists():
        raise FileExistsError(output)
    local, cache, tokenizer_bytes, provenance, source_sha = source_data()
    tokenizer = Tokenizer.from_str(tokenizer_bytes.decode())
    length = lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)
    cases, catalog, records = local['cases.json'], local['catalog.json'], local['generated-records.json']
    jobs, reviews, excluded = [], [], []
    for case in sorted(cases, key=lambda c: c['case_id']):
        if case['split'] != 'train':
            raise ValueError('Reserved family entered augmentation')
        bank = [records[m['memory_id']] for m in case['memories']]
        refs = reference_records(case['question'], bank)
        if len(refs) != 2 or [r['slot'] for r in refs] != ['dinner', 'accommodation']:
            excluded.append({'case_id': case['case_id'], 'reason': 'two valid unique reference records unavailable'})
            continue
        near = [choose_near(case, ref, records, length) for ref in refs]
        if any(r is None for r in near):
            excluded.append({'case_id': case['case_id'], 'reason': 'no real single-field near record with different relevant value'})
            continue
        if any(not n['length_matched_within_8_tokens'] for n in near):
            excluded.append({'case_id': case['case_id'], 'reason': 'near record exceeds prespecified 8-token length gap', 'candidates': near})
            continue
        for ref, n in zip(refs, near):
            wrong = records[n['memory_id']]
            reviews.append({'case_id': case['case_id'], 'family_id': case['family_id'], 'goal': case['question'],
                'slot': ref['slot'], **n, 'reference_quote': ref['evidence'], 'near_quote': wrong['evidence'],
                'reference_source': {'person': ref['person'], 'day': ref['day'], 'choice': ref['choice'], 'past_price': ref['past_price']},
                'near_source': {'person': wrong['person'], 'day': wrong['day'], 'choice': wrong['choice'], 'past_price': wrong['past_price']},
                'relation': 'near record concerns a different person or day; no utility sign assigned',
                'reference_was_successful_experience': None})
        for condition in CONDITIONS:
            current = state(case, condition, records, catalog)
            bank_ids = {m['memory_id'] for m in current['bank']}
            for bits in itertools.product((0, 1), repeat=2):
                mids = [refs[i]['memory_id'] if bits[i] == 0 else near[i]['memory_id'] for i in range(2)]
                # Order is shared across all four cells, not based on support label.
                mids.sort(key=lambda mid: ('dinner', 'accommodation').index(records[mid]['slot']))
                if not set(mids) <= bank_ids:
                    raise ValueError('Unavailable record')
                selected = [records[mid] for mid in mids]
                ms = current_messages(current['public_text'], selected)
                if length('\n'.join(m['evidence'] for m in selected)) > 512:
                    raise ValueError('Bundle exceeds fixed delivery cap')
                sid = case['case_id'] + '--' + condition
                jobs.append({'job_id': sid + '--contrast-' + ''.join(map(str, bits)), 'state_id': sid,
                    'case_id': case['case_id'], 'family_id': case['family_id'], 'condition': condition,
                    'selected_memory_ids': mids, 'messages': ms, 'generation_id': signature(ms),
                    'relation_bits': list(bits), 'delivered_memory_tokens': length('\n'.join(m['evidence'] for m in selected)),
                    'source_family_is_new': False, 'outcome_used_for_construction': False})
    if not jobs:
        raise ValueError('No eligible complete contrast groups')
    # Repeat fixed old messages to check compatibility before merging old/new runs.
    first = sorted(cases, key=lambda c: c['case_id'])[0]
    repeats = []
    for condition, which in (('needed', 'none'), ('visible', 'refs'), ('changed', 'refs'), ('missing', 'none')):
        current = state(first, condition, records, catalog)
        selected = reference_records(first['question'], current['bank']) if which == 'refs' else []
        ms = current_messages(current['public_text'], selected)
        sig = signature(ms)
        if sig not in cache:
            raise ValueError('Planned repeat is not in original cache')
        repeats.append({'generation_id': sig, 'messages': ms})
    pending = {j['generation_id']: {k: j[k] for k in ('generation_id', 'messages')} for j in jobs if j['generation_id'] not in cache}
    reused = {j['generation_id'] for j in jobs if j['generation_id'] in cache} | {r['generation_id'] for r in repeats}
    output.mkdir(parents=True)
    data, local_path = output / 'data', output / 'local'
    data.mkdir(); local_path.mkdir()
    for name, value in local.items():
        write(local_path / name, value)
    write(local_path / 'all-jobs.json', jobs)
    write(local_path / 'record-reviews.json', reviews)
    write(local_path / 'excluded-cases.json', excluded)
    write_rows(local_path / 'cached-predictions.jsonl', [cache[sig] for sig in sorted(reused)])
    write(data / 'pending-jobs.json', list(pending.values()))
    write(data / 'repeat-jobs.json', repeats)
    protocol = {'experiment': 'memory-contrast-loss-20261110-v1', 'source_archive_sha256': source_sha,
        'frozen_before_worker_calls': True, 'pending_unique_prompts': len(pending),
        'reused_unique_prompts': len(reused), 'planned_decisions': len(jobs),
        'eligible_cases': len({j['case_id'] for j in jobs}), 'eligible_families': len({j['family_id'] for j in jobs}),
        'conditions': list(CONDITIONS), 'bundle_size': 2, 'memory_token_cap': 512,
        'record_length_gap_limit': 8, 'maximum_record_length_gap': max(r['record_token_gap'] for r in reviews),
        'construction': 'Four combinations of exact-reference/near-reference dinner and accommodation records, across the same three current states. Whole original records remain unchanged.',
        'new_independent_source_families': 0, 'worker_weight_provenance': provenance,
        'utility_labels': 'Actual current worker results only; relation bits are not utility labels.',
        'scope': 'Exploratory augmentation within existing 12 families; all derivatives stay in the original family fold. Reserved dev/test untouched.',
        'local_sha256': {p.name: digest(p.read_bytes()) for p in sorted(local_path.iterdir())},
        'generator_sha256': digest(Path(__file__).read_bytes())}
    write(data / 'protocol.json', protocol)
    files = ('pending-jobs.json', 'repeat-jobs.json', 'protocol.json')
    write(data / 'manifest.json', {'export_sha256': {n: digest((data / n).read_bytes()) for n in files}})
    return {k: v for k, v in protocol.items() if k not in ('local_sha256', 'worker_weight_provenance')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), indent=2))

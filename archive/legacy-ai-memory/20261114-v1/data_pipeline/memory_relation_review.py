"""Versioned review preparation with visible excerpt scope and no automatic relabeling."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'datasets/development_memory_leads/20261108-v1'
GUIDE = ROOT / 'docs/design-data/memory-data-remediation-20261109-v2/reviewer-guide.md'
CONTEXTS = GUIDE.parent / 'source-issue-contexts.json'
RELATIONS = {'shared_diagnostic_mechanism', 'different_mechanism', 'insufficient_connection'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def readl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def write_rows(path, value):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in value))


def words(value):
    return set(re.findall(r'[a-z][a-z0-9_]{2,}', value.lower()))


def extract_source_contexts(intake, source=SOURCE):
    """Read only the historical issue from hash-verified local raw records."""
    catalog = {r['row_index']: r for r in json.loads((intake / 'catalog.json').read_text())}
    result = {}
    for evidence in json.loads((source / 'source-evidence.json').read_text()):
        entry = catalog[evidence['row_index']]
        raw = Path(entry['path'])
        if sha(raw) != evidence['raw_record_sha256']:
            raise ValueError('Raw historical source changed')
        record = json.loads(raw.read_text())
        candidates = [(i, m['content']) for i, m in enumerate(record['trajectory'])
                      if m.get('role') == 'user' and '<issue_description>' in m.get('content', '')]
        if len(candidates) != 1:
            raise ValueError('Ambiguous historical issue')
        index, text = candidates[0]
        start = text.index('<issue_description>') + len('<issue_description>')
        end = text.index('</issue_description>', start)
        result[evidence['memory_id']] = {
            'raw_record_sha256': sha(raw), 'message_index': index,
            'message_sha256': hashlib.sha256(text.encode()).hexdigest(),
            'start': start, 'end': end, 'issue_excerpt': text[start:end],
            'evidence_kind': 'historical_user_report_not_independent_runtime_verification'}
    return result


def prepare(output, source=SOURCE, guide=GUIDE, contexts=CONTEXTS):
    if output.exists():
        raise FileExistsError(output)
    frozen = json.loads((source / 'artifact-index.json').read_text())['sha256']
    for name, expected in frozen.items():
        if sha(source / name) != expected:
            raise ValueError('Changed frozen source: ' + name)
    inputs = readl(source / 'inputs.jsonl')
    labels = {r['sample_id']: r for r in readl(source / 'supervision.jsonl')}
    queries = {r['query_id']: r for r in json.loads((source / 'review-packet.json').read_text())}
    evidence = {r['memory_id']: r for r in json.loads((source / 'source-evidence.json').read_text())}
    source_contexts = json.loads(contexts.read_text())
    train = [r for r in inputs if r['split'] == 'train']
    positive = [r for r in train if labels[r['sample_id']]['label'] == 0]
    unknown = [r for r in train if labels[r['sample_id']]['label'] == 2]
    negative = [r for r in train if labels[r['sample_id']]['label'] == 1]
    score = {r['sample_id']: len(words(r['input']['state_text']) & words(r['input']['candidate_text'])) /
             max(1, len(words(r['input']['candidate_text']))) for r in negative}
    ranked = sorted(negative, key=lambda r: (-score[r['sample_id']], r['sample_id']))
    close, seen = [], set()
    for row in ranked:
        if row['query_id'] not in seen:
            close.append(row)
            seen.add(row['query_id'])
        if len(close) == 23:
            break
    if len(close) != 23:
        raise ValueError('Unexpected diagnostic coverage')
    diagnostic = positive + unknown + close
    random.Random(731).shuffle(diagnostic)
    chosen_ids = {r['sample_id'] for r in diagnostic}
    remaining = defaultdict(list)
    for row in negative:
        if row['sample_id'] not in chosen_ids:
            remaining[row['query_id']].append(row)
    rng = random.Random(9041)
    controls = [rng.choice(sorted(remaining[q], key=lambda r: r['sample_id'])) for q in sorted(remaining)]
    selected = diagnostic + controls
    reviewer, maintainer = output / 'reviewer', output / 'maintainer'
    reviewer.mkdir(parents=True)
    maintainer.mkdir()
    cards, forms, key, sources, source_keys = [], [], [], {}, {}
    for number, row in enumerate(selected, 1):
        rid = f'review-{number:03d}'
        mid = row['memory_id']
        if mid not in source_keys:
            source_keys[mid] = f'source-{len(source_keys) + 1:02d}'
            context = source_contexts[mid]
            if context['raw_record_sha256'] != evidence[mid]['raw_record_sha256']:
                raise ValueError('Historical issue is from a different record')
            sources[source_keys[mid]] = {'source_key': source_keys[mid], 'source_evidence': evidence[mid],
                                         'historical_issue_context': context}
        query = queries[row['query_id']]
        scope = query['excerpt_provenance']
        if row['input']['state_text'] != 'Current issue excerpt, before agent actions:\n' + query['current_excerpt']:
            raise ValueError('Current excerpt changed')
        if scope['end'] - scope['start'] != len(query['current_excerpt']):
            raise ValueError('Invalid excerpt boundary')
        cards.append({'review_id': rid, 'current_observation': row['input']['state_text'],
                      'candidate_memory': row['input']['candidate_text'], 'source_key': source_keys[mid],
                      'observation_scope': {'stage': 'issue_excerpt_before_target_actions', **scope,
                                            'excerpt_characters': len(query['current_excerpt'])}})
        forms.append({'review_id': rid, 'reviewer_id': None, 'reviewer_type': None, 'original_labels_seen': None,
                      'relation': None, 'source_alignment': None, 'generalization_limit': None,
                      'added_information': None, 'current_evidence_quote': None, 'source_event_indices': [],
                      'bridge': {'past_inspection': None, 'current_inspection': None, 'connecting_observation': None},
                      'reason': None, 'missing_evidence': None, 'status': 'unreviewed'})
        is_control = number > len(diagnostic)
        key.append({'review_id': rid, 'original_sample_id': row['sample_id'], 'original_query_id': row['query_id'],
                    'original_memory_id': mid, 'repository': row['repository'], 'split': row['split'],
                    'original_label': labels[row['sample_id']]['label_name'],
                    'original_rationale': labels[row['sample_id']]['rationale'],
                    'selection_cohort': 'uniform_remaining_negative_within_query' if is_control else 'diagnostic',
                    'remaining_stratum_size': len(remaining[row['query_id']]) if is_control else None,
                    'conditional_selection_probability': 1 / len(remaining[row['query_id']]) if is_control else None})
    write_rows(reviewer / 'cases.jsonl', cards)
    write_rows(reviewer / 'review-form.jsonl', forms)
    write_json(reviewer / 'source-evidence.json', list(sources.values()))
    (reviewer / 'README.md').write_bytes(guide.read_bytes())
    public_manifest = {'schema': 'memory-relation-review-v2', 'guidelines_sha256': sha(guide),
                       'files_sha256': {p.name: sha(p) for p in sorted(reviewer.iterdir()) if p.is_file()},
                       'training_allowed': False, 'supervised_labels_created': 0}
    write_json(reviewer / 'manifest.json', public_manifest)
    write_json(maintainer / 'adjudication-key.json', key)
    summary = {'schema': 'memory-relation-review-v2', 'rows': len(cards), 'diagnostic_rows': len(diagnostic),
               'remaining_negative_control_rows': len(controls), 'current_issues': len({r['query_id'] for r in selected}),
               'memory_sources': len(source_keys), 'split': 'train',
               'clipped_rows': sum(r['observation_scope']['clipped'] for r in cards),
               'old_label_counts': dict(Counter(r['original_label'] for r in key)),
               'cohort_labels_visible_to_reviewer': False, 'independent_reviews_completed': 0,
               'new_source_families': 0, 'new_supervised_labels': 0, 'training_allowed': False,
               'source_files_sha256': {name: sha(source / name) for name in frozen},
               'guidelines_sha256': sha(guide),
               'historical_issue_contexts_sha256': sha(contexts),
               'sampling': 'Diagnostic census of train positives/unknowns plus 23 lexical negatives; separately one uniform residual negative per current query. Not a simple random sample of all pairs.'}
    write_json(maintainer / 'selection.json', summary)
    with zipfile.ZipFile(output / 'reviewer.zip', 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(reviewer.iterdir()):
            info = zipfile.ZipInfo(p.name, date_time=(2026, 11, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    validate_packet(output, source, contexts)
    return summary


def validate_packet(output, source=SOURCE, contexts=CONTEXTS):
    reviewer = output / 'reviewer'
    manifest = json.loads((reviewer / 'manifest.json').read_text())
    if set(manifest['files_sha256']) != {'cases.jsonl', 'review-form.jsonl', 'source-evidence.json', 'README.md'}:
        raise ValueError('Incomplete review manifest')
    for name, expected in manifest['files_sha256'].items():
        if sha(reviewer / name) != expected:
            raise ValueError('Review file changed: ' + name)
    if sha(reviewer / 'README.md') != manifest['guidelines_sha256']:
        raise ValueError('Guideline hash mismatch')
    key = json.loads((output / 'maintainer/adjudication-key.json').read_text())
    originals = {r['sample_id']: r for r in readl(source / 'inputs.jsonl')}
    cards = readl(reviewer / 'cases.jsonl')
    forms = readl(reviewer / 'review-form.jsonl')
    if len(cards) != len(key) or len({r['review_id'] for r in cards}) != len(cards) or len({r['review_id'] for r in key}) != len(key):
        raise ValueError('Duplicate or missing review identity')
    if len(forms) != len(cards) or {r['review_id'] for r in forms} != {r['review_id'] for r in cards}:
        raise ValueError('Form coverage differs')
    for form in forms:
        if (form['status'] != 'unreviewed' or form['source_event_indices'] != [] or
                any(v is not None for k, v in form.items() if k not in ('review_id', 'status', 'source_event_indices', 'bridge')) or
                form['bridge'] != {'past_inspection': None, 'current_inspection': None, 'connecting_observation': None}):
            raise ValueError('Review template contains prior judgments')
    mappings = {r['review_id']: r for r in key}
    queries = {r['query_id']: r for r in json.loads((source / 'review-packet.json').read_text())}
    evidence = {r['memory_id']: r for r in json.loads((source / 'source-evidence.json').read_text())}
    source_contexts = json.loads(contexts.read_text())
    sources = {r['source_key']: r for r in json.loads((reviewer / 'source-evidence.json').read_text())}
    for card in cards:
        row = originals[mappings[card['review_id']]['original_sample_id']]
        if row['split'] != 'train' or card['current_observation'] != row['input']['state_text'] or card['candidate_memory'] != row['input']['candidate_text']:
            raise ValueError('Changed original or nontraining case')
        if set(card) != {'review_id', 'current_observation', 'candidate_memory', 'source_key', 'observation_scope'}:
            raise ValueError('Unapproved reviewer fields')
        query = queries[row['query_id']]
        expected_scope = {'stage': 'issue_excerpt_before_target_actions', **query['excerpt_provenance'],
                          'excerpt_characters': len(query['current_excerpt'])}
        if card['observation_scope'] != expected_scope:
            raise ValueError('Observation scope changed')
        if sources[card['source_key']] != {'source_key': card['source_key'], 'source_evidence': evidence[row['memory_id']],
                                          'historical_issue_context': source_contexts[row['memory_id']]}:
            raise ValueError('Historical evidence differs from source')
    with zipfile.ZipFile(output / 'reviewer.zip') as z:
        if set(z.namelist()) != {p.name for p in reviewer.iterdir()}:
            raise ValueError('Reviewer archive includes unexpected files')
        if any(z.read(p.name) != p.read_bytes() for p in reviewer.iterdir()):
            raise ValueError('Reviewer archive differs from directory')
    return {'rows': len(cards), 'train_only': True, 'exact_input_copies': True, 'instructions_and_archive_verified': True}


def validate_submission(reviewer, submission):
    """Validate review evidence, never turn one review into final supervision."""
    cards = {r['review_id']: r for r in readl(reviewer / 'cases.jsonl')}
    sources = {r['source_key']: r['source_evidence'] for r in json.loads((reviewer / 'source-evidence.json').read_text())}
    rows = readl(submission)
    if len({r['review_id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate submission identity')
    for row in rows:
        card = cards[row['review_id']]
        if row['relation'] not in RELATIONS or row['status'] != 'submitted_provisional':
            raise ValueError('Invalid relation or premature final status')
        if not row['reviewer_id'] or row['reviewer_type'] not in ('human', 'ai') or type(row['original_labels_seen']) is not bool:
            raise ValueError('Reviewer identity/exposure required')
        quote = row['current_evidence_quote']
        if not quote or quote not in card['current_observation']:
            raise ValueError('Current evidence quote not in visible input')
        events = sources[card['source_key']]['events']
        if not row['source_event_indices'] or any(type(i) is not int or not 0 <= i < len(events) for i in row['source_event_indices']):
            raise ValueError('Invalid source event citation')
        if row['relation'] == 'shared_diagnostic_mechanism':
            if set(row['bridge']) != {'past_inspection', 'current_inspection', 'connecting_observation'} or not all(
                    isinstance(v, str) and v.strip() for v in row['bridge'].values()):
                raise ValueError('Positive relation needs concrete bridge')
        if row['relation'] == 'insufficient_connection' and not row['missing_evidence']:
            raise ValueError('Uncertain relation needs missing premise')
        if not row['reason'] or not row['generalization_limit']:
            raise ValueError('Missing reason/generalization boundary')
        if row['source_alignment'] not in ('supported', 'partial', 'contradicted', 'not_established'):
            raise ValueError('Invalid source alignment')
        if row['added_information'] not in ('explicitly_redundant', 'not_explicitly_present', 'unclear'):
            raise ValueError('Invalid information status')
    return {'submitted': len(rows), 'unreviewed': len(cards) - len(rows),
            'labels_seen_by_any_reviewer': any(r['original_labels_seen'] for r in rows),
            'training_allowed': False, 'final_labels_created': 0}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.output), ensure_ascii=False, indent=2))

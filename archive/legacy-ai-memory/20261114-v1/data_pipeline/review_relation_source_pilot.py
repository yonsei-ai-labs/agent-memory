"""Prepare and verify a source-grounded review pilot, without model execution.

This is a partially annotated intake, not a complete retrieval benchmark or an
effect-labelled training set. Unreviewed memories are never made negatives.
"""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import shutil

from data_pipeline.collect_relation_source_pilot import (
    digest, file_sha, groups_for_questions, literal, readl,
    write, writel,
)


def candidates_for_history(history, history_id, source_file):
    """Chunk on observed user turns, independently of any gold annotation."""
    candidates, provenance = [], []
    block = []

    def emit():
        if not block: return
        indices = [i for i, _ in block]
        cid = 'm_' + digest(history_id + ':' + ','.join(map(str, indices)))[:20]
        messages = [{k: t[k] for k in ('role', 'content', 'tool_calls', 'tool_call_id', 'name') if k in t}
                    for _, t in block]
        candidates.append({'candidate_id': cid, 'history_id': 'h_' + digest(history_id)[:16],
                           'turn_start': indices[0], 'messages': messages})
        provenance.append({'candidate_id': cid, 'source_file': source_file,
                           'history_id': history_id, 'turn_indices': indices})
        block.clear()

    for index, turn in enumerate(history):
        # PersonaMem's system message contains the hidden generated persona.
        # It is not an observed user memory and must never enter model input.
        if turn['role'] not in ('user', 'assistant', 'tool'):
            emit(); continue
        if turn['role'] == 'user' or (block and turn.get('source_id') != block[-1][1].get('source_id')):
            emit()
        block.append((index, turn))
    emit()
    return candidates, provenance


def find_anchor(anchor, histories, provenance):
    matches = []
    for key, turns in histories.items():
        for index, turn in enumerate(turns):
            if anchor.get('source_id') and turn.get('source_id') != anchor['source_id']: continue
            if 'turn_index' in anchor and index != anchor['turn_index']: continue
            if anchor.get('role') and turn['role'] != anchor['role']: continue
            text = turn.get('content', '')
            quote = anchor['quote']
            if not isinstance(text, str) or quote not in text: continue
            # Preserve all occurrences, rather than arbitrarily choosing one.
            offset = 0
            while True:
                start = text.find(quote, offset)
                if start < 0: break
                ps = [p for p in provenance if p['history_id'] == key and index in p['turn_indices']]
                if len(ps) != 1: raise ValueError('Anchor is outside observed candidate input')
                matches.append({'candidate_id': ps[0]['candidate_id'], 'source_file': ps[0]['source_file'],
                                'history_id': key, 'turn_index': index, 'start': start,
                                'end': start + len(quote), 'quote': quote, 'role': turn['role']})
                offset = start + len(quote)
    if not matches: raise ValueError(f'Unmatched reviewed quote: {anchor}')
    return matches


def prepare(intake, reviews, output):
    intake, output = Path(intake), Path(output)
    if output.exists(): raise FileExistsError(output)
    selection = readl(intake / 'pilot-selection.jsonl')
    frozen = json.loads((intake / 'selection-freeze.json').read_text())
    for key, name in [('selection_sha256', 'pilot-selection.jsonl'), ('reservation_sha256', 'source-reservation.jsonl')]:
        if file_sha(intake / name) != frozen[key]: raise ValueError('Selection freeze mismatch')
    source_files = json.loads((intake / 'source-files.json').read_text())['files']
    source_files += json.loads((intake / 'history-files.json').read_text())['files']
    for f in source_files:
        if file_sha(intake / 'raw' / f['file']) != f['sha256']: raise ValueError('Raw source changed')
    judgments = readl(reviews)
    if len({r['case_id'] for r in judgments}) != len(judgments): raise ValueError('Duplicate review')
    review_by_id = {r['case_id']: r for r in judgments}
    if set(review_by_id) != {r['case_id'] for r in selection}: raise ValueError('Review coverage mismatch')

    raw = intake / 'raw'
    questions, sessions = readl(raw / 'mem2act-qa.jsonl'), readl(raw / 'mem2act-sessions.jsonl')
    _, _, source_groups = groups_for_questions(questions, sessions, include_index=True)
    reserved = readl(intake / 'source-reservation.jsonl')
    assignment = {r['source_group']: r['split'] for r in reserved}
    output.mkdir(parents=True); (output / 'sources').mkdir()
    for name in ('source-reservation.jsonl', 'selection-summary.json', 'selection-freeze.json'):
        shutil.copy2(intake / name, output / name)
    for name in ('mem2act-README.md', 'persona-README.md', 'persona-columns.md'):
        shutil.copy2(raw / name, output / 'sources' / name)
    # Full QA fields are supervision, stored apart from the model-facing input.
    writel(output / 'upstream-supervision.jsonl', selection)
    writel(output / 'review-decisions.jsonl', judgments)
    write(output / 'upstream-files.json', {'files': source_files, 'model_calls': 0, 'gpu_allocations': 0})
    inputs, annotations, provenance_all = [], [], []
    for item in selection:
        if assignment[item['source_group']] != 'train': raise ValueError('Pilot crossed held-out boundary')
        row = item['source_row']; histories = {}
        if item['source'] == 'personamem_v2':
            name = Path(row['chat_history_32k_link']).name
            shutil.copy2(raw / name, output / 'sources' / name)
            histories[name] = json.loads((raw / name).read_text())['chat_history']
            files = {name: 'sources/' + name}
            query = literal(row['user_query'])['content']
            schema = None
        else:
            # All sessions in the component, not just gold evidence sessions.
            relevant = [s for s in sessions if any(source_groups.get(i) == item['source_group']
                        for i in set(s['original_conversation_ids']) | {t.get('source_id') for t in s['turns']})]
            name = 'mem2act-' + item['source_row']['qa_id'] + '-sessions.jsonl'
            writel(output / 'sources' / name, relevant)
            histories = {s['session_id']: s['turns'] for s in relevant}
            files = {key: 'sources/' + name for key in histories}
            query = row['query']; schema = row['target_tool_schema']
        candidates, ps = [], []
        for key, turns in histories.items():
            cs, origin = candidates_for_history(turns, key, files[key])
            candidates += cs; ps += origin
        # Candidate ordering does not depend on the gold evidence position.
        candidates.sort(key=lambda r: r['candidate_id'])
        inputs.append({'case_id': item['case_id'], 'current_goal': query,
                       'current_observations': [], 'agent_progress': None, 'failure_history': None,
                       'available_tool_schema': schema, 'candidates': candidates})
        provenance_all.append({'case_id': item['case_id'], 'source_group': item['source_group'],
                               'source': item['source'], 'source_histories': files, 'candidate_origins': ps})
        review = review_by_id[item['case_id']]
        anchors = [{'purpose': a['purpose'], 'matches': find_anchor(a, histories, ps)} for a in review['anchors']]
        snippet_matches = []
        if item['source'] == 'personamem_v2':
            h = next(iter(histories.values()))
            for t in literal(row['related_conversation_snippet']):
                snippet_matches.append([i for i, v in enumerate(h) if v.get('role') == t['role'] and v.get('content') == t['content']])
        annotations.append({'case_id': item['case_id'], 'source_group': item['source_group'],
            'source': item['source'], 'split': 'train_pilot', 'review_status': review['review_status'],
            'relation': review['relation'], 'claim_scope': review['claim_scope'], 'notes': review['notes'],
            'issue_tags': review['issue_tags'], 'anchors': anchors, 'upstream_snippet_turn_matches': snippet_matches,
            'annotation_coverage': 'named spans only; all other candidates unlabeled',
            'minimal_sufficient_sets': None, 'utility': None, 'reviewer': 'codex_single_review',
            'independent_review': False, 'training_approved': False})
    writel(output / 'inputs.jsonl', inputs)
    writel(output / 'annotations.jsonl', annotations)
    writel(output / 'provenance.jsonl', provenance_all)
    return check(output, write_reports=True)


def check(output, write_reports=False):
    output = Path(output)
    inputs, ann, provenance = [readl(output / name) for name in ('inputs.jsonl', 'annotations.jsonl', 'provenance.jsonl')]
    reviews = readl(output / 'review-decisions.jsonl')
    selected = readl(output / 'upstream-supervision.jsonl')
    frozen = json.loads((output / 'selection-freeze.json').read_text())
    if file_sha(output / 'upstream-supervision.jsonl') != frozen['selection_sha256']:
        raise ValueError('Upstream selection changed')
    if file_sha(output / 'source-reservation.jsonl') != frozen['reservation_sha256']:
        raise ValueError('Reservation changed')
    ids = [r['case_id'] for r in inputs]
    if len(set(ids)) != len(ids): raise ValueError('Duplicate input')
    for rows in (ann, provenance, selected, reviews):
        if [r['case_id'] for r in rows] != ids: raise ValueError('Case ordering/coverage mismatch')
    assignment = {}
    seen_sources = {}
    reservation = readl(output / 'source-reservation.jsonl')
    for r in reservation:
        if r['source_group'] in assignment: raise ValueError('Duplicate source group')
        assignment[r['source_group']] = r['split']
        for s in r['original_source_ids']:
            key = (r['source'], s)
            if key in seen_sources: raise ValueError('Original source crosses reserved groups')
            seen_sources[key] = r['source_group']
    if len({a['source_group'] for a in ann}) != len(ann): raise ValueError('Pilot repeats source group')
    span_count = candidate_count = excluded_system = 0
    for x, a, p, upstream, review in zip(inputs, ann, provenance, selected, reviews):
        if not (a['source_group'] == p['source_group'] == upstream['source_group']):
            raise ValueError('Source group mismatch')
        if not (a['source'] == p['source'] == upstream['source']): raise ValueError('Source name mismatch')
        if assignment[a['source_group']] != 'train': raise ValueError('Held-out source in reviewed inputs')
        for key in ('review_status', 'relation', 'claim_scope', 'notes', 'issue_tags'):
            if a[key] != review[key]: raise ValueError('Review transformed incorrectly')
        if a['utility'] is not None or a['training_approved'] or a['independent_review']:
            raise ValueError('Unmeasured utility or unapproved training export')
        allowed = {'case_id', 'current_goal', 'current_observations', 'agent_progress', 'failure_history', 'available_tool_schema', 'candidates'}
        if set(x) != allowed: raise ValueError('Unexpected model input field')
        if x['current_observations'] != [] or x['agent_progress'] is not None or x['failure_history'] is not None:
            raise ValueError('Invented current state')
        row = upstream['source_row']
        expected_goal = literal(row['user_query'])['content'] if a['source'] == 'personamem_v2' else row['query']
        if x['current_goal'] != expected_goal: raise ValueError('Changed source query')
        expected_schema = None if a['source'] == 'personamem_v2' else row['target_tool_schema']
        if x['available_tool_schema'] != expected_schema: raise ValueError('Tool schema mismatch')
        histories, files = {}, p['source_histories']
        for key, name in files.items():
            path = output / name
            if path.suffix == '.jsonl':
                for session in readl(path):
                    histories[session['session_id']] = session['turns']
            else:
                histories[key] = json.loads(path.read_text())['chat_history']
        if set(histories) != set(files): raise ValueError('History inventory mismatch')
        cs, origins = [], []
        for key, turns in histories.items():
            one, origin = candidates_for_history(turns, key, files[key]); cs += one; origins += origin
            excluded_system += sum(t['role'] == 'system' for t in turns)
        if sorted(cs, key=lambda r: r['candidate_id']) != x['candidates']: raise ValueError('Model input does not match observed source')
        if sorted(origins, key=lambda r: r['candidate_id']) != sorted(p['candidate_origins'], key=lambda r: r['candidate_id']):
            raise ValueError('Candidate provenance mismatch')
        candidate_count += len(cs)
        expected_anchors = [{'purpose': anchor['purpose'], 'matches': find_anchor(anchor, histories, origins)} for anchor in review['anchors']]
        if expected_anchors != a['anchors']: raise ValueError('Reviewed evidence spans mismatch')
        span_count += sum(len(anchor['matches']) for anchor in a['anchors'])
        if a['source'] == 'personamem_v2':
            h = next(iter(histories.values()))
            expected_snippets = [[i for i, t in enumerate(h) if t.get('role') == s['role'] and t.get('content') == s['content']]
                                 for s in literal(row['related_conversation_snippet'])]
            if expected_snippets != a['upstream_snippet_turn_matches']: raise ValueError('Upstream snippet join mismatch')
    summary = {'cases': len(inputs), 'source_groups': len({a['source_group'] for a in ann}),
        'review_status': dict(Counter(a['review_status'] for a in ann)),
        'status_by_source': {source: dict(Counter(a['review_status'] for a in ann if a['source'] == source)) for source in sorted({a['source'] for a in ann})},
        'usable_relation_facets': dict(Counter(a['relation'] for a in ann if a['review_status'] == 'usable_relation_fragment')),
        'candidate_chunks': candidate_count, 'verified_review_spans': span_count, 'excluded_latent_system_messages': excluded_system,
        'persona_snippet_turns': sum(len(a['upstream_snippet_turn_matches']) for a in ann),
        'persona_snippet_turns_found': sum(bool(m) for a in ann for m in a['upstream_snippet_turn_matches']),
        'source_reservation': dict(Counter(r['split'] for r in reservation)),
        'actual_agent_progress_cases': 0, 'utility_labels': 0, 'independently_reviewed_cases': 0,
        'training_approved_cases': 0, 'model_calls': 0, 'gpu_allocations': 0,
        'scope': 'partially reviewed relation fragments, not a complete relevance/sufficiency/effect benchmark'}
    if write_reports:
        write(output / 'audit.json', summary)
        with (output / 'review-table.csv').open('w', newline='') as f:
            fields = ['case_id', 'source', 'source_group', 'review_status', 'relation', 'claim_scope', 'notes']
            writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
            writer.writerows({k: a[k] for k in fields} for a in ann)
    else:
        if json.loads((output / 'audit.json').read_text()) != summary: raise ValueError('Saved audit mismatch')
        manifest = json.loads((output / 'manifest.json').read_text())
        for item in manifest['files']:
            if file_sha(output / item['path']) != item['sha256']: raise ValueError('Frozen bundle hash mismatch')
    return summary


def freeze(output):
    output = Path(output)
    write(output / 'manifest.json', {'files': [
        {'path': str(p.relative_to(output)), 'sha256': file_sha(p), 'bytes': p.stat().st_size}
        for p in sorted(output.rglob('*')) if p.is_file() and p.name != 'manifest.json'],
        'model_calls': 0, 'gpu_allocations': 0})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'check'))
    parser.add_argument('--intake', type=Path)
    parser.add_argument('--reviews', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.mode == 'prepare':
        if args.intake is None or args.reviews is None: parser.error('prepare needs --intake and --reviews')
        report = prepare(args.intake, args.reviews, args.output); freeze(args.output)
    else: report = check(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))

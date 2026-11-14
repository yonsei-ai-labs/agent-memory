"""Audit frozen semantic-lead data; optionally recheck the public raw records."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from data_pipeline.development_memory_leads import issue_excerpt
from evaluation.verify_development_leads import load_data, sha


def audit(source, intake=None):
    rows, bank = load_data(source)
    queries = json.loads((source / 'review-packet.json').read_text())
    evidence = json.loads((source / 'source-evidence.json').read_text())
    memory = {r['memory_id']: r for r in bank}
    lookup = {r['query_id']: r for r in queries}
    if {r['query_id'] for r in rows} != set(lookup):
        raise ValueError('Review packet does not cover learner rows')
    for row in rows:
        query = lookup[row['query_id']]
        candidate = memory[row['memory_id']]
        if query['instance_id'] == candidate['source_instance']:
            raise ValueError('Same source/current issue')
        if row['input']['candidate_text'] != candidate['text'] or row['input']['state_text'] != 'Current issue excerpt, before agent actions:\n' + query['current_excerpt']:
            raise ValueError('Unreviewed model input')
    counts = {split: Counter(r['memory_id'] for r in rows if r['split'] == split and r['label'] == 0) for split in ('train', 'dev', 'test')}
    result = {
        'status': 'passed', 'pairs': len(rows), 'current_issues': len(queries), 'source_memories': len(bank),
        'current_repositories': len({r['repository'] for r in queries}),
        'clipped_issue_excerpts': sum(r['excerpt_provenance']['clipped'] for r in queries),
        'shared_by_memory': {mid: {split: counts[split][mid] for split in counts} for mid in sorted(memory)},
        'shared_without_training_positive_for_memory': {split: sum(n for mid, n in counts[split].items() if not counts['train'][mid]) for split in ('dev', 'test')},
        'no_shared_queries': {split: sum(not any(r['label'] == 0 for r in rows if r['query_id'] == q['query_id']) for q in queries if q['split'] == split) for split in ('train', 'dev', 'test')},
        'utility_labels': 0, 'independent_annotation': False,
        'raw_source_records_rechecked': False,
    }
    if intake is not None:
        catalog = {r['row_index']: r for r in json.loads((intake / 'catalog.json').read_text())}
        for query in queries:
            raw_path = intake / 'raw' / f'{query["row_index"]}.json'
            if sha(raw_path) != query['raw_record_sha256'] or sha(raw_path) != catalog[query['row_index']]['sha256']:
                raise ValueError('Changed raw target')
            raw = json.loads(raw_path.read_text())
            text, provenance = issue_excerpt(raw)
            if text != query['current_excerpt'] or provenance != query['excerpt_provenance']:
                raise ValueError('Target input is not the recorded pre-action excerpt')
        events_checked = 0
        for source_record in evidence:
            raw_path = intake / 'raw' / f'{source_record["row_index"]}.json'
            if sha(raw_path) != source_record['raw_record_sha256']:
                raise ValueError('Changed raw historical source')
            raw = json.loads(raw_path.read_text())
            for event in source_record['events']:
                if raw['trajectory'][event['message_index']]['tool_calls'] != [event['call']]:
                    raise ValueError('Source action mismatch')
                if raw['trajectory'][event['response_index']] != event['response']:
                    raise ValueError('Source observation mismatch')
                if event['call']['id'] != event['response']['tool_call_id'] or 'has been edited' not in event['response']['content']:
                    raise ValueError('Historical edit has no observed response')
                events_checked += 1
        result.update(raw_source_records_rechecked=True, exact_target_excerpts_checked=len(queries), exact_historical_edit_response_pairs_checked=events_checked)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--intake', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.source, args.intake)
    payload = json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    if args.output:
        if args.output.exists():
            raise FileExistsError(args.output)
        args.output.write_text(payload)
    print(payload)

"""Export reviewed source tool evidence; never execute a trajectory command."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from data_pipeline.collect_memory_evidence_discovery import sha, write


def export(intake, review_path, output):
    review = json.loads(review_path.read_text())
    selection = json.loads((intake / 'selection.json').read_text())
    records = {x['row_index']: x for x in json.loads((intake / 'source-records.json').read_text())}
    if (output / 'source-freeze.json').exists():
        raise FileExistsError('Source memory is already frozen; use a new version')
    cards, frozen = [], []
    for spec in review:
        row = spec['row_index']
        record = records[row]
        raw_path = Path(record['path'])
        if sha(raw_path) != record['sha256']:
            raise ValueError('Source record hash mismatch')
        raw = json.loads(raw_path.read_text())
        task = json.loads((intake / 'source-tasks' / f'{row}.json').read_text())
        actions = []
        for index in spec['calls']:
            message = raw['trajectory'][index]
            for call in message.get('tool_calls') or []:
                matches = [(i, obs) for i, obs in enumerate(raw['trajectory'])
                           if i > index and obs.get('role') == 'tool'
                           and obs.get('tool_call_id') == call['id']]
                if len(matches) != 1:
                    raise ValueError('Ambiguous source action/observation linkage')
                oi, observation = matches[0]
                actions.append({'message_index': index, 'tool_call': call,
                                'observation_index': oi, 'observation': observation})
        if len(actions) != len(spec['calls']):
            raise ValueError('Expected one linked tool call at each reviewed index')
        evidence = {
            'instance_id': raw['instance_id'], 'repo': raw['repo'],
            'trajectory_id': raw['trajectory_id'], 'row_index': row,
            'source_record_sha256': record['sha256'],
            'base_commit': task['base_commit'], 'problem_statement': task['problem_statement'],
            'task_metadata': task, 'actions': actions,
            'source_commands_executed_by_this_review': False,
        }
        path = output / 'evidence' / f'{row}.json'
        write(path, evidence)
        frozen.append({'path': str(path), 'sha256': sha(path)})
        cards.append({**spec, 'source_instance_id': raw['instance_id'], 'repo': raw['repo'],
                      'base_commit': task['base_commit'], 'evidence_file': str(path),
                      'evidence_sha256': sha(path), 'memory_text_status': 'source_only_review_draft',
                      'reviewer': 'single_codex_agent', 'independent_review': False,
                      'observed_utility': None, 'learner_export_allowed': False})
    card_path = output / 'source-cards.jsonl'
    card_path.write_text(''.join(json.dumps(x, ensure_ascii=False, allow_nan=False) + '\n' for x in cards))
    frozen.extend([{'path': str(card_path), 'sha256': sha(card_path)},
                   {'path': str(review_path), 'sha256': sha(review_path)}])
    # This declaration is tied to the staged human review process, not inferred
    # from file access. The intake deliberately keeps target bodies separate.
    write(output / 'source-freeze.json', {
        'created_utc': datetime.now(timezone.utc).isoformat(), 'artifacts': frozen,
        'stage': 'source_cards_frozen_before_target_problem_review',
        'target_bodies_opened_by_reviewer_at_freeze': False,
        'source_records_in_intake': len(records), 'source_cards': len(cards),
        'repository_groups': len({c['repo'] for c in cards}),
        'source_commands_executed': 0, 'utility_labels': 0,
        'intake_selection_sha256': sha(intake / 'selection.json'),
        'plan_sha256': selection['plan_sha256'],
        'exporter_sha256': sha(Path(__file__)),
    })
    write(output / 'intake-ledger.json', {
        'selection': selection, 'intake': json.loads((intake / 'intake.json').read_text()),
        'source_records': list(records.values()),
        'source_review_status': [
            {'row_index': row, 'instance_id': record['instance_id'],
             'status': 'source_card_reviewed' if row in {c['row_index'] for c in cards}
             else 'candidate_inventory_only_not_audited_or_promoted'}
            for row, record in records.items()
        ],
    })
    print(json.dumps({'source_cards': len(cards), 'evidence_calls': sum(len(x['calls']) for x in review),
                      'target_bodies_reviewed': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('intake', 'review', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    export(args.intake, args.review, args.output)

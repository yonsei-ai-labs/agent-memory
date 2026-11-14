"""Export manually reviewed public-log evidence without inventing utility labels.

Raw tool commands remain inert strings. Evidence excerpts retain offsets and
digests into the original linked observation, including clipping information.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(value).hexdigest()


def content(message):
    value = message.get('content') or ''
    return value if isinstance(value, str) else '\n'.join(x.get('text', '') for x in value)


def export(source: Path, annotations: Path, output: Path):
    if output.exists():
        raise FileExistsError(output)
    reviewed = json.loads(annotations.read_text())
    originals = {d['candidate_id']: d for f in (source / 'initial-review').glob('*.json')
                 for d in [json.loads(f.read_text())]}
    rows = []; cards = []
    # Explicitly chosen evidence windows from the manual review, not a labeler.
    windows = {
        6: {6: None, 8: '[dependency-groups]', 10: 'does not provide the extra',
            12: 'Successfully installed', 14: None},
        22: {22: None, 24: None, 26: None, 28: 'Successfully installed',
             30: 'plugins: benchmark'},
        30: {30: 'ModuleNotFoundError', 32: 'Successfully installed', 34: '186 passed, 5 skipped'},
        36: {36: None, 38: 'Successfully installed', 40: 'collected 6247 items'},
        54: {54: 'ModuleNotFoundError', 56: 'Successfully installed',
             58: 'tests/benchmarks/test_north_star.py::test_north_star_validate_json PASSED'},
    }
    for annotation in reviewed['rows']:
        key = f'swesmith-tool0-row{annotation["row"]}-message{annotation["message"]}'
        original = originals[key]
        row = dict(annotation, candidate_id=key, connected_family=original['connected_family'],
                   source_record_sha256=digest((source / 'initial-review' / (key + '.json')).read_bytes()))
        rows.append(row)
        if annotation['decision'] != 'keep_evidence':
            continue
        calls = {original['message_index']: (original['tool_call'], original['observation_index'], original['observation'])}
        calls.update({x['message_index']: (x['tool_call'], x['observation_index'], x['observation'])
                      for x in original['subsequent_tool_calls']})
        evidence = []
        for mi, marker in windows[annotation['message']].items():
            call, oi, observation = calls[mi]
            assert call['id'] in observation['tool_call_ids']
            text = content(observation)
            if marker is None:
                start, end = 0, len(text)
            else:
                index = text.index(marker)
                start, end = max(0, index - 160), min(len(text), index + 1800)
            evidence.append({'message_index': mi, 'observation_index': oi,
                             'tool_call': call, 'original_observation_characters': len(text),
                             'original_observation_sha256': digest(text.encode()),
                             'original_observation_clipped': '<response clipped>' in text,
                             'excerpt_start': start, 'excerpt_end': end,
                             'exact_excerpt': text[start:end]})
        cards.append({**row, **{k: original[k] for k in ['dataset', 'hub_revision', 'parquet_file',
                     'parquet_sha256', 'row_index', 'instance_id', 'traj_id', 'repository',
                     'upstream_revision', 'raw_messages_sha256']}, 'evidence': evidence,
                     'utility_label': None, 'target_state_provided': False,
                     'relation_training_label': None, 'independent_human_review': False})
    if len(rows) != len(originals) or {r['candidate_id'] for r in rows} != set(originals):
        raise ValueError('Every initial candidate must be reviewed exactly once')
    result = {'status': 'evidence_review_only', 'reviewed_tool_calls': len(rows),
              'reviewed_trajectories': len({r['row'] for r in rows}),
              'reviewed_connected_families': len({r['connected_family'] for r in rows}),
              'decisions': dict(Counter(r['decision'] for r in rows)),
              'retained_episodes': len(cards),
              'retained_connected_families': len({c['connected_family'] for c in cards}),
              'new_training_examples': 0, 'actual_utility_labels': 0,
              'source_review_sha256': digest(annotations.read_bytes()),
              'screening_sha256': digest((source / 'screening.json').read_bytes()),
              'export_script_sha256': digest(Path(__file__).read_bytes()),
              'commands_executed': 0, 'rows': rows, 'evidence_cards': cards}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ['rows','evidence_cards']}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ['source', 'annotations', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    export(args.source, args.annotations, args.output)

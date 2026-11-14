"""Recover only the preselected public-log windows for manual evidence review."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import pyarrow.parquet as pq


def extract(parquet, plan_path, output):
    if output.exists():
        raise FileExistsError(output)
    plan = json.loads(plan_path.read_text())
    selected = {x['row_index']: x for x in plan['selected']}
    if len(selected) != len(plan['selected']):
        raise ValueError('This review requires one selected trigger per source row')
    expected = {x['parquet_sha256'] for x in selected.values()}
    if expected != {hashlib.sha256(parquet.read_bytes()).hexdigest()}:
        raise ValueError('Source shard changed')
    records = []; offset = 0
    for batch in pq.ParquetFile(parquet).iter_batches(batch_size=128, columns=plan['loaded_columns']):
        if offset > max(selected):
            break
        for index, source in enumerate(batch.to_pylist(), offset):
            if index not in selected:
                continue
            record = selected[index]
            if (record['instance_id'] != source['instance_id'] or record['traj_id'] != source['traj_id'] or
                record['raw_messages_sha256'] != hashlib.sha256(source['messages'].encode()).hexdigest()):
                raise ValueError('Selected source identity changed')
            messages = json.loads(source['messages']); actions = []
            for mi, message in enumerate(messages):
                for call in message.get('tool_calls') or []:
                    try:
                        json.loads(call['function']['arguments'])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    matches = [(oi, observation) for oi, observation in enumerate(messages[mi+1:], mi+1)
                               if observation.get('role') == 'tool' and call['id'] in (observation.get('tool_call_ids') or [])]
                    actions.append({'message_index': mi, 'tool_call': call,
                                    'observation_index': matches[0][0] if len(matches) == 1 else None,
                                    'observation': matches[0][1] if len(matches) == 1 else None})
            chosen = [i for i, action in enumerate(actions) if action['message_index'] == record['message_index']
                      and action['tool_call']['function']['name'] == 'bash']
            if len(chosen) != 1:
                raise ValueError('Selected command is ambiguous')
            ai = chosen[0]; trigger = actions[ai]
            if trigger['observation_index'] != record['observation_index'] or trigger['observation'] is None:
                raise ValueError('Selected observation linkage changed')
            records.append({**record, **trigger, 'subsequent_tool_calls': actions[ai+1:ai+9]})
        offset += batch.num_rows
    if len(records) != len(selected):
        raise ValueError('Selected window missing')
    output.mkdir(parents=True)
    for record in records:
        (output / (record['candidate_id'] + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'records': len(records), 'connected_families': len({x['connected_family'] for x in records}),
                      'plan_sha256': hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                      'commands_executed': 0, 'utility_labels_created': 0}))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ['parquet', 'plan', 'output']:
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    extract(a.parquet, a.plan, a.output)

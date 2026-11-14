"""Read-only audit of provenance, counterfactual design and shortcut exposure."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from data_pipeline.scope_expansion import input_key, sha, validate_bundle, validate_rows


def source_units(rows, observations):
    """Do not conflate a task identifier with one actual execution trace."""
    ids = {i for r in rows for i in r.get('source_observation_ids', [])}
    chosen = [observations[i] for i in sorted(ids)]
    if any(o['status'] != 'included_identifier_evidence' for o in chosen):
        raise ValueError('Row references an excluded source observation')
    local = sorted({o['provenance']['run_id'] for o in chosen if 'run_id' in o['provenance']})
    public = sorted({(o['provenance']['dataset'],o['provenance']['revision'],o['provenance']['row_index'])
                     for o in chosen if 'row_index' in o['provenance']})
    tasks = sorted({o['source'] for o in chosen})
    pools = sorted({f for r in rows for f in r.get('source_families', [])})
    return {'command_observations':len(chosen), 'task_instances':len(tasks),
            'local_execution_traces':len(local), 'public_trajectory_rows':len(public),
            'execution_records_total':len(local)+len(public), 'connected_source_pools':len(pools),
            'task_instance_ids':tasks, 'local_run_ids':local, 'public_trajectory_coordinates':public,
            'source_pool_ids':pools,
            'independence_warning':'Repeated runs of one task and mutations of one repository revision are not independent source families.'}


def summarize(rows):
    states, memories = defaultdict(Counter), defaultdict(Counter)
    length_correct = 0
    invariance = [defaultdict(set), defaultdict(set)]
    conditions = set()
    for r in rows:
        target = tuple(r['labels'])
        states[r['input']['state_text']][target] += 1
        memories[r['input']['candidate_text']][target] += 1
        m = r['selected_scope'];q = r['controlled_state']['requested_scope'];c = r['controlled_state']['completed_scope']
        prediction = [2 if q is None else int(len(q) != len(m)), 2 if c is None else int(len(c) == len(m))]
        length_correct += prediction == r['labels']
        invariance[0][m, q, r['template']].add(r['labels'][0])
        invariance[1][m, c, r['template']].add(r['labels'][1])
        conditions.add((r.get('repository'), m, q, c))
    if any(len(v) != 1 for group in invariance for v in group.values()):
        raise ValueError('Irrelevant role changed the supervised relation')
    return dict(validate_rows(rows),
                repositories=sorted({r['repository'] for r in rows if 'repository' in r}),
                unique_scope_identifiers=len({r['selected_scope'] for r in rows}),
                distinct_conditions_without_template=len(conditions),
                parsed_scope_character_length_rule_both_correct=length_correct,
                state_only_empirical_joint_upper_bound_correct=sum(max(c.values()) for c in states.values()),
                memory_only_empirical_joint_upper_bound_correct=sum(max(c.values()) for c in memories.values()),
                same_state_memory_changes_label_groups=sum(len(c) > 1 for c in states.values()),
                same_memory_state_changes_label_groups=sum(len(c) > 1 for c in memories.values()),
                irrelevant_role_label_invariance=True)


def audit(data):
    validate_bundle(data)
    manifest = json.loads((data / 'manifest.json').read_text())
    review = json.loads((data / 'source-review.json').read_text())
    observations = {o['observation_id']:o for o in review['observations']}
    families = {f['source']: f for f in review['families']}
    loaded = {name: [json.loads(x) for x in (data / name).read_text().splitlines()]
              for name in manifest['export_sha256'] if name.endswith('.jsonl')}
    ext = manifest['external_holdout']
    if sha(data / ext['file']) != ext['sha256']:
        raise ValueError('External holdout changed')
    loaded[ext['file']] = [json.loads(x) for x in (data / ext['file']).read_text().splitlines()]
    ext_repos = {r['repository'] for r in loaded[ext['file']]}
    training = [r for name, rows in loaded.items() if name.startswith('train-') for r in rows]
    if ext_repos & {r['repository'] for r in training}:
        raise ValueError('External repository leakage')
    if {input_key(r['input']) for r in loaded[ext['file']]} & {input_key(r['input']) for r in training}:
        raise ValueError('External input leakage')
    for rows in loaded.values():
        for r in rows:
            if 'source_families' not in r:
                continue
            scopes = {r['selected_scope'], *r['controlled_state'].values()} - {None}
            if any(not scopes <= set(families[f]['scopes']) for f in r['source_families']):
                raise ValueError('Constructed names are not all evidenced in their declared source pool')
    public_files = 0
    for o in review['observations']:
        p = o['provenance']
        if 'evidence_file' in p:
            path = data / p['evidence_file']
            if sha(path) != p['sha256']:
                raise ValueError('Public observation changed')
            e = json.loads(path.read_text())
            args = json.loads(e['tool_call']['function']['arguments'])
            if args['command'] != p['command'] or e['tool_call']['id'] not in e['observation']['tool_call_ids']:
                raise ValueError('Public command/observation mismatch')
            public_files += 1
    return {'dataset': manifest['dataset'], 'splits': {n: summarize(rows) for n, rows in loaded.items()},
            'source_observation_status_counts': dict(Counter(r['status'] for r in review['observations'])),
            'public_evidence_files_verified': public_files,
            'family_counts': {s: {'repository_revision_or_local_source_groups': sum(f['split'] == s for f in families.values()),
                                  'source_task_instances': len({e for f in families.values() if f['split'] == s for e in f.get('source_instances', [f['source']])}),
                                  'repositories': len({f['repository'] for f in families.values() if f['split'] == s})}
                              for s in ['train', 'dev', 'external']},
            'source_units_by_file': {name:source_units(rows,observations) for name,rows in loaded.items()},
            'source_unit_correction': 'The older source_episodes field counted task IDs, not actual execution traces. Frozen inputs and their earlier metadata are unchanged; task IDs, local run IDs and public trajectory coordinates are now reported separately.',
            'current_states_observed': False, 'memory_utility_labels_present': False,
            'independently_double_annotated': False,
            'warning': 'Development and external source scopes admit a perfect parsed length rule. Use equal-length controls and do not claim semantic generalization from these scores alone.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('data', type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.data), ensure_ascii=False, indent=2))

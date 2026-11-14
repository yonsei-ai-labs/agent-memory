"""Collect a small, explicitly reviewed subset of pinned public tool logs.

No commands from the source are executed. Outcomes/patches are not loaded as
selection criteria or learner fields. Multiple mutations of the same upstream
repository/revision form one family, not independent validation examples.
"""
from __future__ import annotations
from collections import defaultdict
import hashlib
import json
from pathlib import Path

from data_pipeline.scope_expansion import PARQUET_SHA, PUBLIC_REVISION, selected_scopes, sha

REVIEWED_ROWS = {43, 60, 110, 113, 170, 172, 175, 180, 182, 216}
HELD_OUT_REPOS = {'marshmallow-code/apispec', 'amueller/word_cloud'}


def text_content(message):
    content = message.get('content') or ''
    return content if isinstance(content, str) else '\n'.join(x.get('text', '') for x in content)


def collect(parquet, evidence_dir):
    import pyarrow
    import pyarrow.parquet as pq
    if sha(parquet) != PARQUET_SHA:
        raise ValueError('Public parquet differs from pinned Hub LFS digest')
    records = next(pq.ParquetFile(parquet).iter_batches(
        batch_size=256, columns=['instance_id', 'traj_id', 'messages'])).to_pylist()
    if len(records) != 256:
        raise ValueError('Unexpected candidate prefix')
    evidence_dir.mkdir(parents=True, exist_ok=False)
    candidates, observations = [], []
    groups = defaultdict(lambda: defaultdict(list))
    group_sources = defaultdict(set)
    for row_index, record in enumerate(records):
        messages = json.loads(record['messages'])
        repo_part, upstream_revision, _ = record['instance_id'].split('.', 2)
        repository = repo_part.replace('__', '/', 1)
        group = f'public:{repository}@{upstream_revision}'
        created = set()
        actions = []
        for mi, message in enumerate(messages):
            for call in message.get('tool_calls') or []:
                args = json.loads(call['function']['arguments'])
                if call['function']['name'] == 'str_replace_editor' and args.get('command') == 'create':
                    created.add(args.get('path'))
                if call['function']['name'] != 'bash':
                    continue
                command = args.get('command', '')
                scopes = selected_scopes(command)
                if not scopes:
                    continue
                matches = [(oi, obs) for oi, obs in enumerate(messages[mi + 1:mi + 3], mi + 1)
                           if obs['role'] == 'tool' and call['id'] in (obs.get('tool_call_ids') or [])]
                if len(matches) != 1:
                    raise ValueError('Command/observation linkage is ambiguous')
                actions.append((mi, message, call, command, scopes, matches[0]))
        scope_union = sorted({s for a in actions for s in a[4]})
        selected = row_index in REVIEWED_ROWS
        candidates.append({'row_index': row_index, 'source': record['instance_id'], 'repository': repository,
                           'upstream_revision': upstream_revision, 'selected_scopes': scope_union,
                           'status': 'reviewed_for_inclusion' if selected else 'excluded',
                           'reason': 'Command, linked response, repository context and created-file list reviewed.' if selected else
                                     'No supported whole scope, or insufficient distinct scopes in the reviewed repository/revision pool.'})
        if not selected:
            continue
        if not actions or any('/testbed/' + s in created for s in scope_union):
            raise ValueError('Reviewed selection includes no evidence or an agent-created test')
        if repository in {'django/django', 'pvlib/pvlib-python', 'marshmallow-code/marshmallow',
                          'scikit-learn/scikit-learn', 'sphinx-doc/sphinx', 'sympy/sympy'}:
            raise ValueError('Expected a new repository for this public supplement')
        for mi, message, call, command, scopes, (oi, observation) in actions:
            oid = f'swesmith-tool0-row{row_index}-message{mi}'
            evidence = {'dataset': 'SWE-bench/SWE-smith-trajectories', 'hub_revision': PUBLIC_REVISION,
                        'parquet_file': 'data/tool-00000-of-00008.parquet', 'parquet_sha256': PARQUET_SHA,
                        'parquet_row_index': row_index, 'instance_id': record['instance_id'],
                        'traj_id': record['traj_id'], 'repository': repository,
                        'upstream_revision': upstream_revision,
                        'raw_messages_sha256': hashlib.sha256(record['messages'].encode()).hexdigest(),
                        'message_index': mi, 'tool_call': call, 'observation_index': oi,
                        'observation': observation, 'created_paths_in_trace': sorted(created),
                        'observed_whole_scope_selection': scopes,
                        'observed_target_state_or_memory_utility': False}
            path = evidence_dir / (oid + '.json')
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
            observations.append({'observation_id': oid, 'source': record['instance_id'], 'repository': repository,
                                 'provenance': {'dataset': evidence['dataset'], 'revision': PUBLIC_REVISION,
                                                'row_index': row_index, 'message_index': mi, 'command': command,
                                                'evidence_file': 'evidence/public/' + path.name, 'sha256': sha(path)},
                                 'selected_scopes': scopes, 'status': 'included_identifier_evidence',
                                 'reason': 'Actual whole-file command and linked observation; no selected file was created by the agent in this trace. Selection alone is used, not passing/completion.'})
            for s in scopes:
                groups[group][s].append(oid)
            group_sources[group].add(record['instance_id'])
    families = []
    for group, scopes in sorted(groups.items()):
        repository = group.removeprefix('public:').split('@')[0]
        if len(scopes) < 2:
            raise ValueError('Reviewed pool lacks a second observed scope')
        families.append({'source': group, 'source_instances': sorted(group_sources[group]),
                         'repository': repository, 'split': 'external' if repository in HELD_OUT_REPOS else 'train',
                         'scopes': dict(scopes), 'synthetic_defect_tasks': True})
    return {'dataset': 'SWE-bench/SWE-smith-trajectories', 'url': 'https://huggingface.co/datasets/SWE-bench/SWE-smith-trajectories',
            'hub_revision': PUBLIC_REVISION, 'parquet_sha256': PARQUET_SHA, 'declared_license': 'mit',
            'candidate_prefix_rows': 256, 'candidates': candidates, 'observations': observations,
            'families': families, 'reviewed_trajectory_rows': sorted(REVIEWED_ROWS),
            'loaded_columns': ['instance_id', 'traj_id', 'messages'], 'pyarrow_version': pyarrow.__version__,
            'source_outcomes_or_patches_used_for_selection': False,
            'review_status': 'single_researcher_evidence_reviewed_not_independent_double_annotation'}

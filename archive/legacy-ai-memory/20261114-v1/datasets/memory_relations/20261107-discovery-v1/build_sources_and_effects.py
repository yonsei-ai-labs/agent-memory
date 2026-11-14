"""Preserve actual source cards and separate historical assigned-action outcomes."""
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'experiments/swe_memory/scripts'))
from records import now, write_json

HERE = Path(__file__).resolve().parent
FOCUS = {'pvlib__pvlib-python-1154': 11, 'sphinx-doc__sphinx-8282': 14,
    'sphinx-doc__sphinx-8435': 27, 'django__django-14667': 11, 'pytest-dev__pytest-7373': 6,
    'scikit-learn__scikit-learn-10508': 4, 'sympy__sympy-24066': 11,
    'sympy__sympy-18621': 9, 'scikit-learn__scikit-learn-11281': 14, 'django__django-11019': 22}
COHORTS = {
    'framing12': 'framing-smoke-20261105/final-results.json',
    'transfer24': 'new-content-transfer-20261105/final-results.json',
    'prescription32': 'prescription-validation-20261107/final-results.json',
    'replication16': 'prescription-replication-20261107/final-results.json',
    'restoration32': 'process-restoration-20261107/results.json',
}


def ref(path, locator=None):
    result = dict(path=str(path.relative_to(ROOT)), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    if locator is not None:
        result['locator'] = locator
    return result


def dump_lines(path, rows):
    path.write_text(''.join(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n' for row in rows))


def build():
    assert not (HERE / 'artifact-index.json').exists(), 'Frozen dataset; use a new version.'
    global_path = ROOT / 'docs/reports/data/runs.json'
    records = json.loads(global_path.read_text()); bank = []; source_by_card = {}; source_by_task = {}
    for record in records:
        if record.get('record_kind') != 'memory_authoring':
            continue
        author = ROOT / 'runs' / record['record_id']; path = author / 'candidate-bank.json'
        if not path.exists():
            continue
        cards = json.loads(path.read_text())['cards']
        for original in cards:
            source = original['source_instance_id']; directory = ROOT / 'runs' / original['source_run']
            manifest = json.loads((directory / 'manifest.json').read_text())
            summary = json.loads((directory / 'summary.json').read_text())
            task = json.loads((directory / 'task.json').read_text())
            commands = [(n, json.loads(line)) for n, line in enumerate((directory / 'commands.jsonl').read_text().splitlines(), 1)
                        if json.loads(line)['phase'] == 'agent']
            line, focused = commands[FOCUS[source]]
            kind = original.get('kind') or ('procedure' if 'procedure' in original['id'] else 'facts')
            episode_path = HERE / 'evidence/source-episodes' / (source + '.json')
            if not episode_path.exists():
                selected = sorted(set(original.get('evidence_command_indices') or []) | {FOCUS[source]})
                if source == 'django__django-11019':
                    selected = sorted(set(selected) | {17})
                evidence = [dict(agent_command_index=i, jsonl_line=commands[i][0], event=commands[i][1]) for i in selected]
                write_json(episode_path, dict(source_instance_id=source, source_run=original['source_run'],
                    public_task={k: task[k] for k in ['instance_id','repo','base_commit','version','created_at','problem_statement']},
                    official_resolved=summary['official_resolved'], evaluation_complete=summary.get('evaluation_complete'),
                    focused_command_index=FOCUS[source], commands=evidence,
                    original_files=[ref(directory / name) for name in ['manifest.json','summary.json','task.json','commands.jsonl','official_report.json']],
                    metadata_note='Source-local observations and official source outcome are separate. For11019 explicit failures establish '
                        'unresolved status despite parser gaps; source raw manifest stays unchanged.'))
            entry = dict(memory_id=original['id'], kind=kind, text=original['text'],
                text_sha256=hashlib.sha256(original['text'].encode()).hexdigest(),
                source_instance_id=source, source_run=original['source_run'], repo=original['repo'],
                available_at=original['available_at'], eligible_target_ids=original['eligible_target_ids'],
                actual_source_official_resolved=summary['official_resolved'],
                original_card_source_outcome=original.get('source_official_resolved'),
                source_evaluation_complete=summary.get('evaluation_complete'),
                source_evidence=ref(episode_path), original_card_file=ref(path),
                focused_observation=dict(command=focused['action']['command'],
                    returncode=focused['output']['returncode'],
                    output_excerpt=focused['output']['output'][-700:], agent_command_index=FOCUS[source], jsonl_line=line),
                memory_origin='original_agent_written_card_no_rewrite')
            bank.append(entry); source_by_card[entry['memory_id']] = source
            if kind == 'procedure':
                source_by_task[source] = entry
    assert len(bank) == 20 and len(source_by_task) == 10
    write_json(HERE / 'source-bank.json', dict(at=now(), memory_cards=bank, source_tasks=10,
        repositories=sorted({entry['repo'] for entry in bank}), model_api_calls=0,
        interpretation='Original20 facts/procedure cards from10 historical agent source episodes. No new writer,teacher,or paraphrase calls.'))

    observations = []; groups = defaultdict(list); seen_runs = set(); input_refs = []
    framing_cases = json.loads((ROOT / 'docs/reports/data/framing-smoke-20261105/cases.json').read_text())['cases']
    transfer_cases = json.loads((ROOT / 'docs/reports/data/new-content-transfer-20261105/cases.json').read_text())['cases']
    for cohort, relative in COHORTS.items():
        path = ROOT / 'docs/reports/data' / relative
        result = json.loads(path.read_text()); input_refs.append(ref(path))
        for row in result['rows']:
            run_id = row['run_id']; assert run_id not in seen_runs; seen_runs.add(run_id)
            task_id = row['instance_id']; directory = ROOT / 'runs' / run_id
            actor_manifest = json.loads((directory / 'manifest.json').read_text())
            effective = json.loads((directory / 'effective-config.json').read_text())
            if cohort == 'framing12':
                source = source_by_card[framing_cases[task_id]['card']]
            elif cohort == 'transfer24':
                source = transfer_cases[task_id]['source_card']['source_instance_id']
            elif cohort in ['prescription32', 'replication16']:
                source = 'sympy__sympy-24066'
            else:
                source = row['source_instance_id']
            arm = row.get('arm', row.get('content_variant', row.get('framing')))
            external = row.get('externally_interrupted', False)
            status = row['status']; known = row.get('outcome_known', not external)
            if external:
                success = None
            elif status == 'no_submission':
                success = 0
            elif status == 'evaluated' and row.get('evaluation_complete'):
                success = int(row['official_resolved'] is True)
            else:
                raise ValueError('Historical outcome requires explicit adjudication: ' + run_id)
            complete = row['cost_complete']
            entry = dict(observation_id=run_id, cohort=cohort, source_instance_id=source,
                target_instance_id=task_id, action=arm, replicate=row['replicate'],
                labels=dict(assigned_action=arm, submitted_success=success,
                    api_cost_usd=row['reported_cost_usd'] if complete else None),
                observed=dict(status=status, official_resolved=row.get('official_resolved'),
                    no_submission=status == 'no_submission', outcome_known=known,
                    externally_interrupted=external, cost_complete=complete,
                    logged_partial_cost_usd=row['reported_cost_usd'], api_attempts=row['api_attempts'],
                    cached_input_tokens=row.get('cached_input_tokens')),
                actor_conditions=dict(requested_model=actor_manifest.get('requested_model'),
                    batch_id=actor_manifest.get('batch_id'), phase=actor_manifest.get('phase'),
                    limits={k: effective['agent'].get(k) for k in ['step_limit','cost_limit','wall_time_limit_seconds']},
                    configuration=ref(directory / 'effective-config.json')),
                provenance=[ref(path), ref(directory / 'manifest.json')],
                counterfactual_labels=None, individual_memory_utility_label=None,
                assignment_probability=None,
                eligibility='historical_discovery_ledger_not_counterfactual_training_labels')
            observations.append(entry); groups[(cohort, task_id, arm)].append(entry)
    assert len(observations) == 116 and sum(r['labels']['submitted_success'] is None for r in observations) == 1
    dump_lines(HERE / 'effect-observations.jsonl', observations)
    cells = []
    for (cohort, task, arm), group in sorted(groups.items()):
        successes = sum(r['labels']['submitted_success'] == 1 for r in group)
        unknown = sum(r['labels']['submitted_success'] is None for r in group)
        cells.append(dict(cohort=cohort, task_id=task, source_instance_id=group[0]['source_instance_id'],
            action=arm, assigned=len(group), successes=successes, unknown_outcomes=unknown,
            success_rate_lower=successes / len(group), success_rate_upper=(successes + unknown) / len(group),
            no_submission=sum(r['observed']['no_submission'] for r in group),
            mean_api_cost_usd=sum(r['labels']['api_cost_usd'] for r in group) / len(group)
                if all(r['labels']['api_cost_usd'] is not None for r in group) else None))
    with (HERE / 'effect-cells.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(cells[0])); writer.writeheader(); writer.writerows(cells)
    panel = []
    for cohort, task in sorted({(c['cohort'], c['task_id']) for c in cells}):
        subset = [c for c in cells if c['cohort'] == cohort and c['task_id'] == task]
        complete = [c for c in subset if c['unknown_outcomes'] == 0]
        varied = len({c['success_rate_lower'] for c in complete}) > 1
        panel.append(dict(cohort=cohort, task_id=task,
            observed_rate_variation=varied, has_unknown_outcome=any(c['unknown_outcomes'] for c in subset),
            variation_comparison='Only arms with completely observed outcomes; missingness bounds are not observed gains.',
            all_complete_actions_successful=all(c['successes'] == c['assigned'] for c in subset),
            all_observed_outcomes_failed=all(c['successes'] == 0 for c in subset),
            normal_no_submission=sum(c['no_submission'] for c in subset),
            cells=subset,
            interpretation='Descriptive small-sample panel only. Arm differences are not useful/harmful labels. '
                'Do not pool different cohorts,reader budgets,or memory packages to train a universal gain label.'))
    write_json(HERE / 'historical-signal-map.json', dict(at=now(), observations=116, unique_run_ids=len(seen_runs),
        unknown_outcomes=1, cohorts={cohort: sum(r['cohort'] == cohort for r in observations) for cohort in COHORTS},
        panel=panel, original_results=input_refs,
        interpretation='Existing action outcomes only; no new SWE agent experiment. Success/cost are factual labels for assigned actions, '
            'not both potential outcomes of one execution. Reported differences remain exploratory and include sampling/submission effects.'))
    print(json.dumps(dict(source_cards=20, source_experiences=10, repositories=len({r['repo'] for r in bank}),
        historical_assigned_outcomes=len(observations), unknown_outcomes=1, model_api_calls=0), ensure_ascii=False))


if __name__ == '__main__':
    build()

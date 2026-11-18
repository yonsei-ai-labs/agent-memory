"""Verify the fixed-bank repeats against original writers, provider wires and the native evaluator."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.analysis.forms_results import contrast
from agent_memory_eval.analysis.fresh_behavior import both_controls, field_rates, inspect_mutations, tabulate_groups
from agent_memory_eval.analysis.requests import audit_requests
from agent_memory_eval.contracts.core import ContractError, MemoryPolicy, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS, write_json
from agent_memory_eval.memory.context import memory_system_suffix, past_memory_instruction, snapshot_message
from agent_memory_eval.memory.preparation import snapshot_items
from agent_memory_eval.recording.journal import BlobStore, read_experiment, safe_path
from analyze_fresh_forms import accounting
from run_frozen_bank_repeats import prepare, sealed
from regrade_two_pass_forms import regrade

WRITES = {'book_reservation', 'cancel_reservation', 'send_certificate', 'update_reservation_baggages',
          'update_reservation_flights', 'update_reservation_passengers'}


def verify_shard(args):
    workspace, run, name, manifest, root_seal, offset, criteria = args
    shard = run / name
    close = read_json((shard / 'completion.json').read_bytes())
    if close != root_seal['shards'][name]:
        raise ContractError('Shard closure differs from the full run')
    control = sealed(shard, 'control.json', close['control_sha256'])
    rows = sealed(shard, 'allocations.json', close['allocation_sha256'])
    expected = manifest['scheduled'][name]
    if (control['profile']['preflight_sha256'] != digest(canonical(manifest))
            or control['design']['allocation']['schedule_sha256'] != digest(canonical(expected))
            or len(rows) != len(expected)):
        raise ContractError('Shard points to a different protocol or schedule')
    banks = {(b['task'], b['writer'], b['arm']): b for b in manifest['banks']}
    native = regrade(shard, workspace, workspace / 'data/tau2-bench')
    grades = {r['index']: r for r in native['grades']}
    episodes, generation_ids = [], []
    for row, slot in zip(rows, expected, strict=True):
        if any(row[k] != v for k, v in slot.items()):
            raise ContractError('Observed solve does not match its allocated slot')
        task, arm, rep = row['task_id'], row['arm'], row['repetition']
        folder = safe_path(shard, row['directory'])
        bank = banks.get((task, rep, arm))
        entry = dict(index=offset+row['index'], shard=name, local_index=row['index'], task_id=task, arm=arm,
                     writer_block=rep, wave=row['wave'], solver_seed=row['solver_seed'], run_id=row['run_id'],
                     repetition=(row['wave'] - 1) * 2 + rep,
                     source_writer_run_id=bank['run'] if bank else None, status=row['status'], official_success=None)
        episodes.append(entry)
        if row['status'] == 'not_started':
            if folder.exists() or 'result' in row:
                raise ContractError('Unstarted slot has side effects')
            entry['reason'] = row.get('reason')
            continue
        res = row['result']
        if (digest((folder / 'log.jsonl').read_bytes()) != res['log_sha256'] or res['run_id'] != row['run_id']
                or res['status'] != row['status']):
            raise ContractError('Episode seal or status changed')
        evidence = read_experiment(folder)
        audit = audit_requests(folder, evidence=evidence)
        scan, state, _ = evidence
        blobs = BlobStore(folder)
        assignment = state['assignments'][row['run_id']]
        start = scan['starts'][row['run_id']]['payload']
        if (not start['fresh_session'] or (assignment['arm'], assignment['task_id'], assignment['repetition']) != (arm, task, rep)
                or start['source_run_ids'] != ([bank['run']] if bank else [])):
            raise ContractError('A fresh episode received another writer or source')
        cfg = blobs.get(assignment['native_config_ref'])
        if cfg['task_split_name'] != 'train' or cfg['task_ids'] != [task] or cfg['seed'] != row['solver_seed']:
            raise ContractError('Recorded native configuration differs from the block')
        items = snapshot_items(blobs, state, start['start_snapshot_id']) if arm != 'no_memory' else []
        if bank and (digest(canonical(items)) != bank['items_sha256'] or assignment['source_writer_bank_sha256'] != bank['bank_sha256']
                     or assignment['source_writer_snapshot_id'] != bank['snapshot']):
            raise ContractError('Snapshot content or writer identity differs across repeated uses')
        if not bank and (items or assignment['source_writer_run_id'] is not None or assignment['source_writer_snapshot_id'] is not None):
            raise ContractError('Control contains a writer or a saved item')
        policy = MemoryPolicy(**assignment['policy'])
        system = blobs.get(start['task_ref'])['content']
        if arm != 'no_memory':
            instruction = blobs.get(assignment['instruction_ref'])
            if digest(instruction.encode()) != control['design']['second_pass']['solve_instruction_ref']['sha256']:
                raise ContractError('Memory instruction changed')
            system += memory_system_suffix(policy, past_memory_instruction(instruction))
        for e in scan['events'].values():
            p = e['payload']
            if e['type'] == 'model.request_finished' and p['status'] == 'response':
                raw = blobs.get(p['response_ref'])
                generation_ids.append(raw['id'])
            elif e['type'] == 'model.request_started':
                wire = blobs.get(p['request_ref'])
                if p['actor_role'] == 'worker':
                    if (wire['seed'] != row['solver_seed'] or wire['messages'][0] != dict(role='system', content=system)
                            or (arm != 'no_memory' and wire['messages'][1] != snapshot_message(items))
                            or (arm == 'no_memory' and any(m == snapshot_message([]) for m in wire['messages'][1:]))
                            or any(t['function']['name'].startswith('memory_') for t in wire.get('tools', []))):
                        raise ContractError('Worker request differs from fixed read-only delivery')
                elif p['actor_role'] != 'user_simulator' or 'seed' in wire or any(m == snapshot_message(items) for m in wire['messages']):
                    raise ContractError('Unexpected simulator seed, role or memory')
        billed = res['billing_by_role']
        unknown, reservation = accounting(row, scan, blobs, control['model_catalog_entries'])
        if (len(unknown) != audit['unknown_cost_requests']
                or sum(Decimal(v['reported_usd']) for v in billed.values()) != Decimal(audit['observed_cost_usd_decimal'])
                or sum(v['unknown_count'] for v in billed.values()) != audit['unknown_cost_requests']
                or audit['memory_input_spans'] != res['memory_input_spans']
                or audit['normal_response_memory_spans'] != res['normal_response_memory_spans']
                or res['normal_response_memory_spans'] != 2 * len(items) * res['usage_by_role']['worker']['responses']):
            raise ContractError('Recorded price or exposure differs from the provider journal')
        outcome, = [e['payload'] for e in scan['events'].values() if e['type'] == 'outcome.scored']
        if any(outcome[k] != res[k] for k in ('official_score', 'official_success', 'grading_status')):
            raise ContractError('Episode score differs from its observed outcome event')
        entry.update(session_id=res['session_id'], start_snapshot=start['start_snapshot_id'], item_count=len(items),
                     official_success=res['official_success'], grading_status=res['grading_status'],
                     error_code=res['error_code'], native_steps=res['native_steps'], normal_spans=res['normal_response_memory_spans'],
                     body_exposed=res['normal_response_memory_spans'] > 0,
                     known_usd=audit['observed_cost_usd_decimal'], billing_by_role=billed, http_calls=res['http_calls'],
                     reservation=reservation, first_worker_request_sha256=res['first_worker_request_sha256'],
                     journal_path=str((folder/'log.jsonl').relative_to(workspace)), journal_sha256=res['log_sha256'])
        if row['index'] in grades:
            g = grades[row['index']]
            report = blobs.get(res['report_ref'])
            if report['seed'] != row['solver_seed']:
                raise ContractError('Native trajectory seed differs')
            messages = report['messages']
            entry.update(grade=g, report_path=str((folder/res['report_ref']['path']).relative_to(workspace)),
                         tool_sequence=[c['name'] for m in messages for c in m.get('tool_calls') or []],
                         mutations=inspect_mutations(messages, criteria, WRITES))
        else:
            trajectory = res.get('native_report_ref') or res.get('partial_trajectory_ref')
            if trajectory:
                partial = blobs.get(trajectory)
                messages = partial['messages'] if isinstance(partial, dict) else partial
                entry.update(partial_path=str((folder/trajectory['path']).relative_to(workspace)),
                             partial_tool_sequence=[c['name'] for m in messages for c in m.get('tool_calls') or []],
                             partial_mutations=inspect_mutations(messages, criteria, WRITES))
            if res.get('reached_state_assessment_ref'):
                entry['reached_state_diagnostic'] = blobs.get(res['reached_state_assessment_ref'])
    reserved = sum((Decimal((e.get('reservation') or {}).get('usd','0')) for e in episodes), Decimal(0))
    known = sum((Decimal(e.get('known_usd','0')) for e in episodes), Decimal(0))
    if (sum(e.get('http_calls', 0) for e in episodes) != close['http_calls'] or reserved != Decimal(close['reserved_usd'])
            or abs(known - Decimal(str(close['reported_usd']))) > Decimal('0.00000001')
            or known + reserved > Decimal(str(manifest['guards_per_task_wave']['max_usd']))):
        raise ContractError('Per-shard expense or reservation differs from its seal')
    return dict(episodes=episodes, generations=generation_ids, verified=native['official_results_checked'],
                native_method=native['method_sha256'], network=native['new_model_or_network_attempts'])


def variability(rows, by):
    pairs = defaultdict(list)
    for r in rows:
        pairs[tuple(r[k] for k in by)].append(r)
    counts = defaultdict(Counter)
    for pair in pairs.values():
        if len(pair) != 2:
            raise ContractError('Repeated comparison is missing an allocated member')
        a, b = pair
        x, y = a['official_success'], b['official_success']
        label = 'ungraded' if x is None or y is None else 'different' if x != y else 'both_success' if x else 'both_failure'
        counts[a['arm']][label] += 1
    return dict(counts)


def empty_writer_comparisons(episodes, manifest):
    empty_banks = {b['run'] for b in manifest['banks'] if b['item_count'] == 0}
    blanks = {(r['task_id'], r['repetition']): r for r in episodes if r['arm'] == 'empty_preload'}
    pairs = []
    for r in episodes:
        if r['source_writer_run_id'] not in empty_banks:
            continue
        blank = blanks[r['task_id'], r['repetition']]
        a, b = r['official_success'], blank['official_success']
        wire = r.get('first_worker_request_sha256')
        pairs.append(dict(episode=r['index'], empty_control=blank['index'],
                          both_graded=a is not None and b is not None,
                          different_outcome=None if a is None or b is None else a != b,
                          identical_first_worker_wire=(wire == blank.get('first_worker_request_sha256'))
                          if wire and blank.get('first_worker_request_sha256') else None))
    return pairs


def analyze(workspace):
    manifest, state = prepare(workspace)
    run = workspace / 'runs' / manifest['study']
    final = read_json((run/'completion.json').read_bytes())
    registered = sealed(run, 'manifest.json', final['manifest_sha256'])
    if registered['preflight'] != manifest or set(final['shards']) != set(manifest['scheduled']):
        raise ContractError('Reconstructed assignment or source banks differ from the live study')
    args, offset = [], 0
    for name, scheduled in manifest['scheduled'].items():
        criteria = state['tasks'][scheduled[0]['task_id']]['evaluation_criteria']
        args.append((workspace, run, name, manifest, final, offset, criteria))
        offset += len(scheduled)
    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(verify_shard, args))
    episodes = [r for shard in results for r in shard['episodes']]
    generations = [g for shard in results for g in shard['generations']]
    started = [e for e in episodes if 'session_id' in e]
    if (len(set(generations)) != len(generations) or len({e['session_id'] for e in started}) != len(started)
            or len({e['run_id'] for e in episodes}) != len(episodes)):
        raise ContractError('Provider reply, native session or run identifier was reused')
    starts = [e['start_snapshot'] for e in started if e['arm'] != 'no_memory']
    if len(set(starts)) != len(starts):
        raise ContractError('A solver reused a mutable start snapshot')
    groups = {r['task_id']:r['source_group'] for r in manifest['tasks']}
    first = {r['task_id']:r['first_pass_success'] for r in manifest['tasks']}
    pairs = [dict(task_id=e['task_id'], index=e['index'], arm=e['arm'], repetition=e['repetition'],
                  result=dict(official_success=e['official_success'])) for e in episodes]
    contrasts = [contrast(pairs, arm, control, groups) for arm in FORMS for control in state['cfg']['controls']]
    transitions = defaultdict(Counter)
    for e in episodes:
        src = 'first_success' if first[e['task_id']] else 'first_failure'
        dst = {None:'ungraded', True:'success', False:'failure'}[e['official_success']]
        transitions[e['arm']][src+'__'+dst] += 1
    unknown = sum(v['unknown_count'] for e in started for v in e['billing_by_role'].values())
    reserved_count = sum(bool(e.get('reservation')) for e in started)
    arms = [*FORMS, *state['cfg']['controls']]
    costs = {arm: {role: str(sum((Decimal(e['billing_by_role'].get(role, {}).get('reported_usd', '0'))
                                 for e in started if e['arm'] == arm), Decimal(0)))
                   for role in ('worker', 'user_simulator')} for arm in arms}
    return dict(study=manifest['study'], protocol_sha256=manifest['protocol_sha256'], implementation=registered['implementation'],
                preflight_sha256=digest(canonical(manifest)), completion_sha256=digest((run/'completion.json').read_bytes()),
                original_writer_usd=str(sum(Decimal(b['writer_usd']) for b in manifest['banks'])),
                new_known_usd=str(sum(Decimal(e.get('known_usd','0')) for e in episodes)),
                reserved_usd=str(sum(Decimal((e.get('reservation') or {}).get('usd','0')) for e in episodes)),
                unpriced_requests=unknown, reserved_requests=reserved_count, unreserved_unknown=unknown-reserved_count,
                http_calls=sum(e['http_calls'] for e in started), unique_model_replies=len(generations),
                strict_regrades=sum(s['verified'] for s in results), regrade_network_attempts=sum(s['network'] for s in results),
                strict_regrade_methods=sorted({s['native_method'] for s in results}),
                task_outcomes=tabulate_groups(episodes, {'all': list(groups), **{t:[t] for t in groups}}, arms),
                new_known_usd_by_arm_and_role=costs, empty_writers_vs_matched_blank=empty_writer_comparisons(episodes, manifest),
                source_transitions=dict(transitions), contrasts=contrasts, both_controls=both_controls(episodes, FORMS),
                reference_field_rates=field_rates(episodes), same_bank_across_solver_seeds=variability(episodes, ('task_id','arm','writer_block')),
                two_writer_blocks_at_same_solver_seed=variability(episodes, ('task_id','arm','solver_seed')), episodes=episodes)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a new output path')
    result = analyze(args.workspace.resolve())
    write_json(args.output, result)
    print({k: result[k] for k in ('strict_regrades', 'http_calls', 'new_known_usd', 'reserved_usd')})

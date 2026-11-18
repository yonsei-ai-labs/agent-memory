"""Repeat full airline episodes on immutable first-pass writer banks, with fresh paired controls."""
import argparse
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from decimal import Decimal
import fcntl
import json
from multiprocessing import get_context
import os
from pathlib import Path

from agent_memory_eval.adapters.tau2 import PIN, native_modules, verify_checkout
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import (
    FORMS, BatchGuard, endpoint_terms, execute_prepared, live_models, ordered_jobs, solve_connection, write_json,
)
from agent_memory_eval.experiments.forms_solve import _bank_input
from agent_memory_eval.experiments.forms_write import load_form_sources, read_form_bank
from agent_memory_eval.experiments.fresh_forms import CostReservations
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from agent_memory_eval.runner.tau2_baseline import native_config

PROFILE = 'protocols/pilots/tau2-fixed-bank-repeats-v1.json'


def sealed(root, filename, sha):
    return read_json(asset_text(root, {'path': filename, 'sha256': sha}).encode())


def select_tasks(cohort, sources, tasks):
    strata = [[] for _ in range(5)]
    for item in cohort:
        task = item['task_id']
        success = sources[task].official_success
        criteria = tasks[task]['evaluation_criteria']
        names = {action['name'] for action in criteria['actions']}
        if success and not item['reference_has_db_write']:
            strata[int(bool(criteria['communicate_info']))].append(task)
        elif success and 'book_reservation' in names:
            strata[2].append(task)
        elif not success and 'book_reservation' not in names:
            if 'update_reservation_flights' in names:
                strata[3].append(task)
            if 'cancel_reservation' in names:
                strata[4].append(task)
    return [min(candidates, key=int) for candidates in strata]


def schedule(cfg, task, wave):
    def rank(writer, label):
        return digest(canonical([cfg['name'], task, wave, writer, label]))
    jobs = []
    for writer in sorted(cfg['writer_repetitions'], key=lambda rep: rank(rep, 'block')):
        seed = cfg['solver_seeds_by_wave_and_writer'][wave - 1][writer - 1]
        arms = sorted([*cfg['forms'], *cfg['controls']], key=lambda arm: rank(writer, arm))
        for arm in arms:
            jobs.append(dict(index=len(jobs) + 1, phase='solve', repetition=writer, task_id=task, arm=arm,
                             wave=wave, solver_seed=seed))
    return jobs


def prepare(workspace):
    cfg_bytes = (ROOT / PROFILE).read_bytes()
    cfg = read_json(cfg_bytes)
    if (cfg['status'] != 'ready' or cfg['format'] != 'agent-memory-fixed-bank-repeats/v1'
            or tuple(cfg['forms']) != FORMS or cfg['controls'] != ['no_memory', 'empty_preload']
            or cfg['delivery'] != 'preload' or cfg['split'] != 'train' or cfg['writer_repetitions'] != [1, 2]
            or cfg['solver_seeds_by_wave_and_writer'] != [[4, 5], [5, 4]]):
        raise ContractError('Unexpected fixed-bank protocol')
    parent = safe_path(workspace / 'runs', cfg['parent'])
    seal = sealed(parent, 'completion.json', cfg['parent_completion_sha256'])
    control = sealed(parent, 'control.json', seal['control_sha256'])
    rows = sealed(parent, 'allocations.json', seal['allocation_sha256'])
    design = control['design']
    original = ordered_jobs(design)
    if len(rows) != len(original) or any(any(r[k] != v for k, v in e.items()) for r, e in zip(rows, original)):
        raise ContractError('Parent rows do not match its preregistered assignment')
    checkout = verify_checkout(workspace / 'data/tau2-bench')
    base = checkout / 'data/tau2/domains/airline'
    tasks = {t['id']: t for t in read_json((base / 'tasks.json').read_bytes())}
    split = read_json((base / 'split_tasks.json').read_bytes())[cfg['split']]
    sources = load_form_sources(design, workspace)
    cohort = {r['task_id']: r for r in design['source']['cohort']}
    selected = select_tasks(cohort.values(), sources, tasks)
    if (selected != cfg['tasks'] or len({cohort[t]['source_group'] for t in selected}) != len(selected)
            or not set(selected) <= set(split) or design['source']['benchmark_revision'] != PIN):
        raise ContractError('Task selection, customer independence or benchmark revision changed')
    for task in selected:
        anchor = cfg['analyst_opportunities'][task]
        messages = read_json(sources[task].public_context.encode())['messages']
        if anchor['excerpt'] not in messages[anchor['message']]['content']:
            raise ContractError('The declared opportunity lacks its cited public first-pass evidence')
    cap = Decimal(str(cfg['per_task_wave_guards']['max_usd'])) * len(selected) * 2
    if cap != Decimal(str(cfg['total_usd_limit'])):
        raise ContractError('Independent task-wave caps must sum to the fixed budget')
    connection = read_json(asset_text(ROOT, design['model']['connection_profile_ref']).encode())
    validate_profile(connection)
    instruction = asset_text(ROOT, design['second_pass']['solve_instruction_ref'])
    bindings = {}
    bank_index = []
    for row in rows:
        task, arm, rep = row['task_id'], row['arm'], row['repetition']
        if row['phase'] != 'write' or task not in selected or arm not in FORMS:
            continue
        result = row['result']
        bank = read_form_bank(safe_path(parent, row['directory']), expected_log_sha256=result['log_sha256'])
        if (bank['run_id'] != row['run_id'] or bank['snapshot_id'] != result['snapshot_id']
                or bank['status'] != row['status'] or sources[task].source_run_id != cohort[task]['run_id']):
            raise ContractError('Writer provenance differs from its sealed allocation')
        items, _ = _bank_input(arm, task, rep, bank, None, sources[task].public_context_sha256)
        bindings[rep, task, arm] = row
        bank_index.append(dict(task=task, arm=arm, writer=rep, job=row['index'], run=bank['run_id'],
                               snapshot=bank['snapshot_id'], bank_sha256=digest(canonical(bank)),
                               items_sha256=digest(canonical(items)), item_count=len(items),
                               writer_usd=result['billing_by_role']['writer']['reported_usd']))
    if len(bindings) != len(selected) * len(FORMS) * 2 or len({b['run'] for b in bank_index}) != len(bindings):
        raise ContractError('Two independent original writers per task and form are required')
    selected_sources = [dict(cohort[t], first_pass_success=sources[t].official_success) for t in selected]
    manifest = dict(study=cfg['name'], protocol_sha256=digest(cfg_bytes), parent=cfg['parent'],
                    parent_completion_sha256=cfg['parent_completion_sha256'], benchmark_revision=PIN,
                    tasks=selected_sources, banks=bank_index, guards_per_task_wave=cfg['per_task_wave_guards'],
                    scheduled={shard_name(t, w): schedule(cfg, t, w) for w in (1, 2) for t in selected})
    state = dict(cfg=cfg, parent=parent, design=design, endpoints=endpoint_terms(control['model_catalog_entries']),
                 connection=connection, instruction=instruction, sources={t: sources[t] for t in selected},
                 tasks={t: tasks[t] for t in selected}, bindings=bindings, checkout=checkout)
    return manifest, state


def shard_name(task, wave):
    return f'task-{int(task):02d}-wave-{wave}'


def run_shard(root, manifest, state, identity, models, task, wave):
    cfg = state['cfg']
    name = shard_name(task, wave)
    dest = root / name
    jobs = manifest['scheduled'][name]
    design = deepcopy(state['design'])
    design['format'] = 'agent-memory-frozen-bank-native-shard/v1'
    design['source'] = dict(split='train', benchmark_revision=PIN, cohort=[r for r in manifest['tasks'] if r['task_id'] == task])
    design['writing']['lines'] = {arm: design['writing']['lines'][arm] for arm in FORMS}
    for pair in design['model']['seed_pairs']:
        pair['native_and_solver'] = cfg['solver_seeds_by_wave_and_writer'][wave - 1][pair['repetition'] - 1]
    design['allocation'] = dict(task=task, wave=wave, schedule_sha256=digest(canonical(jobs)))
    design['second_pass'].update(primary_arms=list(FORMS), matched_controls=cfg['controls'], diagnostic_task_ids=[])
    design.pop('execution_freeze_pending', None)
    design.update(status='ready', executable=True, scope=cfg['scope'], purpose=cfg['primary'], analysis=cfg['secondary'])
    profile = dict(name=cfg['name'], shard=name, config_sha256=manifest['protocol_sha256'],
                   preflight_sha256=digest(canonical(manifest)), implementation_commit=identity['implementation_commit'],
                   guards=cfg['per_task_wave_guards'], http_timeout_seconds=cfg['http_timeout_seconds'])
    if dest.exists():
        seal = read_json((dest / 'completion.json').read_bytes())
        archived = sealed(dest, 'control.json', seal['control_sha256'])
        allocated = sealed(dest, 'allocations.json', seal['allocation_sha256'])
        if (archived['profile'] != profile or archived['design'] != design or len(allocated) != len(jobs)
                or any(any(row[k] != v for k, v in job.items()) for row, job in zip(allocated, jobs))):
            raise ContractError('Closed shard differs from the expected protocol')
        return seal
    modules = native_modules(verify_checkout(state['checkout']))
    from loguru import logger
    logger.disable('tau2')
    for row in jobs:
        native_config(solve_connection(state['connection'], design, row, profile['guards']['max_usd']))
    plan = dict(jobs=jobs, sources={task: state['sources'][task]}, tasks={task: state['tasks'][task]},
                modules=modules, instruction=state['instruction'], connection=state['connection'],
                summary=dict(shard=name, jobs=len(jobs), schedule_sha256=design['allocation']['schedule_sha256']))
    guard = BatchGuard(**profile['guards'])
    reserve = CostReservations(dest, cfg['unpriced_http'], guard, models)
    return execute_prepared(profile, ROOT, dest, identity, design, plan, models, guard,
                            imported_writers=(state['parent'], state['bindings']), close_cost=reserve)


def run_task(args):
    root, manifest, state, identity, models, task = args
    results = {}
    for wave in (1, 2):
        name = shard_name(task, wave)
        results[name] = run_shard(root, manifest, state, identity, models, task, wave)
    return results


def run(workspace, manifest, state):
    cfg = state['cfg']
    identity = implementation_identity(ROOT)
    if not os.environ.get('OPENROUTER_API_KEY'):
        raise ContractError('OPENROUTER_API_KEY_MISSING')
    models = live_models(state['connection'])
    if endpoint_terms(models) != state['endpoints'] or implementation_identity(ROOT) != identity:
        raise ContractError('Route, pricing or implementation changed before launch')
    out = safe_path(workspace / 'runs', cfg['name'])
    out.mkdir(exist_ok=True)
    fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        registry = dict(preflight=manifest, implementation=identity, protocol=cfg)
        ledger = out / 'manifest.json'
        if ledger.exists():
            if read_json(ledger.read_bytes()) != registry:
                raise ContractError('Existing execution has a different protocol, bank or implementation')
        elif any(out.iterdir()):
            raise ContractError('Unrecognized execution directory')
        else:
            write_json(ledger, registry)
        args = [(out, manifest, state, identity, models, task) for task in cfg['tasks']]
        with ProcessPoolExecutor(max_workers=cfg['max_concurrent_tasks'], mp_context=get_context('spawn')) as pool:
            results = {name: seal for completed in pool.map(run_task, args) for name, seal in completed.items()}
        summary = dict(manifest_sha256=digest(ledger.read_bytes()), shards=results)
        terminal = out / 'completion.json'
        if terminal.exists():
            if read_json(terminal.read_bytes()) != summary:
                raise ContractError('Final seal has changed')
        else:
            write_json(terminal, summary)
        return summary
    finally:
        os.close(fd)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--preflight-output', type=Path)
    args = parser.parse_args()
    manifest, state = prepare(args.workspace.resolve())
    if args.preflight_output:
        if args.preflight_output.exists():
            parser.error('Use a new preflight output')
        write_json(args.preflight_output, manifest)
    if args.live:
        result = run(args.workspace.resolve(), manifest, state)
        print(json.dumps({'shards': len(result['shards']), 'manifest_sha256': result['manifest_sha256']}))
    else:
        print(json.dumps({'tasks': len(manifest['tasks']), 'writers': len(manifest['banks']),
                          'episodes': sum(map(len, manifest['scheduled'].values())), 'preflight_sha256': digest(canonical(manifest))}))

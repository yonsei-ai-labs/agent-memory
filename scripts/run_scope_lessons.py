"""Compare two writing prompts for the same lesson form on different customers' full episodes."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path

from agent_memory_eval.adapters.tau2 import PIN, verify_checkout
from agent_memory_eval.contracts.connection import validate_profile
from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import (
    BatchGuard, endpoint_terms, execute_prepared, live_models, preflight_sizes, write_json,
)
from agent_memory_eval.experiments.forms_solve import _bank_input
from agent_memory_eval.experiments.forms_write import load_form_sources, read_form_bank
from agent_memory_eval.experiments.fresh_forms import CostReservations, scenario_customer
from agent_memory_eval.experiments.prompt_comparison import CONTROLS, FORMAT, comparison_jobs, compile_config
from agent_memory_eval.recording.assets import asset_text
from agent_memory_eval.recording.journal import safe_path
from agent_memory_eval.reproduction.freeze import implementation_identity
from scripts.run_frozen_bank_repeats import run, schedule, sealed, shard_name

PROFILE = 'protocols/pilots/tau2-scope-lessons-v1.json'


def prepare(workspace):
    raw = (ROOT / PROFILE).read_bytes()
    cfg = read_json(raw)
    base = read_json(asset_text(ROOT, cfg['base_design_ref']).encode())
    previous = read_json(asset_text(ROOT, cfg['previous_test_ref']).encode())
    transfer = read_json(asset_text(ROOT, cfg['previous_transfer_ref']).encode())
    checkout = verify_checkout(workspace / 'data/tau2-bench')
    native = checkout / 'data/tau2/domains/airline'
    assets = {k: sealed(native, k, sha) for k, sha in previous['native_assets'].items()}
    tasks = {t['id']: t for t in assets['tasks.json']}
    split = assets['split_tasks.json']
    customers = {k: scenario_customer(t, assets['db.json']) for k, t in tasks.items()}
    train = {customers[t] for t in split['train']}
    old_tests = set(previous['tasks'] + transfer['tasks'])
    seen = train | {customers[t] for t in old_tests}
    first = {}
    for t in sorted(split['test'], key=int):
        if customers[t] not in seen:
            first.setdefault(customers[t], t)
    pairs = cfg['pairs']
    targets = [p['target'] for p in pairs]
    source_ids = [p['source'] for p in pairs]
    guards = cfg['per_task_wave_guards']
    if (cfg['format'] != 'agent-memory-transfer-writing-comparison/v1' or cfg['status'] != 'ready'
            or tuple(cfg['controls']) != CONTROLS or cfg['forms'] != ['lesson_original', 'lesson_scoped']
            or cfg['reference_form'] != 'lesson' or cfg['delivery'] != 'preload'
            or (cfg['source_split'], cfg['split']) != ('train', 'test') or PIN != base['source']['benchmark_revision']
            or cfg['writer_repetitions'] != [1, 2] or cfg['writer_seeds'] != [0, 1]
            or cfg['solver_seeds_by_wave_and_writer'] != [[4, 5], [5, 4]]
            or targets != cfg['tasks'] or len(set(customers[t] for t in targets + source_ids)) != 2 * len(pairs)
            or set(targets) - set(split['test']) or set(targets) & old_tests
            or set(source_ids) - set(split['train']) or set(source_ids) & {p['source'] for p in transfer['pairs']}
            or set(first.values()) != {p['target'] for p in pairs if p['stratum'] == 'new_customer'}
            or Decimal(str(cfg['writer_guards']['max_usd'])) + len(pairs)*2*Decimal(str(guards['max_usd']))
            != Decimal(str(cfg['total_usd_limit']))):
        raise ContractError('Task splits, customers, conditions, crossing or stage budgets differ')
    targets_meta = []
    for p in pairs:
        t = p['target']
        expected = 'train_customer_new_task' if customers[t] in train else 'new_customer'
        if p['stratum'] != expected or any(assets['db.json']['reservations'][i]['user_id'] != customers[t]
                                           for i in p['reservation_ids']):
            raise ContractError('Target stratum or diagnostic booking belongs to a different customer')
        targets_meta.append(dict(task_id=t, source_task_id=p['source'], stratum=p['stratum'],
                                 customer_group='customer-' + digest(customers[t].encode())[:12],
                                 native_task_sha256=digest(canonical(tasks[t]))))
    line = base['writing']['lines'][cfg['reference_form']]
    conditions = [dict(id=arm, representation='lesson', prompt_version=arm+'-v1', prompt=prompt)
                  for arm,prompt in zip(cfg['forms'], [line, line+' '+cfg['append_instruction']], strict=True)]
    design = compile_config(ROOT, dict(format=FORMAT, status=cfg['status'], name=cfg['parent'],
        base_design_ref=cfg['base_design_ref'], endpoint_ref=cfg['endpoint_ref'], conditions=conditions,
        read_mode=cfg['delivery'], task_sets=dict(development=[], evaluation=source_ids),
        seed_pairs=base['model']['seed_pairs'], guards=cfg['writer_guards'],
        http_timeout_seconds=cfg['http_timeout_seconds'], selection_reason=cfg['selection']))
    if [s['writer'] for s in design['model']['seed_pairs']] != cfg['writer_seeds']:
        raise ContractError('Archived writer seeds differ')
    sources = load_form_sources(design, workspace)
    connection = read_json(asset_text(ROOT, design['model']['connection_profile_ref']).encode())
    validate_profile(connection)
    instruction = asset_text(ROOT, design['second_pass']['solve_instruction_ref'])
    common = asset_text(ROOT, design['writing']['common_instruction_ref'])
    jobs = [r for r in comparison_jobs(design) if r['phase'] == 'write']
    wire_sizes = preflight_sizes(jobs, design, sources, tasks, connection, instruction, common)
    sources_meta = [dict(c, first_pass_success=sources[c['task_id']].official_success) for c in design['source']['cohort']]
    scheduled = {shard_name(p['target'], w): [dict(r, source_task_id=p['source']) for r in schedule(cfg,p['target'],w)]
                 for w in (1,2) for p in pairs}
    manifest = dict(study=cfg['name'], protocol_sha256=digest(raw), parent=cfg['parent'], benchmark_revision=PIN,
                    target_split=cfg['split'], tasks=targets_meta, sources=sources_meta, forms=cfg['forms'],
                    conditions=design['conditions'], writer_jobs=jobs, writer_wire_sizes=wire_sizes,
                    guards_per_task_wave=guards, scheduled=scheduled)
    receipt = read_json(asset_text(ROOT, cfg['endpoint_ref']).encode())
    state = dict(cfg=cfg, parent=safe_path(workspace/'runs', cfg['parent']), design=design,
                 connection=connection, endpoints=endpoint_terms(receipt['models']), checkout=checkout,
                 sources=sources, source_for_target=dict(zip(targets,source_ids,strict=True)),
                 tasks={t:tasks[t] for t in targets}, instruction=instruction)
    return manifest, state


def writer_profile(manifest, state, identity):
    cfg = state['cfg']
    return dict(name=cfg['parent'], config_sha256=manifest['protocol_sha256'], preflight_sha256=digest(canonical(manifest)),
                implementation_commit=identity['implementation_commit'], guards=cfg['writer_guards'],
                http_timeout_seconds=cfg['http_timeout_seconds'])


def write_banks(manifest, state, identity, models):
    folder = state['parent']
    if folder.exists():  # Never retry an observed writer, including a partially written ledger.
        if not (folder/'completion.json').is_file():
            raise ContractError('Writer ledger is incomplete; preserve its started jobs')
        return
    profile = writer_profile(manifest,state,identity)
    guard = BatchGuard(**profile['guards'])
    reserve = CostReservations(folder, state['cfg']['unpriced_http'], guard, models)
    plan = dict(jobs=manifest['writer_jobs'], sources=state['sources'], connection=state['connection'],
                summary=manifest['writer_wire_sizes'])
    execute_prepared(profile, ROOT, folder, identity, state['design'], plan, models, guard, close_cost=reserve)


def bind_banks(manifest, state, identity):
    parent = state['parent']
    raw = (parent/'completion.json').read_bytes()
    close = read_json(raw)
    ctl = sealed(parent, 'control.json', close['control_sha256'])
    rows = sealed(parent, 'allocations.json', close['allocation_sha256'])
    if (ctl['profile'] != writer_profile(manifest,state,identity) or ctl['design'] != state['design']
            or ctl['implementation'] != identity or endpoint_terms(ctl['model_catalog_entries']) != state['endpoints']):
        raise ContractError('Writer protocol, implementation or endpoint changed')
    banks, missing, bindings = [], [], {}
    for row, slot in zip(rows, manifest['writer_jobs'], strict=True):
        if any(row[k] != v for k,v in slot.items()):
            raise ContractError('Writer schedule differs from the sealed allocation')
        t, arm, rep = row['task_id'], row['arm'], row['repetition']
        bindings[rep,t,arm] = row
        if row['status'] not in {'completed','empty'}:
            missing.append([t,rep,arm])
            continue
        res = row['result']
        bank = read_form_bank(safe_path(parent,row['directory']), expected_log_sha256=res['log_sha256'])
        if bank['run_id'] != row['run_id'] or bank['snapshot_id'] != res['snapshot_id'] or bank['status'] != row['status']:
            raise ContractError('Assigned bank and sealed writer disagree')
        items,_ = _bank_input(arm,t,rep,bank,None,state['sources'][t].public_context_sha256,written_arms=manifest['forms'])
        banks.append(dict(task=t, arm=arm, writer=rep, job=row['index'], run=bank['run_id'], snapshot=bank['snapshot_id'],
                          items_sha256=digest(canonical(items)), bank_sha256=digest(canonical(bank)), item_count=len(items),
                          writer_usd=res['billing_by_role']['writer']['reported_usd']))
    if len({r['run_id'] for r in rows}) != len(rows):
        raise ContractError('A writer run ID was reused')
    return (dict(manifest, parent_completion_sha256=digest(raw), banks=banks, unavailable_writers=missing),
            dict(state, bindings=bindings))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--preflight-output', type=Path)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    manifest, state = prepare(workspace)
    if args.preflight_output:
        if args.preflight_output.exists():
            parser.error('Use a new preflight path')
        write_json(args.preflight_output,manifest)
    if args.live:
        identity = implementation_identity(ROOT)
        if not os.environ.get('OPENROUTER_API_KEY'):
            raise ContractError('OPENROUTER_API_KEY_MISSING')
        models = live_models(state['connection'])
        if endpoint_terms(models) != state['endpoints'] or implementation_identity(ROOT) != identity:
            raise ContractError('Route, pricing or implementation changed before writing')
        write_banks(manifest,state,identity,models)
        manifest,state = bind_banks(manifest,state,identity)
        run(workspace,manifest,state)
    print(json.dumps(dict(writer_assignments=len(manifest['writer_jobs']), full_episodes=sum(map(len, manifest['scheduled'].values())),
                          live=args.live, protocol_sha256=manifest['protocol_sha256'])))


if __name__ == '__main__':
    main()

"""Run two retrieval policies on the very same four-form banks used in the fixed decisions."""
import argparse
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS, write_json
from agent_memory_eval.recording.assets import asset_text
from run_frozen_bank_repeats import prepare, run, schedule, sealed, shard_name

PROFILE = 'protocols/pilots/tau2-multi-workflow-v1.json'


def prepare_native(workspace):
    raw = (ROOT / PROFILE).read_bytes()
    profile = read_json(raw)
    cfg = dict(profile['native'])
    previous, state = prepare(workspace)
    inventory = read_json(asset_text(ROOT, profile['inventory_ref']).encode())
    closed = read_json(asset_text(workspace, profile['parent_seal_ref']).encode())
    registered = sealed(workspace / 'runs' / previous['study'], 'manifest.json', closed['manifest_sha256'])
    if previous != inventory or registered['preflight'] != inventory:
        raise ContractError('Original writer inventory differs from the frozen native control')
    tasks = cfg['tasks']
    modes = cfg['read_modes']
    if (tuple(cfg['forms']) != FORMS or cfg['controls'] != ['no_memory', 'empty_preload']
            or modes != ['preload', 'on_demand'] or set(tasks) != set(profile['anchors'])
            or Decimal(str(cfg['per_task_wave_guards']['max_usd'])) * len(tasks) * 2 != Decimal(str(cfg['total_usd_limit']))
            or cfg['solver_seeds_by_wave_and_writer'] != [[6, 7], [7, 6]]):
        raise ContractError('Unexpected treatment, seed crossing or cost allocation')
    selected = [r for r in inventory['tasks'] if r['task_id'] in tasks]
    if len({r['source_group'] for r in selected}) != len(tasks):
        raise ContractError('Each workflow must belong to a distinct first-pass customer')
    arms = {f'{form}__{mode}': dict(writer=form, read_mode=mode) for form in FORMS for mode in modes}
    cfg.update(delivery_arms=arms, instruction_ref=profile['instruction_ref'])
    original = state['bindings']
    bank_rows = [b for b in inventory['banks'] if b['task'] in tasks]
    bindings = {(rep, task, arm): original[rep, task, delivery['writer']] for task in tasks
                for rep in cfg['writer_repetitions'] for arm, delivery in arms.items()}
    instruction = asset_text(ROOT, cfg['instruction_ref'])
    if instruction != profile['instruction'] or len(bank_rows) != len(tasks) * len(FORMS) * 2:
        raise ContractError('Common instruction or original bank count differs')
    scheduled = {shard_name(t, w): schedule(cfg, t, w) for w in (1, 2) for t in tasks}
    manifest = dict(study=cfg['name'], protocol_sha256=digest(raw), parent=inventory['parent'],
                    parent_completion_sha256=inventory['parent_completion_sha256'], benchmark_revision=inventory['benchmark_revision'],
                    tasks=selected, banks=bank_rows, delivery_arms=arms, forms=list(arms),
                    guards_per_task_wave=cfg['per_task_wave_guards'], scheduled=scheduled)
    state.update(cfg=cfg, bindings=bindings, instruction=instruction, tasks={t: state['tasks'][t] for t in tasks})
    return manifest, state


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--preflight-output', type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    manifest, state = prepare_native(workspace)
    if args.preflight_output:
        if args.preflight_output.exists():
            parser.error('Preflight output already exists')
        write_json(args.preflight_output, manifest)
    if args.live:
        run(workspace, manifest, state)
    print(f"{len(manifest['banks'])} sealed banks, {sum(map(len, manifest['scheduled'].values()))} native slots")

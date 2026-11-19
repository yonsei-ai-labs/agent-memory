"""Test the isolated database effect of per-reservation refund methods on the sealed transfer runs."""
import argparse
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import safe_path
from analyze_frozen_bank_repeats import WRITES
from review_fact_recheck_baggage import state_hashes


def replay(workspace, results):
    raw = results.read_bytes()
    evidence = read_json(raw)
    checkout = verify_checkout(workspace / 'data/tau2-bench')
    native_modules(checkout)
    from loguru import logger
    from tau2.data_model.simulation import SimulationRun
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call
    logger.disable('tau2')
    task = next(t for t in read_json((checkout/'data/tau2/domains/airline/tasks.json').read_bytes()) if t['id'] == '18')
    actions = task['evaluation_criteria']['actions']
    methods = {a['arguments']['reservation_id']:a['arguments']['payment_id'] for a in actions}
    if (evidence['study'] != 'tau2-bank-transfer-01' or task['initial_state'] is not None or len(methods) != 5
            or any(a['name'] != 'update_reservation_flights' for a in actions)):
        raise ContractError('Unexpected target, reference actions or initialization')
    first_method = actions[0]['arguments']['payment_id']
    make = registry.get_env_constructor('airline')
    rows = []
    for e in evidence['episodes']:
        if e['task_id'] != '18' or 'grade' not in e:
            continue
        data = safe_path(workspace, e['report_path']).read_bytes()
        if digest(data) != e['grade']['report_sha256']:
            raise ContractError('Target report differs from the regraded trajectory')
        sim = SimulationRun.model_validate(read_json(data))
        options = language_environment_kwargs(reload_recorded_call(sim.language_context, 'airline')) or {}
        ref = make(**options)
        for a in actions:
            ref.make_tool_call(a['name'], requestor='assistant', **a['arguments'])
        reference = state_hashes(ref)
        full = make(**options)
        full.set_state(initialization_data=None, initialization_actions=None, message_history=sim.messages, strict=True)
        actual = state_hashes(full)
        if (actual == reference) != e['grade']['db_match']:
            raise ContractError('Unedited strict DB differs from the submitted score')
        calls = []
        replies = {m.id:m for m in sim.messages if m.role == 'tool'}
        for turn, msg in enumerate(sim.messages):
            for c in getattr(msg,'tool_calls',None) or []:
                if c.name in WRITES and not replies[c.id].error and replies[c.id].execution_status != 'rejected':
                    calls.append((turn,c))
        item = dict(index=e['index'], arm=e['arm'], blind_id=e['transfer_behavior']['blind_id'],
                    actual_db_sha256=actual[0], reference_db_sha256=reference[0], original_match=actual == reference)
        rows.append(item)
        if (len(calls) != len(methods) or any(c.name != 'update_reservation_flights' for _, c in calls)
                or {c.arguments.get('reservation_id') for _,c in calls} != set(methods)):
            item['eligible'] = False
            continue
        item['eligible'] = True
        for label in ('unedited','original_method_per_reservation','first_reference_method_for_all'):
            env = make(**options)
            changed = []
            for turn, call in calls:
                old = call.arguments['payment_id']
                replacement = {'unedited':old, 'original_method_per_reservation':methods[call.arguments['reservation_id']],
                               'first_reference_method_for_all':first_method}[label]
                if old != replacement:
                    changed.append(dict(turn=turn, reservation=call.arguments['reservation_id'], before=old, after=replacement))
                reply = env.get_response(call.model_copy(update={'arguments':dict(call.arguments, payment_id=replacement)}))
                if reply.error:
                    raise ContractError('Isolated refund operation failed')
            hashes = state_hashes(env)
            if label == 'unedited' and hashes != actual:
                raise ContractError('Isolated original writes did not reproduce the full trajectory DB')
            item[label] = dict(matches_reference=hashes == reference, final_db_sha256=hashes[0], changed=changed)
    return dict(format='bank-transfer-refund-field-replay/v1', primary_results_sha256=digest(raw), first_method=first_method,
                purpose='Post hoc, fixed recorded writes and tool ordering. No new dialogue or provider calls.',
                cells=rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a new output path')
    with patch('socket.socket.connect', side_effect=RuntimeError('Network blocked during state replay')) as blocked:
        output = replay(args.workspace.resolve(), args.results)
        if blocked.call_count:
            raise ContractError('Network access attempted during DB replay')
    write_json(args.output, output)
    print('observed cells:', len(output['cells']), 'eligible for paired field edits:', sum(c['eligible'] for c in output['cells']))

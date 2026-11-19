"""Rebuild task 14's DB after changing only total_baggages on one actual replacement booking."""
import argparse
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from agent_memory_eval.adapters.tau2 import native_modules, verify_checkout
from agent_memory_eval.contracts.core import ContractError, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.recording.journal import safe_path
from review_fact_recheck_baggage import state_hashes
from review_tau2_baseline_screening import write_tool_names


def replay(workspace, results):
    info = read_json(results.read_bytes())
    if info['native']['study'] != 'tau2-multi-workflow-native-01':
        raise ContractError('Expected strict regrades from both retrieval conditions')
    checkout = verify_checkout(workspace/'data/tau2-bench')
    native_modules(checkout)
    from tau2.data_model.simulation import SimulationRun
    from tau2.multilingual.runtime import language_environment_kwargs
    from tau2.registry import registry
    from tau2.runner.presets import reload_recorded_call
    domain = checkout/'data/tau2/domains/airline/tasks.json'
    task = next(t for t in read_json(domain.read_bytes()) if t['id']=='14')
    actions = task['evaluation_criteria']['actions']
    if task.get('initial_state') is not None or [a['name'] for a in actions] != ['cancel_reservation','book_reservation'] or actions[-1]['arguments']['total_baggages'] != 0:
        raise ContractError('Initial state or booking task reference changed')
    writes = set(write_tool_names((checkout/'src/tau2/domains/airline/tools.py').read_text()))
    constructor = registry.get_env_constructor('airline')
    rows = []
    for e in info['native_evidence']:
        if e['task_id'] != '14':
            continue
        row = dict(index=e['index'], arm=e['arm'], writer=e['writer_block'], seed=e['solver_seed'], status='ungraded')
        rows.append(row)
        if 'grade' not in e:
            continue
        data = safe_path(workspace,e['report_path']).read_bytes()
        if digest(data) != e['grade']['report_sha256']:
            raise ContractError('Source differs from its regraded native report')
        sim = SimulationRun.model_validate(read_json(data))
        options = language_environment_kwargs(reload_recorded_call(sim.language_context,'airline')) or {}
        actual, edited, reference, strict = (constructor(**options) for _ in range(4))
        for action in actions:
            reference.make_tool_call(action['name'], requestor=action.get('requestor','assistant'), **action['arguments'])
        strict.set_state(initialization_data=None, initialization_actions=None, message_history=sim.messages, strict=True)
        found = {m.id:m for m in sim.messages if m.role=='tool'}
        attempts = [c for m in sim.messages for c in getattr(m,'tool_calls',None) or []
                    if c.name in writes and c.id in found and found[c.id].execution_status != 'rejected']
        bookings = [c for c in attempts if c.name=='book_reservation' and not found[c.id].error]
        row.update(status='no_single_0_or_6_booking', recorded_db_match=e['grade']['db_match'],
                   communication_passed=e['grade']['communication_passed'], native_report_sha256=digest(data))
        if len(bookings)!=1 or bookings[0].arguments.get('total_baggages') not in (0,6):
            continue
        booking = bookings[0]
        old = booking.arguments['total_baggages']
        new = 6 - old
        for c in attempts:
            modified = c.model_copy(update={'arguments':dict(c.arguments,total_baggages=new)}) if c.id==booking.id else c
            outcome = actual.get_response(c)
            alternative = edited.get_response(modified)
            if outcome.error != found[c.id].error or alternative.error != outcome.error:
                raise ContractError('Recorded and modified writes differ in error status')
        baseline, counterfactual, target = map(state_hashes,(actual,edited,reference))
        if baseline != state_hashes(strict) or (baseline==target) != e['grade']['db_match']:
            raise ContractError('Unmodified write sequence differs from the strict native grade')
        row.update(status='replayed', old=old, new=new, edited_db_match=counterfactual==target,
            actual_db_sha256=baseline[0], edited_db_sha256=counterfactual[0], reference_db_sha256=target[0])
    return dict(study=info['native']['study'], results_sha256=digest(results.read_bytes()), task_file_sha256=digest(domain.read_bytes()),
                method_sha256=digest(Path(__file__).read_bytes()), status_counts=dict(Counter(r['status'] for r in rows)), rows=rows)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=ROOT)
    parser.add_argument('--results',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        parser.error('Select a new output file')
    with patch('socket.socket.connect',side_effect=AssertionError('Network disabled for DB replay')) as conn, \
            patch('socket.socket.connect_ex',side_effect=AssertionError('Network disabled for DB replay')) as conn_ex:
        result=replay(args.workspace.resolve(),args.results)
        if conn.call_count or conn_ex.call_count:
            raise ContractError('Network was attempted during DB replay')
    write_json(args.output,result)
    print(result['status_counts'])

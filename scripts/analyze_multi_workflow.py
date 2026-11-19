"""Join immutable first-pass banks to fixed decisions and strictly regraded free-running sessions."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import FORMS, write_json
from agent_memory_eval.experiments.multi_workflow import PROFILE, STUDY, arm_id
from agent_memory_eval.experiments.replay_comparison import audited_allocations
from analyze_frozen_bank_repeats import verify_shard
from run_frozen_bank_repeats import sealed
from run_multi_workflow_native import prepare_native

TEXT_LABELS = {'above_limit', 'withheld_only', 'asks_for_detail', 'plans_lookup', 'answers_other',
               'claims_action', 'requests_confirmation'}


def transition(treatment, control):
    if treatment is None or control is None:
        return 'missing'
    return {(True, False): 'gain', (False, True): 'loss', (True, True): 'both_pass', (False, False): 'both_fail'}[treatment, control]


def native_timing(messages, task):
    """Locate public disclosures relative to native business tool requests, including rejected requests."""
    last_customer = last_assistant = None
    customer_lines, tool_returns, reads, changes = [], [], [], []
    for i, m in enumerate(messages):
        role, text = m['role'], m.get('content')
        if role == 'user' and text:
            last_customer = dict(turn=i, text=text)
            customer_lines.append(last_customer)
        if role == 'tool' and isinstance(text, str):
            tool_returns.append(text.casefold())
        if role != 'assistant':
            continue
        for c in m.get('tool_calls') or []:
            name, args = c['name'], c['arguments']
            if name in {'get_reservation_details', 'get_user_details'}:
                identifier = args.get('user_id') or args.get('reservation_id')
                reads.append(dict(turn=i, tool=name, identifier=identifier,
                    customer_named_before=bool(identifier) and any(identifier.casefold() in u['text'].casefold() for u in customer_lines),
                    current_tool_named_before=bool(identifier) and any(identifier.casefold() in reply for reply in tool_returns)))
            elif name in {'book_reservation', 'cancel_reservation'}:
                changes.append(dict(turn=i, tool=name, arguments=args, preceding_customer=last_customer,
                                    preceding_assistant=last_assistant))
        if text:
            last_assistant = dict(turn=i, text=text)
    trace = dict(reads=reads, requested_changes=changes)
    if task == '3':
        target = next((r for r in reads if r['tool'] == 'get_reservation_details' and r['identifier'] == 'JMO1MG'), None)
        trace['target_booking_lookup'] = target
    if task == '42':
        trace['requested_cancellations'] = [c['arguments'].get('reservation_id') for c in changes if c['tool'] == 'cancel_reservation']
    return trace


def native_analysis(workspace):
    manifest, state = prepare_native(workspace)
    run = workspace / 'runs' / manifest['study']
    final = read_json((run / 'completion.json').read_bytes())
    registered = sealed(run, 'manifest.json', final['manifest_sha256'])
    if registered['preflight'] != manifest or set(final['shards']) != set(manifest['scheduled']):
        raise ContractError('Native registry or number of completed shards differs')
    args, offset = [], 0
    for name, rows in manifest['scheduled'].items():
        task = state['tasks'][rows[0]['task_id']]
        args.append((workspace, run, name, manifest, final, offset, task['evaluation_criteria']))
        offset += len(rows)
    with ProcessPoolExecutor(max_workers=4) as pool:
        parts = list(pool.map(verify_shard, args))
    episodes = [e for p in parts for e in p['episodes']]
    generations = [g for p in parts for g in p['generations']]
    for key in ('run_id', 'session_id', 'start_snapshot'):
        values = [e[key] for e in episodes if e.get(key)]
        if len(set(values)) != len(values):
            raise ContractError('A native run, session or start snapshot was reused')
    if None in generations or len(set(generations)) != len(generations):
        raise ContractError('A native provider reply lacks a unique generation ID')
    for e in episodes:
        if e.get('report_path'):
            report = read_json((workspace / e['report_path']).read_bytes())
            e['public_decision_timing'] = native_timing(report['messages'], e['task_id'])
    by = {(e['task_id'], e['writer_block'], e['solver_seed'], e['arm']): e for e in episodes}
    sources = {s['task_id']: s['first_pass_success'] for s in manifest['tasks']}
    arms = defaultdict(Counter)
    intersections = []
    pairs = []
    for e in episodes:
        counts = arms[e['arm']]
        counts['planned'] += 1
        counts['graded'] += type(e['official_success']) is bool
        counts['success'] += e['official_success'] is True
        counts['first_pass_' + ('success' if sources[e['task_id']] else 'failure') + '_to_' +
               {True:'success',False:'failure',None:'ungraded'}[e['official_success']]] += 1
        for k in ('list_requests', 'get_requests', 'list_returns', 'get_returns', 'title_spans', 'body_spans'):
            counts[k] += e.get(k, 0)
            counts[k+'_episodes'] += e.get(k, 0) > 0
        delivery = manifest['delivery_arms'].get(e['arm'])
        if not delivery:
            continue
        controls = list(state['cfg']['controls'])
        c0, c1 = (by[e['task_id'], e['writer_block'], e['solver_seed'], c] for c in controls)
        if c0['official_success'] is not None and c0['official_success'] == c1['official_success']:
            direction = transition(e['official_success'], c0['official_success'])
            if direction in {'gain','loss'}:
                intersections.append(dict(task=e['task_id'], writer=e['writer_block'], seed=e['solver_seed'],
                    arm=e['arm'], outcome=direction, treatment=e['index'], pure=c0['index'], empty=c1['index']))
        if delivery['read_mode'] == 'preload':
            controls.append(delivery['writer'] + '__on_demand')
        for ctrl in controls:
            b = by[e['task_id'], e['writer_block'], e['solver_seed'], ctrl]
            if ctrl.endswith('__on_demand') and e['source_writer_run_id'] != b['source_writer_run_id']:
                raise ContractError('Retrieval comparison crossed independent writer banks')
            pairs.append(dict(task=e['task_id'], writer=e['writer_block'], seed=e['solver_seed'], treatment=e['arm'],
                              control=ctrl, outcome=transition(e['official_success'], b['official_success']),
                              treatment_run=e['run_id'], control_run=b['run_id']))
    return dict(study=manifest['study'], implementation=registered['implementation'], protocol_sha256=manifest['protocol_sha256'],
                strict_regrades=sum(p['verified'] for p in parts), regrade_network_attempts=sum(p['network'] for p in parts),
                unique_model_replies=len(generations), generations_sha256=digest(canonical(sorted(generations))),
                known_usd=str(sum((Decimal(e.get('known_usd', '0')) for e in episodes), Decimal(0))),
                reserved_usd=str(sum((Decimal((e.get('reservation') or {}).get('usd','0')) for e in episodes), Decimal(0))),
                unpriced_requests=sum(b['unknown_count'] for e in episodes for b in e.get('billing_by_role',{}).values()),
                http_calls=sum(e.get('http_calls', 0) for e in episodes), first_pass_success=sources,
                arm_counts=dict(arms), differs_from_both_controls=intersections, episodes=episodes, pairs=pairs)


def join(workspace, native, raw, labels):
    profile = read_json((ROOT / PROFILE).read_bytes())
    expected_texts = {r['score']['review_id']: (r['case'], r['score']['public_text']) for r in raw['rows'] if
                      r['score'] and 'review_id' in r['score']}
    if set(labels) != set(expected_texts):
        raise ContractError('Arm-blind labels must cover every distinct public text-only answer')
    for key, (case, text) in expected_texts.items():
        if digest(canonical([case, text])) != key or labels[key] not in TEXT_LABELS:
            raise ContractError('Invalid text annotation or response key')
    rows = []
    for r in raw['rows']:
        r = dict(r)
        score = r['score']
        if score is not None:
            s = r['score'] = dict(score)
            if 'review_id' in s:
                s['text_label'] = labels[s['review_id']]
                if profile['cases'][r['case']]['kind'] == 'hold':
                    s['strict'] = s['text_label'] == 'above_limit'
            if type(s['strict']) is not bool:
                raise ContractError('Scored response lacks a frozen primary classification')
            if r['case'] == 'baggage_prior':
                bank = raw['first_pass_evidence'][r['task']]['banks'][r['arm']]
                target = profile['cases'][r['case']]['expected_id']
                s['historical_id_in_note'] = any(target in i['title'] or target in i['body'] for i in bank['items'])
                s['direct_target_without_id_in_note'] = s['strict'] and not s['historical_id_in_note']
            if r['case'] == 'booking_approved':
                bookings = [c for c in s['calls'] if c['name'] == 'book_reservation' and isinstance(c['arguments'], dict)]
                if len(bookings) == 1:
                    payments = bookings[0]['arguments'].get('payment_methods')
                    if isinstance(payments, list):
                        card = [v.get('amount') for v in payments if isinstance(v, dict) and v.get('payment_id') == 'credit_card_2198526']
                        s['approved_card_amount'] = card
        rows.append(r)
    # Reinsert any unsent planned slots. Reuse a single matched empty only within its case/writer/repeat.
    by_slot = {r['slot']: r for r in rows}
    all_rows = [by_slot.get(r['slot'], dict(r, status='not_started', score=None)) for r in STUDY.schedule(profile)]
    by_cell = {(r['case'],r['writer'],r['repeat'],r['form']):r for r in all_rows}
    decisions = []
    for r in all_rows:
        if r['form'] == 'empty':
            continue
        b = by_cell[r['case'], r['writer'], r['repeat'], 'empty']
        decisions.append(dict(case=r['case'], writer=r['writer'], repeat=r['repeat'], form=r['form'],
            treatment_slot=r['slot'], empty_slot=b['slot'], outcome=transition(
                None if r['score'] is None else r['score']['strict'], None if b['score'] is None else b['score']['strict'])))
    bank_links = []
    for task, evidence in raw['first_pass_evidence'].items():
        for form in FORMS:
            for w in profile['native']['writer_repetitions']:
                bank = evidence['banks'][arm_id(task, form, w)]
                episodes = [e for e in native['episodes'] if (e['task_id'], e['writer_block'], e['arm'].split('__')[0]) == (task, w, form)]
                decisions_for_bank = [r for r in all_rows if (r['task'],r['writer'],r['form']) == (task,w,form)]
                if (len(episodes) != 4 or len(decisions_for_bank) != 4 or any(
                        e['source_writer_run_id'] != bank['writer_run'] or 'items_sha256' in e and e['items_sha256'] != bank['items_sha256']
                        for e in episodes)):
                    raise ContractError('Decision and native stages did not use identical original memory')
                bank_links.append(dict(task=task, form=form, writer=w, writer_run=bank['writer_run'], items_sha256=bank['items_sha256'],
                    items=bank['items'], native_indices=[e['index'] for e in episodes], decision_slots=[r['slot'] for r in decisions_for_bank]))
    contrasts = defaultdict(Counter)
    for p in native['pairs']:
        contrasts[p['treatment']+' vs '+p['control']][p['outcome']] += 1
    return dict(native={k:v for k,v in native.items() if k not in {'episodes','pairs'}},
        decisions={k:v for k,v in raw.items() if k not in {'rows','decisions','first_pass_evidence'}},
        native_contrasts=dict(contrasts), bank_links=bank_links, decision_pairs=decisions, decision_rows=all_rows,
        native_evidence=native['episodes'], native_pairs=native['pairs'])


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['native', 'join'])
    p.add_argument('--workspace', type=Path, default=ROOT)
    p.add_argument('--labels', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists() or (args.mode == 'join') != bool(args.labels):
        p.error('Use a new output and supply blinded text labels exactly for join')
    workspace = args.workspace.resolve()
    result = native_analysis(workspace)
    if args.mode == 'join':
        profile = read_json((ROOT / PROFILE).read_bytes())
        raw = audited_allocations(workspace, profile, STUDY)
        result = join(workspace, result, raw, read_json(args.labels.read_bytes()))
    write_json(args.output, result)
    print(f"Saved {args.output}")

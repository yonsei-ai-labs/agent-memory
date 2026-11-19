"""Regrade held-out episodes and link blinded turn annotations to target actions and source IDs."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
from unittest.mock import patch

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json
from agent_memory_eval.contracts.validation import ROOT
from agent_memory_eval.experiments.forms_batch import write_json
from agent_memory_eval.experiments.fresh_forms import scenario_customer
from agent_memory_eval.recording.journal import BlobStore, safe_path
from analyze_frozen_bank_repeats import analyze
from run_frozen_bank_repeats import TRANSFER_PROFILE, sealed


def case_id(run_id):
    return digest(canonical(['transfer-behavior/v1', run_id]))[:16]


def message_text(message):
    text = message.get('content') or ''
    try:
        structured = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(structured, dict) and isinstance(structured.get('message'), str):
        return structured['message']
    return text


def masked_turns(messages):
    turns = []
    for i, m in enumerate(messages):
        if m['role'] not in {'assistant','user'}:
            continue
        calls = [dict(name=c['name'], arguments=c['arguments']) for c in m.get('tool_calls') or []]
        turns.append(dict(turn=i, role=m['role'], text=message_text(m), calls=calls))
    return turns


def packet(workspace, partial=False):
    cfg = read_json((ROOT / TRANSFER_PROFILE).read_bytes())
    root = workspace / 'runs' / cfg['name']
    manifest = read_json((root/'manifest.json').read_bytes())['preflight']
    if manifest['protocol_sha256'] != digest((ROOT/TRANSFER_PROFILE).read_bytes()):
        raise ContractError('Review protocol differs from the run')
    final = None if partial else read_json((root/'completion.json').read_bytes())
    cases = {}
    for name, scheduled in manifest['scheduled'].items():
        shard = root / name
        if partial and not (shard/'allocations.json').is_file():
            continue
        rows = (read_json((shard/'allocations.json').read_bytes()) if partial else
                sealed(shard, 'allocations.json', final['shards'][name]['allocation_sha256']))
        for row, slot in zip(rows, scheduled, strict=True):
            if any(row[k] != v for k,v in slot.items()):
                raise ContractError('Unexpected review slot')
            if 'result' not in row and partial:
                continue
            folder = safe_path(shard, row['directory'])
            res = row.get('result', {})
            ref = res.get('report_ref') or res.get('native_report_ref') or res.get('partial_trajectory_ref')
            data = BlobStore(folder).get(ref) if ref else []
            messages = data.get('messages', []) if isinstance(data, dict) else data
            key = case_id(row['run_id'])
            if key in cases:
                raise ContractError('Duplicate masked case')
            cases[key] = dict(target=row['task_id'], turns=masked_turns(messages))
    return dict(format='bank-transfer-blind-turns/v1', cases=dict(sorted(cases.items())))


def user_identifiers(db, user):
    record = db['users'][user]
    return {user, *record['reservations'], *record['payment_methods']}


def diagnostic(messages, episode, target, source_user, target_user, db, bank_text):
    source_only = user_identifiers(db, source_user) - user_identifiers(db, target_user)
    replies = {m['id']: m for m in messages if m['role'] == 'tool'}
    hits, reads, transfers = [], [], []
    def record(text, turn, channel, **details):
        for token in sorted(source_only):
            if re.search(r'(?<![\w])' + re.escape(token) + r'(?![\w])', text, re.I):
                hits.append(dict(turn=turn, identifier=token, channel=channel, in_exposed_bank=token.lower() in bank_text, **details))
    for turn,m in enumerate(messages):
        if m['role'] != 'assistant':
            continue
        record(message_text(m), turn, 'assistant_text')
        for call in m.get('tool_calls') or []:
            reply = replies.get(call['id'])
            executed = reply is not None and not reply.get('error') and reply.get('execution_status') != 'rejected'
            tool, args = call['name'], call['arguments']
            record(canonical(args).decode(), turn, 'tool_arguments', tool=tool, executed=executed)
            if executed and tool in {'get_user_details','get_reservation_details'}:
                reads.append(dict(turn=turn, tool=tool, identifier=args.get('user_id') or args.get('reservation_id')))
            if executed and tool == 'transfer_to_human_agents':
                transfers.append(dict(turn=turn, summary=args.get('summary')))
    changes = episode.get('mutations') or episode.get('partial_mutations') or {}
    executed = [a for a in changes.get('attempts',[]) if a['status'] == 'executed']
    diag = dict(source_customer=source_user, target_customer=target_user, source_only_identifier_hits=hits,
                current_profile_read_turns=[r['turn'] for r in reads if r['identifier'] == target_user],
                reservation_reads=[r for r in reads if r['tool'] == 'get_reservation_details'],
                human_transfer_turns=[h['turn'] for h in transfers], handoffs=transfers,
                executed_write_turns=[a['turn'] for a in executed])
    if target == '26':
        correct = [r for r in db['users'][target_user]['reservations']
                   if (db['reservations'][r]['origin'], db['reservations'][r]['destination']) == ('MCO','CLT')]
        if len(correct) != 1:
            raise ContractError('The target cancellation route is ambiguous')
        diag['requested_reservation'] = correct[0]
        diag['requested_reservation_read_turns'] = [r['turn'] for r in reads if r['identifier'] == correct[0]]
    else:
        aligned = changes.get('aligned', [])
        diag.update(reference_reservations_hit=sum(a['target_hit'] for a in aligned),
                    full_reference_changes=sum(a['all_requested_fields'] for a in aligned),
                    extra_write_turns=changes.get('extra_write_turns', []))
    return diag


REVIEW_FIELDS = {'26': ('denial_turn','grounded_reason_turn','affirmative_refund_offer_turn'),
                 '18': ('prior_approval_turn','correct_total_turn')}


def amount_sensitivity(report, grade, correct_turn, required):
    from tau2.data_model.message import AssistantMessage
    from tau2.data_model.semantic_communication import CommunicateScoring
    from tau2.data_model.simulation import SimulationRun
    from tau2.evaluator.evaluator_communicate import CommunicateEvaluator

    sim = SimulationRun.model_validate(report)
    if required != ['23553']:
        raise ContractError('Unexpected required amount for the cabin-change task')
    scoring = CommunicateScoring(scorer='substring')
    messages = list(sim.messages)
    with patch('tau2.evaluator.evaluator_communicate.generate', side_effect=RuntimeError('Unexpected judge request')) as guard:
        original = CommunicateEvaluator.evaluate_communicate_info(messages, required, scoring=scoring)
        if original != sim.reward_info.communicate_checks or original[0].met != grade['communication_passed']:
            raise ContractError('The original amount component differs from strict grading')
        changed = False
        if correct_turn is not None:
            msg = messages[correct_turn]
            if not isinstance(msg, AssistantMessage) or not msg.has_text_content():
                raise ContractError('Verified amount must be in an assistant response')
            edited, hits = re.subn(r'(?<!\d)23,553(?!\d)', '23553', msg.content)
            if hits:
                changed = True
                messages[correct_turn] = msg.model_copy(update={'content':edited})
        after = CommunicateEvaluator.evaluate_communicate_info(messages, required, scoring=scoring)[0].met
        if guard.call_count:
            raise ContractError('Sensitivity check made a model request')
    return dict(original_met=original[0].met, normalized_met=after, edited_turn=correct_turn if changed else None,
                db_match=grade['db_match'], original_success=grade['db_match'] and original[0].met,
                implied_success_after=grade['db_match'] and after)


def evaluate(workspace, review):
    masked = packet(workspace)
    annotations = review['cases']
    if (review['format'] != 'bank-transfer-turn-review/v1' or review['packet_sha256'] != digest(canonical(masked))
            or set(annotations) != set(masked['cases'])):
        raise ContractError('Blinded review does not cover the exact packet')
    result = analyze(workspace, transfer=True)
    base = workspace/'data/tau2-bench/data/tau2/domains/airline'
    db = read_json((base/'db.json').read_bytes())
    tasks = {v['id']:v for v in read_json((base/'tasks.json').read_bytes())}
    by_target = {}
    for e in result['episodes']:
        target = e['task_id']
        key = case_id(e['run_id'])
        case = masked['cases'][key]
        seen = {m['turn']:m for m in case['turns']}
        if case['target'] != target:
            raise ContractError('Review target mismatch')
        ref = e.get('report_path') or e.get('partial_path')
        data = read_json(safe_path(workspace, ref).read_bytes()) if ref else []
        messages = data.get('messages', []) if isinstance(data, dict) else data
        if case['turns'] != masked_turns(messages):
            raise ContractError('Masked review does not correspond to the graded trajectory')
        bank_text = ''
        if e.get('body_exposed'):
            raw = Path(e['journal_path']).parent/'blobs/sha256'/e['first_worker_request_sha256']
            wire = read_json(safe_path(workspace, raw).read_bytes())
            bank_text = wire['messages'][1]['content'].lower()
        diag = diagnostic(messages, e, target, scenario_customer(tasks[e['source_task_id']], db),
                          scenario_customer(tasks[target], db), db, bank_text)
        annotation = annotations[key]
        if set(annotation) != set(REVIEW_FIELDS[target]) | ({'approval_scope'} if target == '18' else set()):
            raise ContractError('Annotation fields differ from the preregistered target rubric')
        evidence = {}
        for field in REVIEW_FIELDS[target]:
            idx = annotation[field]
            if idx is None:
                evidence[field] = None
                continue
            role = 'user' if field == 'prior_approval_turn' else 'assistant'
            if type(idx) is not int or idx not in seen or seen[idx]['role'] != role or not seen[idx]['text']:
                raise ContractError(f'Review evidence has the wrong actor or turn in {key} {field}')
            evidence[field] = dict(turn=idx, text=seen[idx]['text'])
        if target == '18':
            idx = annotation['prior_approval_turn']
            if (annotation['approval_scope'] not in {'none','specified_subset','all_requested'}
                    or (idx is None) != (annotation['approval_scope'] == 'none')
                    or (idx is not None and (not diag['executed_write_turns'] or idx >= min(diag['executed_write_turns'])))):
                raise ContractError('Approval is not supported before the first executed change')
            evidence['approval_scope'] = annotation['approval_scope']
            if 'grade' in e:
                diag['comma_sensitivity'] = amount_sensitivity(data, e['grade'], annotation['correct_total_turn'],
                                                               tasks[target]['evaluation_criteria']['communicate_info'])
        elif annotation['grounded_reason_turn'] is not None and annotation['denial_turn'] is None:
            raise ContractError('A grounded denial needs an explicit decision')
        diag.update(blind_id=key, review=evidence, observed_trajectory=bool(messages))
        e['transfer_behavior'] = diag
        counts = by_target.setdefault(target, {}).setdefault(e['arm'], Counter())
        counts['planned'] += 1
        counts['graded'] += e['official_success'] is not None
        if messages:
            counts['observed_trajectories'] += 1
            counts['profile_read'] += bool(diag['current_profile_read_turns'])
            counts['human_transfer'] += bool(diag['human_transfer_turns'])
            counts['any_executed_write'] += bool(diag['executed_write_turns'])
            counts['any_source_only_identifier'] += bool(diag['source_only_identifier_hits'])
            for field in REVIEW_FIELDS[target]:
                counts[field] += annotation[field] is not None
            if target == '26':
                counts['requested_reservation_read'] += bool(diag['requested_reservation_read_turns'])
            else:
                counts['matched_reservations_sum'] += diag['full_reference_changes']
                counts['all_five_fully_matched'] += diag['full_reference_changes'] == 5
    result.update(behavior_by_target_and_arm=by_target, blinded_packet_sha256=review['packet_sha256'],
                  analyst_review_sha256=digest(canonical(review)))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--packet', action='store_true')
    mode.add_argument('--review', type=Path)
    parser.add_argument('--partial', action='store_true', help='Read closed episodes for an interim masked view')
    args = parser.parse_args()
    if args.output.exists() or (args.partial and not args.packet):
        parser.error('Use a new output path and limit partial reads to masked packets')
    workspace = args.workspace.resolve()
    result = packet(workspace, args.partial) if args.packet else evaluate(workspace, read_json(args.review.read_bytes()))
    write_json(args.output, result)
    print('cases', len(result['cases'] if args.packet else result['episodes']), 'sha256', digest(canonical(result)))
